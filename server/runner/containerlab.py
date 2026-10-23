"""containerlab adapter executed inside the learner VM.

The topology is deployed from the learner's work copy (~lab/network-lab/<dir>/<definition>) as the
`lab` user, exactly like the `lab` shell command, so the CLI and the web see the same containers and
the same clab-topo-file label. Only topologies inside the work copy are ever destroyed.

In-container commands record their PID inside the container. Killing the outer `docker exec` does not
stop the inner process, so on timeout or cancel the adapter kills that PID and confirms it is gone.
"""
import os
import shutil
import time
import uuid

from . import proc, store

MISSING_MARKERS = ('No such container', 'is not running', 'not found', 'OCI runtime exec failed',
                   'Error response from daemon')
WRAPPER = 'echo $$ > "$0"; exec "$@"'


class ExecResult:
    def __init__(self, rc, out, err, transport=False, timed_out=False, cancelled=False,
                 confirmed=True, truncated=False):
        self.rc, self.out, self.err = rc, out, err
        self.transport, self.timed_out, self.cancelled = transport, timed_out, cancelled
        self.confirmed, self.truncated = confirmed, truncated


def run_host(argv, timeout, cancel=None):
    """Run a declared command on the learner VM itself (node 'host'), as root, in its own group."""
    r = proc.run(list(argv), timeout, cancel=cancel, new_group=True)
    transport = r.rc == 127 and not r.timed_out and not r.cancelled and (
        'No such file' in r.err or 'not found' in r.err or not r.out)
    return ExecResult(r.rc, r.out, r.err, transport, r.timed_out, r.cancelled, r.stopped, r.truncated)


def copy_work(paths, ldef, bundle_dir, target):
    """Create a lesson work copy from the bundle if absent. Never overwrites learner edits."""
    source = os.path.join(bundle_dir, 'files', ldef['dir'])
    if not os.path.isdir(source):
        return False
    if not os.path.isdir(target):
        shutil.copytree(source, target)
        uid = store.lab_uid(paths)
        if uid is not None:
            for current, dirs, files in os.walk(target):
                for name in [current] + [os.path.join(current, n) for n in dirs + files]:
                    os.chown(name, uid, uid)
            parent = os.path.dirname(target)
            while parent.startswith(paths.lab_home + os.sep):
                os.chown(parent, uid, uid)
                parent = os.path.dirname(parent)
    return True


class Containerlab:
    name = 'containerlab'

    def __init__(self, paths):
        self.paths = paths

    # ── paths ──
    def lesson_dir(self, ldef):
        return store.work_dir(self.paths, ldef)

    def topology(self, ldef):
        return os.path.join(self.lesson_dir(ldef), ldef['definition'])

    def ensure_work_copy(self, ldef, bundle_dir):
        """Create the work copy of a lesson from the bundle if the learner has none.

        Existing work copies are never overwritten: the learner may have edited them.
        """
        if os.path.exists(self.topology(ldef)):
            return True
        copy_work(self.paths, ldef, bundle_dir, self.lesson_dir(ldef))
        return os.path.exists(self.topology(ldef))

    def containers(self, ldef):
        return [n for n in ldef['nodes'] if n != 'host']

    def _as_lab(self, argv):
        return ['runuser', '-u', self.paths.lab_user, '--', 'env', 'HOME=' + self.paths.lab_home, *argv]

    # ── observation ──
    def running_topologies(self):
        """Topology files of containerlab containers (running or not); None if docker cannot be read."""
        r = proc.run(['docker', 'ps', '-a', '--filter', 'label=clab-topo-file',
                      '--format', '{{.Label "clab-topo-file"}}'], 20)
        if r.rc != 0:
            return None
        return sorted({line.strip() for line in r.out.splitlines() if line.strip()})

    def node_states(self, ldef):
        r = proc.run(['docker', 'ps', '-a', '--filter', 'name=^' + ldef['prefix'] + '-',
                      '--format', '{{.Names}}\t{{.State}}'], 20)
        if r.rc != 0:
            return None
        states = {}
        for line in r.out.splitlines():
            name, _, status = line.partition('\t')
            if name.startswith(ldef['prefix'] + '-'):
                states[name[len(ldef['prefix']) + 1:]] = status.strip()
        return states

    def ready(self, ldef):
        states = self.node_states(ldef)
        return bool(states) and all(states.get(node) == 'running' for node in self.containers(ldef))

    def current(self, ldef):
        """True if this lesson's topology (and only from the work copy) is deployed."""
        topologies = self.running_topologies()
        if topologies is None:
            return None
        return os.path.realpath(self.topology(ldef)) in {os.path.realpath(t) for t in topologies}

    # ── lifecycle ──
    def up(self, ldef, timeout):
        r = proc.run(self._as_lab(['containerlab', 'deploy', '-t', self.topology(ldef)]), timeout,
                     cwd=self.lesson_dir(ldef), new_group=True)
        return r.rc == 0 and not r.timed_out, (r.out + r.err)[-4000:]

    def down_all(self, timeout, keep=None):
        """Destroy every containerlab topology of the work copy (except `keep`).

        Refuses, without destroying anything, if a deployed topology lives outside the work copy or
        its file is gone — the same safety rule as the shell `lab` command.
        """
        topologies = self.running_topologies()
        if topologies is None:
            return False, 'docker 상태를 읽지 못했습니다.'
        roots = [os.path.realpath(self.paths.work_root), os.path.realpath(self.paths.labs_root)]
        targets = []
        for topo in topologies:
            resolved = os.path.realpath(topo)
            if keep and resolved == os.path.realpath(keep):
                continue
            inside = any(resolved.startswith(root + os.sep) for root in roots)
            if not inside or not resolved.endswith(('.clab.yml', '.clab.yaml')):
                return False, f'실습 폴더 밖의 토폴로지는 자동으로 철거하지 않습니다: {topo}'
            if not os.path.isfile(resolved):
                return False, f'토폴로지 파일이 없어 안전하게 철거할 수 없습니다: {topo}'
            targets.append(resolved)
        log = []
        for topo in targets:
            r = proc.run(self._as_lab(['containerlab', 'destroy', '-t', topo]), timeout,
                         cwd=os.path.dirname(topo), new_group=True)
            log.append((r.out + r.err)[-2000:])
            if r.rc != 0 or r.timed_out:
                return False, '\n'.join(log)[-4000:]
        return True, '\n'.join(log)[-4000:]

    # ── commands inside nodes ──
    def exec(self, ldef, node, argv, timeout, cancel=None, track=None):
        if node == 'host':
            return run_host(argv, timeout, cancel)
        container = ldef['prefix'] + '-' + node
        pidfile = f'/tmp/.netlab-exec-{uuid.uuid4().hex}.pid'
        if track:
            track(container, pidfile)
        r = proc.run(['docker', 'exec', container, 'sh', '-c', WRAPPER, pidfile, *argv], timeout, cancel=cancel)
        transport = (r.rc in (125, 126, 127) or any(m in r.err for m in MISSING_MARKERS)) \
            and not r.timed_out and not r.cancelled
        confirmed = True
        if r.timed_out or r.cancelled:
            confirmed = self.stop_inner(container, pidfile)
        self._cleanup(container, pidfile)
        if track:
            track(None, None)
        return ExecResult(r.rc, r.out, r.err, transport, r.timed_out, r.cancelled, confirmed, r.truncated)

    def stop_inner(self, container, pidfile, wait=3.0):
        """Kill the in-container process recorded in pidfile and confirm it has ended."""
        for sig in ('TERM', 'KILL'):
            proc.run(['docker', 'exec', container, 'sh', '-c',
                      'p=$(cat "$0" 2>/dev/null) || exit 0; kill -' + sig + ' "$p" 2>/dev/null; exit 0', pidfile], 10)
            deadline = time.monotonic() + wait / 2
            while time.monotonic() < deadline:
                alive = self._inner_alive(container, pidfile)
                if alive is False:
                    return True
                time.sleep(0.2)
        return self._inner_alive(container, pidfile) is False

    def _inner_alive(self, container, pidfile):
        r = proc.run(['docker', 'exec', container, 'sh', '-c',
                      'p=$(cat "$0" 2>/dev/null) || exit 3; kill -0 "$p" 2>/dev/null && exit 0; exit 1', pidfile], 10)
        if r.rc == 0:
            return True
        if r.rc in (1, 3):
            return False            # exited, or never started (no pidfile)
        if any(m in r.err for m in ('No such container', 'is not running')):
            return False            # the container itself is gone
        return None                 # unknown: treat as not confirmed

    def _cleanup(self, container, pidfile):
        proc.run(['docker', 'exec', container, 'rm', '-f', pidfile], 10)

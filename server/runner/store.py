"""Runner state, locks and environment identity inside the learner VM.

State is published by write-temp → fsync → rename → fsync(dir), so status readers always see a
complete document without taking a lock. A missing or unreadable state file after installation is
reported as damaged; it is never silently reset to generation 0 (contract "세대와 상태").
"""
import errno
import fcntl
import json
import os
import pwd
import socket
import time
import uuid

STATE_VERSION = 1
RECENT_LIMIT = 32


class Paths:
    def __init__(self, prefix=None):
        prefix = prefix or os.environ.get('NETLAB_RUNNER_PREFIX') or ''
        self.test = bool(prefix)
        j = lambda *p: os.path.join(prefix or '/', *[x.lstrip('/') for x in p])
        self.opt = j('/opt/netlab')
        self.current = j('/opt/netlab/current')
        self.packs = j('/opt/netlab/current/packs')
        self.state_dir = j('/var/lib/netlab/runner')
        self.state = os.path.join(self.state_dir, 'state.json')
        self.installed = os.path.join(self.state_dir, 'installed.json')
        self.ctl_lock = os.path.join(self.state_dir, 'ctl.lock')
        self.exec_lock = os.path.join(self.state_dir, 'exec.lock')
        self.checks = os.path.join(self.state_dir, 'checks')
        self.lab_user = 'lab'
        self.lab_home = j('/home/lab')
        self.work_root = os.path.join(self.lab_home, 'network-lab')
        self.cli_lock = os.path.join(self.lab_home, '.netlab-switch.lock')   # same lock as lab-shell `lab`
        self.labs_root = os.path.join(self.lab_home, 'labs')
        self.devlxd = '/dev/lxd/sock'
        self.boot_id = '/proc/sys/kernel/random/boot_id'
        if self.test:
            self.devlxd = None
            self.incarnation_file = j('/test/incarnation')
            self.boot_id = j('/test/boot_id')


def work_dir(paths, ldef):
    """Learner's work copy of a lesson. The network pack keeps its historic ~/network-lab/<dir>
    layout (shell `lab`/`doc`/`walk` depend on it); other packs live under ~/labs/<pack>/<lesson dir>."""
    if ldef.get('pack', 'network') == 'network':
        return os.path.join(paths.work_root, ldef['dir'])
    return os.path.join(paths.labs_root, ldef['pack'], os.path.basename(ldef['dir'].rstrip('/')))


def fsync_write(path, data, mode=0o600):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    tmp = os.path.join(directory, f'.{os.path.basename(path)}.{os.getpid()}.{uuid.uuid4().hex[:6]}')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load_state(paths):
    """Return (state, damaged). damaged=True means the file exists but cannot be trusted,
    or it vanished after installation."""
    try:
        with open(paths.state, 'rb') as f:
            state = json.loads(f.read())
        if not isinstance(state, dict) or state.get('version') != STATE_VERSION:
            return None, True
        for key, kind in (('generation', int), ('state', str)):
            if not isinstance(state.get(key), kind) or isinstance(state.get(key), bool):
                return None, True
        return state, False
    except FileNotFoundError:
        return None, os.path.exists(paths.installed)
    except (OSError, ValueError):
        return None, True


def save_state(paths, state):
    state['version'] = STATE_VERSION
    state['updated'] = time.time()
    fsync_write(paths.state, json.dumps(state, sort_keys=True).encode())


def initial_state(incarnation, boot_id):
    return dict(version=STATE_VERSION, incarnation=incarnation, generation=0, boot_id=boot_id,
                state='idle', lesson=None, lesson_def=None, bundle=None, ready=False, stage=None,
                op=None, check=None, reserve=None, last_cancel=None, failure=None, recovery=None,
                recent=[])


def remember(state, request_id, response):
    """Keep a small record per request_id so a resend is answered, never re-executed."""
    if not request_id:
        return
    recent = [r for r in state.get('recent') or [] if r.get('request_id') != request_id]
    slim = {k: response.get(k) for k in ('ok', 'state', 'lesson', 'executed', 'error', 'message', 'noop')}
    if isinstance(response.get('result'), dict):
        slim['result'] = response['result']
    recent.append(dict(request_id=request_id, response=slim))
    state['recent'] = recent[-RECENT_LIMIT:]


def recalled(state, request_id):
    for item in (state or {}).get('recent') or []:
        if item.get('request_id') == request_id:
            return item['response']
    return None


# ─────────────────────────────── locks ───────────────────────────────

class Lock:
    """flock(2) wrapper. acquire(wait=0) is non-blocking; wait>0 retries briefly (control lock)."""

    def __init__(self, path, owner_uid=None):
        self.path, self.owner_uid, self.fd = path, owner_uid, None

    def acquire(self, wait=0.0):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        created = not os.path.exists(self.path)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o664)
        if created and self.owner_uid is not None:
            try:
                os.fchown(fd, self.owner_uid, self.owner_uid)
            except OSError:
                pass
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = fd
                return True
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    os.close(fd)
                    raise
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.02)

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def lab_uid(paths):
    try:
        return pwd.getpwnam(paths.lab_user).pw_uid
    except KeyError:
        return None


# ─────────────────────────────── identity ───────────────────────────────

def read_boot_id(paths):
    try:
        with open(paths.boot_id) as f:
            return f.read().strip() or None
    except OSError:
        return None


def read_incarnation(paths):
    """The host-assigned incarnation (LXD config user.netlab.incarnation via /dev/lxd), or None."""
    if paths.test:
        try:
            with open(paths.incarnation_file) as f:
                return f.read().strip() or None
        except OSError:
            return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(paths.devlxd)
            sock.sendall(b'GET /1.0/config/user.netlab.incarnation HTTP/1.0\r\nHost: lxd\r\n\r\n')
            data = b''
            while len(data) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
    except OSError:
        return None
    head, _, body = data.partition(b'\r\n\r\n')
    status = head.split(b'\r\n', 1)[0].split()
    if len(status) < 2 or status[1] != b'200':
        return None
    value = body.decode('utf-8', 'replace').strip()
    if value.startswith('"'):          # some LXD versions return a JSON string
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value or None

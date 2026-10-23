"""Runner actions: status, start, reset, stop, solve, check, probe.

Locks
  ctl.lock   short critical sections around state reads/writes and reservations
  exec.lock  held for the whole duration of a change or an inspection (check/probe)
  cli lock   ~lab/.netlab-switch.lock, shared with the shell `lab` command; held by changes only

Order of a change: ctl → verify tokens/state → take cli lock → reserve → (cancel a running inspection
and confirm it ended) → exec lock → ctl → re-verify tokens → generation+1 and an incomplete-op marker
are made durable → perform → ctl → final state. Changes never wait for each other: they get `busy`.
"""
import json
import os
import signal
import tempfile
import time

from . import checks, proc, store
from .containerlab import Containerlab
from .protocol import PROTOCOL, ProtocolError, parse
from .shell import Shell

CAPABILITIES = ['status', 'start', 'reset', 'stop', 'solve', 'check', 'probe']
MAX_RESPONSE = 64 * 1024
CANCEL_GRACE = 8.0
KILL_GRACE = 2.0
RESULT_LIMIT = 4096
PROBE_OUTPUT = 2000
TRANSITIONAL = {'start': 'starting', 'reset': 'resetting', 'stop': 'stopping', 'solve': 'solving'}
ALLOWED = {
    'start': {'idle', 'running', 'start_failed', 'reset_failed'},
    'reset': {'running', 'reset_failed'},
    'stop': {'running', 'start_failed', 'reset_failed', 'stop_failed'},
    'solve': {'running'},
}
MESSAGES = {
    'busy': '다른 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요.',
    'conflict': '실습 환경이 그사이 바뀌었습니다. 상태를 새로 확인한 뒤 다시 시도하세요.',
    'bundle_mismatch': '이 실습 환경의 교재 묶음이 서버와 다릅니다. 환경을 교체해야 합니다.',
    'unsupported_action': '이 실습 환경은 아직 그 동작을 지원하지 않습니다.',
    'recovery_required': '실습 환경 상태를 확인할 수 없습니다. 복구 안내를 따르세요.',
    'cancel_failed': '진행 중인 확인을 멈추지 못해 변경하지 않았습니다.',
    'interrupted': '이전 작업이 중단되었습니다. 다시 실행하지 않았습니다.',
}


class Runner:
    def __init__(self, paths=None, adapters=None):
        self.paths = paths or store.Paths()
        self.adapters = adapters or {'containerlab': Containerlab(self.paths), 'shell': Shell(self.paths)}

    def adapter(self, ldef):
        return self.adapters[(ldef or {}).get('runtime') or 'containerlab']

    # ─────────────────────────── bundle ───────────────────────────
    def installed(self):
        """{pack: (manifest, recipe)} of the installed bundles."""
        found = {}
        try:
            names = sorted(os.listdir(self.paths.packs))
        except OSError:
            return found
        for name in names:
            base = os.path.join(self.paths.packs, name)
            try:
                with open(os.path.join(base, 'manifest.json')) as f:
                    manifest = json.load(f)
                with open(os.path.join(base, 'recipe.json')) as f:
                    recipe = json.load(f)
            except (OSError, ValueError):
                continue
            if manifest.get('pack') == name and recipe.get('pack') == name:
                found[name] = (manifest, recipe)
        return found

    def lesson_def(self, lesson_id):
        pack = lesson_id.partition('/')[0]
        manifest, recipe = self.installed().get(pack, (None, None))
        if not recipe or lesson_id not in recipe.get('lessons', {}):
            return None, None
        ldef = dict(recipe['lessons'][lesson_id], id=lesson_id, pack=pack)
        return ldef, manifest['bundle']

    def bundle_dir(self, pack):
        return os.path.join(self.paths.packs, pack)

    def bundle_file(self, ldef, relative):
        return os.path.join(self.bundle_dir(ldef['pack']), 'files', ldef['dir'], relative)

    # ─────────────────────────── responses ───────────────────────────
    def respond(self, request, state, ok, error=None, message=None, executed=None, result=None, **extra):
        observed = None
        if state is not None:
            observed = dict(incarnation=state.get('incarnation'), generation=state.get('generation'))
        body = dict(protocol=PROTOCOL, ok=ok,
                    state=state['state'] if state else 'recovery_required',
                    lesson=state.get('lesson') if state else None,
                    observed=observed, executed=executed,
                    bundle=state.get('bundle') if state else None,
                    result=result, error=error,
                    message=message or (MESSAGES.get(error) if error else None))
        if request and request.get('request_id'):
            body['request_id'] = request['request_id']
        body.update(extra)
        data = json.dumps(body, ensure_ascii=False)
        if len(data.encode()) > MAX_RESPONSE and isinstance(result, dict):
            for item in result.get('items', []):
                item['detail'] = item.get('detail', '')[-200:]
                item['truncated'] = True
            if isinstance(result.get('output'), str):
                result['output'] = result['output'][:500]
        return body

    # ─────────────────────────── preparation ───────────────────────────
    def prepare(self, state):
        """Apply identity changes, reboot and interrupted-work rules. Caller holds ctl.lock.
        Returns (state, changed)."""
        incarnation = store.read_incarnation(self.paths)
        boot = store.read_boot_id(self.paths)
        changed = False
        if incarnation and state.get('incarnation') != incarnation:
            # New incarnation = the host restored or replaced this VM. Tokens of the old one are void.
            state = store.initial_state(incarnation, boot)
            return state, True
        if boot and state.get('boot_id') != boot:
            state['generation'] += 1
            state['boot_id'] = boot
            state['check'] = state['reserve'] = None
            ldef = state.get('lesson_def') or {}
            if state.get('op'):
                self._interrupt(state, 'interrupted')
            elif state['state'] == 'running' and state.get('ready') and ldef.get('runtime') == 'shell':
                pass       # files on the VM disk survive a restart; the durable ready marker is enough
            elif state['state'] in ('running', 'starting', 'resetting', 'stopping', 'solving'):
                # Container links do not survive a VM restart; never resume as running unverified.
                state.update(state='recovery_required', ready=False,
                             recovery=dict(reason='reboot', allowed=self._recovery_actions(state)))
            changed = True
        op = state.get('op')
        if op and not proc.process_alive(op.get('pid'), op.get('ticks')):
            self._interrupt(state, 'interrupted')
            changed = True
        check = state.get('check')
        if check and not proc.process_alive(check.get('pid'), check.get('ticks')):
            store.remember(state, check.get('request_id'),
                           dict(ok=False, state=state['state'], error='interrupted'))
            state['check'] = None
            changed = True
        reserve = state.get('reserve')
        if reserve and not proc.process_alive(reserve.get('pid'), reserve.get('ticks')):
            state['reserve'] = None
            changed = True
        return state, changed

    def _recovery_actions(self, state):
        return ['reset', 'stop'] if state.get('lesson') else ['stop']

    def _interrupt(self, state, reason):
        op = state.get('op') or {}
        store.remember(state, op.get('request_id'), dict(ok=False, state='recovery_required', error='interrupted'))
        state.update(state='recovery_required', ready=False, op=None,
                     recovery=dict(reason=reason, action=op.get('action'), stage=state.get('stage'),
                                   allowed=self._recovery_actions(state)))

    def locked_state(self, wait=5.0):
        """(ctl_lock, state, damaged) with preparation applied, or (None, None, None) if busy."""
        ctl = store.Lock(self.paths.ctl_lock)
        if not ctl.acquire(wait=wait):
            return None, None, None
        state, damaged = store.load_state(self.paths)
        if damaged:
            return ctl, None, True
        if state is None:
            state = store.initial_state(store.read_incarnation(self.paths), store.read_boot_id(self.paths))
            store.save_state(self.paths, state)
        state, changed = self.prepare(state)
        if changed:
            store.save_state(self.paths, state)
        return ctl, state, False

    # ─────────────────────────── entry ───────────────────────────
    def handle(self, raw):
        try:
            request = parse(raw)
        except ProtocolError as exc:
            return self.respond(None, None, False, error=exc.code, message=str(exc))
        if not os.path.isdir(self.paths.state_dir):
            os.makedirs(self.paths.state_dir, mode=0o700, exist_ok=True)
        action = request['action']
        if action == 'status':
            return self.status(request)
        if action not in CAPABILITIES:
            ctl, state, damaged = self.locked_state()
            if ctl:
                ctl.release()
            return self.respond(request, state, False, error='unsupported_action')
        if action in ('check', 'probe'):
            return self.inspect(request)
        return self.change(request)

    # ─────────────────────────── status ───────────────────────────
    def status(self, request):
        ctl, state, damaged = self.locked_state(wait=0.3)
        if ctl:
            ctl.release()
        else:
            state, damaged = store.load_state(self.paths)
            if state and state.get('boot_id') != store.read_boot_id(self.paths):
                state = dict(state, state='recovering')
        if damaged or state is None:
            return self.respond(request, None, False, error='recovery_required',
                                message='실습 환경의 상태 기록이 손상되었거나 사라졌습니다. 자동으로 정리하지 않았습니다.',
                                recovery=dict(reason='state_damaged', allowed=[]),
                                capabilities=CAPABILITIES, installed=self._installed_map())
        if not state.get('incarnation'):
            return self.respond(request, state, False, error='recovery_required',
                                message='호스트가 지정한 환경 식별자를 읽지 못했습니다.',
                                capabilities=CAPABILITIES, installed=self._installed_map())
        runtime = self._runtime_view(state)
        return self.respond(request, state, True, ready=bool(state.get('ready')),
                            recovery=state.get('recovery'),
                            failure=state.get('failure'),
                            check_running=bool(state.get('check')),
                            runtime=runtime, capabilities=CAPABILITIES,
                            installed=self._installed_map())

    def _installed_map(self):
        return {pack: manifest['bundle'] for pack, (manifest, _) in self.installed().items()}

    def _runtime_view(self, state):
        clab = self.adapters['containerlab']
        topologies = clab.running_topologies()
        if topologies is None:
            return dict(topologies=None, matches=None)
        home = os.path.realpath(self.paths.lab_home)
        shown = [os.path.relpath(os.path.realpath(t), home) if os.path.realpath(t).startswith(home + os.sep)
                 else t for t in topologies]
        matches = None
        ldef = state.get('lesson_def')
        if ldef and state['state'] == 'running':
            if ldef.get('runtime') == 'shell':
                matches = not topologies          # a terminal-started topology means someone switched away
            else:
                matches = bool(clab.current(ldef)) and len(topologies) == 1
        elif state['state'] == 'idle':
            matches = not topologies
        return dict(topologies=shown, matches=matches)

    # ─────────────────────────── validation ───────────────────────────
    def _tokens_match(self, state, request):
        expected = request['expected']
        return (expected['incarnation'] == state.get('incarnation')
                and expected['generation'] == state.get('generation'))

    def _precheck(self, state, request):
        """Return (error, message, ldef, bundle) for a change; error None if acceptable."""
        action = request['action']
        if not self._tokens_match(state, request):
            return 'conflict', None, None, None
        current = state['state']
        recovery = state.get('recovery') or {}
        if current == 'recovery_required':
            if action not in recovery.get('allowed', []):
                return 'recovery_required', None, None, None
        elif current not in ALLOWED[action]:
            return 'conflict', f'지금 상태({current})에서는 이 동작을 할 수 없습니다.', None, None
        if action == 'start':
            ldef, bundle = self.lesson_def(request['target_lesson'])
            if not ldef:
                return 'bundle_mismatch', '설치된 교재 묶음에 이 실습이 없습니다.', None, None
            if bundle != request['expected_bundle']:
                return 'bundle_mismatch', None, None, None
            return None, None, ldef, bundle
        if request['lesson'] != state.get('lesson'):
            return 'conflict', '요청한 실습이 현재 실습과 다릅니다.', None, None
        if action in ('reset', 'solve'):
            ldef, bundle = self.lesson_def(request['lesson'])
            if not ldef or bundle != request['expected_bundle']:
                return 'bundle_mismatch', None, None, None
            if action == 'solve' and not (ldef.get('hooks') or {}).get('solve'):
                return 'unsupported_action', '이 실습은 해설 적용(solve)을 제공하지 않습니다.', None, None
            if action == 'solve' and not state.get('ready'):
                return 'conflict', '실습이 실행 중일 때만 해설을 적용할 수 있습니다.', None, None
            return None, None, ldef, bundle
        return None, None, state.get('lesson_def'), state.get('bundle')     # stop: no bundle requirement

    def _alive(self, record):
        return bool(record) and proc.process_alive(record.get('pid'), record.get('ticks'))

    def _me(self, request):
        return dict(request_id=request['request_id'], action=request['action'], pid=os.getpid(),
                    ticks=proc.start_time(os.getpid()), started=time.time())

    # ─────────────────────────── changes ───────────────────────────
    def change(self, request):
        ctl, state, damaged = self.locked_state()
        if ctl is None:
            return self.respond(request, None, False, error='busy')
        cli = exe = None
        reserved = False
        try:
            if damaged:
                return self.respond(request, None, False, error='recovery_required',
                                    recovery=dict(reason='state_damaged', allowed=[]))
            replay = store.recalled(state, request['request_id'])
            if replay:
                return dict(self.respond(request, state, replay.get('ok'), error=replay.get('error'),
                                         message=replay.get('message'), executed=replay.get('executed'),
                                         result=replay.get('result')), replayed=True)
            if self._alive(state.get('op')) or self._alive(state.get('reserve')):
                return self.respond(request, state, False, error='busy')
            error, message, ldef, bundle = self._precheck(state, request)
            if error:
                return self.respond(request, state, False, error=error, message=message)
            if request['action'] == 'start' and self._noop_start(state, ldef):
                response = self.respond(request, state, True, executed=None, noop=True,
                                        message='이미 실행 중인 실습입니다. 컨테이너와 변경 사항을 유지했습니다.')
                store.remember(state, request['request_id'], response)
                store.save_state(self.paths, state)
                return response
            cli = store.Lock(self.paths.cli_lock, store.lab_uid(self.paths))
            if not cli.acquire():
                cli = None
                return self.respond(request, state, False, error='busy',
                                    message='터미널에서 실습을 전환하는 중입니다. 끝난 뒤 다시 시도하세요.')
            running_check = state.get('check') if self._alive(state.get('check')) else None
            previous = state.get('lesson_def') if state['state'] != 'idle' else None
            state['reserve'] = self._me(request)
            store.save_state(self.paths, state)
            reserved = True
        finally:
            ctl.release()

        try:
            if running_check:
                confirmed, exe = self.cancel_check(running_check)
                if not confirmed:
                    self._update(lambda s: s.update(
                        reserve=None, state='recovery_required', ready=False,
                        recovery=dict(reason='exec_unconfirmed', allowed=[])))
                    reserved = False
                    return self.respond(request, self._read(), False, error='cancel_failed')
            if exe is None:
                exe = store.Lock(self.paths.exec_lock)
                if not exe.acquire(wait=1.0):
                    exe = None
                    return self.respond(request, self._read(), False, error='busy')
            ctl = store.Lock(self.paths.ctl_lock)
            if not ctl.acquire(wait=5.0):
                return self.respond(request, self._read(), False, error='busy')
            try:
                state, damaged = store.load_state(self.paths)
                if damaged or state is None:
                    return self.respond(request, None, False, error='recovery_required')
                if not self._tokens_match(state, request):    # changed while we cancelled
                    state['reserve'] = None
                    store.save_state(self.paths, state)
                    reserved = False
                    return self.respond(request, state, False, error='conflict')
                state['generation'] += 1
                state.update(state=TRANSITIONAL[request['action']], ready=False, reserve=None,
                             failure=None, recovery=None, stage=None, last_cancel=None,
                             op=self._me(request))
                if request['action'] == 'start':
                    state.update(lesson=ldef['id'], lesson_def=ldef, bundle=bundle)
                store.save_state(self.paths, state)
                reserved = False
                executed = dict(incarnation=state['incarnation'], generation=state['generation'])
            finally:
                ctl.release()
            ok, stage, log, previous_failed = self.perform(request['action'], ldef, previous)

            def finish(s):
                s['op'] = None
                if ok:
                    if request['action'] == 'stop':
                        s.update(state='idle', lesson=None, lesson_def=None, bundle=None, ready=False, stage=None)
                    else:
                        s.update(state='running', ready=True, stage=None)
                elif previous_failed:
                    # The old lesson could not be stopped: it is still (partly) there; the new one never started.
                    s.update(state='stop_failed', lesson=previous['id'], lesson_def=previous,
                             bundle=s.get('bundle'), ready=False, stage=stage,
                             failure=dict(stage=stage, log_tail=log[-4000:]))
                else:
                    s.update(state=request['action'] + '_failed', ready=False, stage=stage,
                             failure=dict(stage=stage, log_tail=log[-4000:]))
                if request['action'] == 'solve' and not ok:
                    s.update(state='running', ready=True)     # a failed solve leaves the lesson running
            state = self._update(finish)
            response = self.respond(request, state, ok, executed=executed,
                                    error=None if ok else 'failed',
                                    message=None if ok else f'{stage} 단계에서 실패했습니다.',
                                    stage=None if ok else stage)
            self._update(lambda s: store.remember(s, request['request_id'], response))
            return response
        finally:
            if reserved:
                self._update(lambda s: s.update(reserve=None))
            if exe:
                exe.release()
            if cli:
                cli.release()

    def _noop_start(self, state, ldef):
        if state['state'] != 'running' or state.get('lesson') != ldef['id'] or not state.get('ready'):
            return False
        adapter = self.adapter(ldef)
        current = adapter.current(ldef)
        return (current is None or bool(current)) and adapter.ready(ldef)

    # ─────────────────────────── performing ───────────────────────────
    def perform(self, action, ldef, previous=None):
        """Run the runtime work of a change. Returns (ok, failed_stage, log, previous_stop_failed)."""
        log = []

        def step(stage, fn):
            self._stage(stage)
            good, out = fn()
            log.append(out or '')
            return good

        def text():
            return '\n'.join(x for x in log if x)[-4000:]

        if action == 'solve':
            ok = step('solve', lambda: self.run_hook(ldef, 'solve'))
            return ok, None if ok else 'solve', text(), False

        if action == 'start' and previous:
            if not step('teardown', lambda: self.run_hook(previous, 'teardown')):
                return False, 'teardown', text(), True
        if action in ('stop', 'reset') and ldef:
            if action == 'reset' and (ldef.get('hooks') or {}).get('reset'):
                ok = step('reset_hook', lambda: self.run_hook(ldef, 'reset'))
                return ok, None if ok else 'reset_hook', text(), False
            if not step('teardown', lambda: self.run_hook(ldef, 'teardown')):
                return False, 'teardown', text(), False
        clab = self.adapters['containerlab']
        timeouts = (ldef or {}).get('timeouts', {})
        # One lesson at a time: also clear topologies started from the terminal (same rule as `lab`).
        if not step('runtime_down', lambda: clab.down_all(timeouts.get('teardown', 180))):
            return False, 'runtime_down', text(), action == 'start' and bool(previous)
        if action == 'stop':
            return True, None, text(), False
        adapter = self.adapter(ldef)
        if not adapter.ensure_work_copy(ldef, self.bundle_dir(ldef['pack'])):
            return False, 'runtime_up', '작업 사본을 만들지 못했습니다.', False
        if not step('runtime_up', lambda: adapter.up(ldef, timeouts.get('setup', 300))):
            return False, 'runtime_up', text(), False
        deadline = time.monotonic() + 60
        while not adapter.ready(ldef):
            if time.monotonic() > deadline:
                return False, 'runtime_up', text() + '\n노드가 준비되지 않았습니다.', False
            time.sleep(1)
        for hook in ('setup', 'inject'):
            if not step(hook, lambda hook=hook: self.run_hook(ldef, hook)):
                return False, hook, text(), False
        return True, None, text(), False

    def hook_env(self, ldef, **extra):
        env = dict(os.environ)
        env.update(NETLAB_PACK=ldef['pack'], NETLAB_LESSON=ldef['id'], NETLAB_RUNTIME=ldef.get('runtime', ''),
                   NETLAB_LESSON_DIR=self.bundle_file(ldef, ''), NETLAB_WORK_DIR=store.work_dir(self.paths, ldef),
                   NETLAB_STATE_DIR=self.paths.state_dir, NETLAB_LOCALE='ko', HOME='/root')
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def run_hook(self, ldef, name):
        """Run a lesson hook from the installed bundle (cwd: the work copy). Missing hook = nothing to do."""
        relative = (ldef.get('hooks') or {}).get(name)
        if not relative:
            return True, ''
        script = self.bundle_file(ldef, relative)
        if not os.path.isfile(script) or os.path.islink(script):
            return False, f'{name} 훅 파일이 설치되어 있지 않습니다: {relative}'
        cwd = store.work_dir(self.paths, ldef)
        if not os.path.isdir(cwd):
            cwd = os.path.dirname(script)
        timeout = (ldef.get('timeouts') or {}).get(name, 120)
        state = self._read() or {}
        r = proc.run([script], timeout, env=self.hook_env(ldef, NETLAB_GENERATION=state.get('generation', 0)),
                     cwd=cwd, new_group=True)
        out = (r.out + r.err)[-4000:]
        if r.timed_out:
            return False, out + f'\n{name} 훅이 {timeout}초 안에 끝나지 않았습니다.'
        return r.rc == 0, out

    def run_script_check(self, ldef, step_id):
        """Callback for script checks: run the check from the bundle and read its result file."""
        def run(item, cancel):
            script = self.bundle_file(ldef, item['script'])
            fd, result_path = tempfile.mkstemp(prefix='result-', dir=self.paths.checks)
            os.close(fd)
            try:
                env = self.hook_env(ldef, NETLAB_STEP=step_id, NETLAB_RESULT=result_path,
                                    NETLAB_GENERATION=(self._read() or {}).get('generation', 0))
                cwd = store.work_dir(self.paths, ldef)
                r = proc.run([script], item.get('timeout', 30), cancel=cancel, env=env,
                             cwd=cwd if os.path.isdir(cwd) else None, new_group=True)
                r.confirmed = r.stopped
                r.transport = False
                r.message, r.claimed = self._result_file(result_path)
                return r
            finally:
                try:
                    os.unlink(result_path)
                except OSError:
                    pass
        return run

    @staticmethod
    def _result_file(path):
        """(message, claimed status) from a check's result file; anything malformed is ignored."""
        try:
            info = os.lstat(path)
            if not os.path.isfile(path) or os.path.islink(path) or info.st_size > RESULT_LIMIT:
                return None, None
            with open(path, 'rb') as f:
                data = json.loads(f.read(RESULT_LIMIT).decode('utf-8'))
        except (OSError, ValueError, UnicodeDecodeError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        message = data.get('message') if isinstance(data.get('message'), str) else None
        claimed = data.get('status') if data.get('status') in ('pass', 'fail', 'unverifiable', 'error') else None
        return (message[:2048] if message else None), claimed

    def _stage(self, stage):
        self._update(lambda s: s.update(stage=stage))

    def _read(self):
        state, _ = store.load_state(self.paths)
        return state

    def _update(self, fn):
        """Apply fn to the stored state under ctl.lock and return the new state."""
        ctl = store.Lock(self.paths.ctl_lock)
        ctl.acquire(wait=30.0)
        try:
            state, damaged = store.load_state(self.paths)
            if state is None:
                return None
            fn(state)
            store.save_state(self.paths, state)
            return state
        finally:
            ctl.release()

    # ─────────────────────────── cancel ───────────────────────────
    def cancel_check(self, record):
        """Ask a running inspection to stop and wait until it has. Returns (confirmed, exec_lock or None)."""
        try:
            os.kill(record['pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass
        exe = store.Lock(self.paths.exec_lock)
        if exe.acquire(wait=CANCEL_GRACE):
            last = (self._read() or {}).get('last_cancel') or {}
            if last.get('request_id') == record['request_id'] and not last.get('confirmed'):
                exe.release()
                return False, None
            return True, exe
        kill_tree(record['pid'])
        if not exe.acquire(wait=KILL_GRACE):
            return False, None
        sidecar = os.path.join(self.paths.checks, record['request_id'] + '.json')
        try:
            with open(sidecar) as f:
                target = json.load(f)
        except (OSError, ValueError):
            target = {}
        confirmed = True
        if target.get('container'):
            adapter = self.adapters.get(record.get('runtime') or 'containerlab', self.adapters['containerlab'])
            confirmed = adapter.stop_inner(target['container'], target['pidfile'])
        try:
            os.unlink(sidecar)
        except OSError:
            pass
        if not confirmed:
            exe.release()
            return False, None
        self._update(lambda s: s.update(check=None))
        return True, exe

    # ─────────────────────────── check / probe ───────────────────────────
    def inspect(self, request):
        """check and probe share one path: tokens, lesson, exec lock, cancellation."""
        kind = request['action']
        ctl, state, damaged = self.locked_state()
        if ctl is None:
            return self.respond(request, None, False, error='busy')
        exe = None
        try:
            if damaged:
                return self.respond(request, None, False, error='recovery_required',
                                    recovery=dict(reason='state_damaged', allowed=[]))
            replay = store.recalled(state, request['request_id'])
            if replay:
                return dict(self.respond(request, state, replay.get('ok'), error=replay.get('error'),
                                         executed=replay.get('executed'), result=replay.get('result')),
                            replayed=True)
            if not self._tokens_match(state, request):
                return self.respond(request, state, False, error='conflict')
            if state['state'] == 'recovery_required':
                return self.respond(request, state, False, error='recovery_required')
            if self._alive(state.get('op')) or self._alive(state.get('reserve')):
                return self.respond(request, state, False, error='busy')
            if state['state'] != 'running' or not state.get('ready'):
                return self.respond(request, state, False, error='conflict',
                                    message='실습이 실행 중일 때만 확인할 수 있습니다.')
            if request['lesson'] != state.get('lesson'):
                return self.respond(request, state, False, error='conflict',
                                    message='요청한 실습이 현재 실습과 다릅니다.')
            ldef, bundle = self.lesson_def(request['lesson'])
            if not ldef or bundle != request['expected_bundle'] or bundle != state.get('bundle'):
                return self.respond(request, state, False, error='bundle_mismatch')
            if kind == 'check':
                target = ldef.get('steps', {}).get(request['step'])
                if not target or 'check' not in target:
                    return self.respond(request, state, False, error='invalid_request',
                                        message='등록되지 않은 단계입니다.')
            else:
                target = ldef.get('probes', {}).get(request['probe'])
                if not target:
                    return self.respond(request, state, False, error='invalid_request',
                                        message='등록되지 않은 진단입니다.')
            exe = store.Lock(self.paths.exec_lock)
            if not exe.acquire():
                exe = None
                return self.respond(request, state, False, error='busy')
            state['check'] = dict(self._me(request), runtime=ldef['runtime'],
                                  step=request.get('step'), probe=request.get('probe'))
            store.save_state(self.paths, state)
            executed = dict(incarnation=state['incarnation'], generation=state['generation'])
        finally:
            ctl.release()

        cancel = _Flag()
        previous = signal.signal(signal.SIGTERM, lambda *_: cancel.set())
        sidecar = os.path.join(self.paths.checks, request['request_id'] + '.json')
        outcome = dict(cancelled=False, confirmed=True, unconfirmed_timeout=False)
        try:
            adapter = self.adapter(ldef)
            if adapter.current(ldef) is False:
                return self.respond(request, self._read(), False, error='conflict',
                                    message='터미널에서 실습 환경이 바뀌었습니다. 다시 시작한 뒤 확인하세요.')

            def track(container, pidfile):
                if container:
                    store.fsync_write(sidecar, json.dumps(dict(container=container, pidfile=pidfile)).encode())
                else:
                    try:
                        os.unlink(sidecar)
                    except OSError:
                        pass

            try:
                if kind == 'check':
                    result = checks.evaluate(adapter, ldef, target['check'], cancel=cancel, track=track,
                                             script=self.run_script_check(ldef, request['step']))
                else:
                    result = self.probe(adapter, ldef, target, cancel, track)
            except checks.Cancelled as exc:
                outcome.update(cancelled=True, confirmed=exc.confirmed)
                result = dict(status='cancelled', items=[])
            except RuntimeError as exc:
                outcome['unconfirmed_timeout'] = True
                result = dict(status='error', items=[], message=str(exc))
            return self.respond(request, self._read(), True, executed=executed, result=result)
        finally:
            signal.signal(signal.SIGTERM, previous)

            def finish(s):
                if (s.get('check') or {}).get('request_id') == request['request_id']:
                    s['check'] = None
                if outcome['cancelled']:
                    s['last_cancel'] = dict(request_id=request['request_id'], confirmed=outcome['confirmed'])
                if outcome['unconfirmed_timeout'] or (outcome['cancelled'] and not outcome['confirmed']):
                    s.update(state='recovery_required', ready=False,
                             recovery=dict(reason='exec_unconfirmed', allowed=[]))
            self._update(finish)
            try:
                os.unlink(sidecar)
            except OSError:
                pass
            if exe:
                exe.release()

    def probe(self, adapter, ldef, target, cancel, track):
        timeout = (ldef.get('timeouts') or {}).get('probe', 20)
        r = adapter.exec(ldef, target['node'], target['argv'], timeout, cancel=cancel, track=track)
        if r.cancelled or (cancel is not None and cancel.is_set()):
            raise checks.Cancelled(r.confirmed)
        if r.timed_out and not r.confirmed:
            raise RuntimeError('inner process did not stop after timeout')
        status = 'error' if r.timed_out else 'unverifiable' if r.transport else 'ok'
        output = r.out + r.err
        return dict(status=status, rc=r.rc, node=target['node'], output=output[:PROBE_OUTPUT],
                    truncated=len(output) > PROBE_OUTPUT or r.truncated)


class _Flag:
    def __init__(self):
        self.value = False

    def set(self):
        self.value = True

    def is_set(self):
        return self.value


def kill_tree(pid):
    """SIGKILL pid and all of its descendants (never a whole process group: lxd-agent may share it)."""
    children = {}
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            try:
                with open(f'/proc/{entry}/stat') as f:
                    ppid = int(f.read().rsplit(')', 1)[1].split()[1])
                children.setdefault(ppid, []).append(int(entry))
            except (OSError, ValueError, IndexError):
                pass
    order, stack = [], [pid]
    while stack:
        current = stack.pop()
        order.append(current)
        stack.extend(children.get(current, []))
    for target in order:
        try:
            os.kill(target, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

"""In-process host boundary for learning pack lessons (M1).

Same interface and refusal rules as the later host agent service: the caller names an account and
registered ids, never an LXD name, path or argv. The instance comes from the server's ownership
record; the only guest command is the fixed runner path with a validated JSON body on stdin.
This class runs inside labd, which keeps its existing host privileges — it is not a privilege
boundary yet (contract "우선순위와 범위").
"""
import json
import subprocess
import threading
import time
import uuid

from runner.protocol import ProtocolError, validate

from .store import LOCK, STATE

RUNNER_PATH = '/opt/netlab/current/runner/netlab-runner'
INCARNATION_KEY = 'user.netlab.incarnation'
TIMEOUTS = {'status': 30, 'start': 480, 'reset': 480, 'stop': 300, 'solve': 300, 'check': 180, 'probe': 90}
MAX_RESPONSE = 64 * 1024 + 1024
STATUS_CACHE_SECONDS = 2.0
MISSING_CACHE_SECONDS = 60.0   # v1 VMs have no runner; do not probe them on every panel poll


class AgentError(Exception):
    def __init__(self, code, message, http=409):
        super().__init__(message)
        self.code, self.message, self.http = code, message, http


class RunnerMissing(AgentError):
    def __init__(self):
        super().__init__('runner_missing', '이 실습 환경에는 단계형 실습 실행기가 없습니다. 새 환경에서 사용할 수 있습니다.', 410)


def _lxc(*args, timeout=30, stdin=None):
    p = subprocess.run(['lxc', *args], input=stdin, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr.decode('utf-8', 'replace')


class AgentClient:
    def __init__(self, lxc=_lxc):
        self.lxc = lxc
        self._status_cache = {}
        self._missing = {}
        self._incarnations = {}
        self._lock = threading.Lock()

    # ── ownership ──
    def instance_for(self, account):
        with LOCK:
            rec = dict(STATE['users'].get(account) or {})
        if not rec.get('instance'):
            raise AgentError('no_environment', '실습 환경이 없습니다. 먼저 환경을 시작하세요.')
        if rec.get('status') != 'running':
            raise AgentError('no_environment', '실습 환경이 실행 중일 때만 사용할 수 있습니다.')
        return rec['instance']

    def ensure_incarnation(self, instance):
        with self._lock:
            if instance in self._incarnations:
                return self._incarnations[instance]
        rc, out, err = self.lxc('config', 'get', instance, INCARNATION_KEY)
        if rc != 0:
            raise AgentError('runner_error', '환경 정보를 읽지 못했습니다.', 502)
        value = out.decode().strip()
        if not value:
            value = self._set(instance)
        with self._lock:
            self._incarnations[instance] = value
        return value

    def _set(self, instance):
        value = str(uuid.uuid4())
        rc, _, _ = self.lxc('config', 'set', instance, f'{INCARNATION_KEY}={value}')
        if rc != 0:
            raise AgentError('runner_error', '환경 식별자를 설정하지 못했습니다.', 502)
        return value

    def forget(self, instance):
        """Drop cached identity/status (after restore, replacement or any change)."""
        with self._lock:
            self._incarnations.pop(instance, None)
            self._status_cache.pop(instance, None)
            self._missing.pop(instance, None)

    # ── calls ──
    def status(self, account, fresh=False):
        instance = self.instance_for(account)
        with self._lock:
            cached = self._status_cache.get(instance)
        if cached and not fresh and time.monotonic() - cached[0] < STATUS_CACHE_SECONDS:
            return cached[1]
        response = self._run(instance, {'protocol': 1, 'action': 'status'})
        with self._lock:
            self._status_cache[instance] = (time.monotonic(), response)
        return response

    def call(self, account, request):
        """Send a validated request for this account's own environment and return the response."""
        try:
            validate(request)
        except ProtocolError as exc:
            raise AgentError('invalid_request', str(exc), 400) from None
        instance = self.instance_for(account)
        if request['action'] != 'status':
            capabilities = (self.status(account).get('capabilities') or [])
            if request['action'] not in capabilities:
                raise AgentError('unsupported_action', '이 실습 환경은 그 동작을 지원하지 않습니다.')
        try:
            return self._run(instance, request)
        finally:
            with self._lock:
                self._status_cache.pop(instance, None)

    def _run(self, instance, request):
        with self._lock:
            missing_since = self._missing.get(instance)
        if missing_since is not None and time.monotonic() - missing_since < MISSING_CACHE_SECONDS:
            raise RunnerMissing()
        self.ensure_incarnation(instance)
        body = json.dumps(request, separators=(',', ':')).encode()
        try:
            rc, out, err = self.lxc('exec', instance, '--', RUNNER_PATH,
                                    timeout=TIMEOUTS[request['action']], stdin=body)
        except subprocess.TimeoutExpired:
            raise AgentError('runner_error', '실습 실행기가 제한 시간 안에 응답하지 않았습니다.', 504) from None
        if len(out) > MAX_RESPONSE:
            raise AgentError('runner_error', '실습 실행기 응답이 너무 큽니다.', 502)
        try:
            response = json.loads(out.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            response = None
        if not isinstance(response, dict) or response.get('protocol') != 1 or not isinstance(response.get('ok'), bool):
            if self._runner_absent(instance):
                with self._lock:
                    self._missing[instance] = time.monotonic()
                raise RunnerMissing()
            raise AgentError('runner_error', '실습 실행기가 올바르게 응답하지 않았습니다.', 502)
        return self._sanitize(response)

    def _runner_absent(self, instance):
        rc, _, _ = self.lxc('exec', instance, '--', 'test', '-x', RUNNER_PATH, timeout=20)
        return rc != 0

    @staticmethod
    def _sanitize(response):
        """Treat runner output as untrusted: keep known fields with expected types only."""
        def token(value):
            if (isinstance(value, dict) and isinstance(value.get('incarnation'), str)
                    and isinstance(value.get('generation'), int) and not isinstance(value.get('generation'), bool)):
                return dict(incarnation=value['incarnation'][:64], generation=value['generation'])
            return None

        def text(value, limit=2000):
            return value[:limit] if isinstance(value, str) else None

        out = dict(ok=response['ok'], state=text(response.get('state'), 40), lesson=text(response.get('lesson'), 81),
                   observed=token(response.get('observed')), executed=token(response.get('executed')),
                   bundle=text(response.get('bundle'), 64), error=text(response.get('error'), 40),
                   message=text(response.get('message'), 500), noop=response.get('noop') is True,
                   ready=response.get('ready') is True, check_running=response.get('check_running') is True,
                   stage=text(response.get('stage'), 40), replayed=response.get('replayed') is True)
        result = response.get('result')
        if isinstance(result, dict) and isinstance(result.get('status'), str):
            items = []
            for item in result.get('items') or []:
                if isinstance(item, dict):
                    items.append(dict(index=item.get('index') if isinstance(item.get('index'), int) else None,
                                      label=text(item.get('label'), 120), node=text(item.get('node'), 40),
                                      result=text(item.get('result'), 20),
                                      rc=item.get('rc') if isinstance(item.get('rc'), int) else None,
                                      detail=text(item.get('detail'), 800),
                                      message=text(item.get('message'), 2048),
                                      fallback_used=item.get('fallback_used') is True))
            out['result'] = dict(status=result['status'][:20], items=items[:20])
            if isinstance(result.get('output'), str):           # probe answers
                out['result'].update(output=result['output'][:2000], node=text(result.get('node'), 40),
                                     rc=result.get('rc') if isinstance(result.get('rc'), int) else None,
                                     truncated=result.get('truncated') is True)
        else:
            out['result'] = None
        for key in ('recovery', 'failure', 'runtime'):
            value = response.get(key)
            out[key] = value if isinstance(value, dict) else None
        if out['failure']:
            out['failure'] = dict(stage=text(out['failure'].get('stage'), 40),
                                  log_tail=text(out['failure'].get('log_tail'), 4000))
        if out['recovery']:
            allowed = out['recovery'].get('allowed')
            out['recovery'] = dict(reason=text(out['recovery'].get('reason'), 40),
                                   allowed=[a for a in allowed if isinstance(a, str)][:5] if isinstance(allowed, list) else [])
        if out['runtime']:
            tops = out['runtime'].get('topologies')
            out['runtime'] = dict(topologies=[t[:200] for t in tops if isinstance(t, str)][:20] if isinstance(tops, list) else None,
                                  matches=out['runtime'].get('matches') if isinstance(out['runtime'].get('matches'), bool) else None)
        caps = response.get('capabilities')
        out['capabilities'] = [c for c in caps if isinstance(c, str)][:10] if isinstance(caps, list) else []
        installed = response.get('installed')
        out['installed'] = ({k[:40]: v[:64] for k, v in installed.items() if isinstance(k, str) and isinstance(v, str)}
                            if isinstance(installed, dict) else {})
        return out


CLIENT = AgentClient()

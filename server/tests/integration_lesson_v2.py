#!/usr/bin/env python3
"""Real-machine check of the M1 lesson flow on a DEVELOPMENT server (run as root on the host of labd).

    python3 integration_lesson_v2.py --account m1test

Refuses unless /etc/netlab/config.json has environment=development and the account's VM carries
user.netlab.verify=true (a dedicated verification VM). Talks to labd on 127.0.0.1:9000 exactly like
nginx would, so authentication, routing, jobs and the progress DB are the real ones.

  M1-01  start → check(fail) → manual fix → check(pass, completed) → reset → check(fail)
  M1-02  start of the running lesson is a no-op (generation and containers kept)
  M1-03  an old token is refused before anything runs
  M1-05  reset while a check runs: the check is cancelled (or finishes) before the reset, never counted after
  M1-07  damaged runner state: status says recovery_required, nothing is torn down
  M1-10  after completion, reset and stop still work and the completion stays recorded
  --with-cli     the shell `lab` command still works next to the runner (shared lock), and the web
                 notices an environment switched from the terminal instead of judging the wrong lesson
  --with-reboot  after a VM restart the runner asks for explicit recovery; reset recovers
  --m2           M2: solve (12, 03, 17), terminal-started lessons adopted by the web (lab 12, lab 17 ospf/bgp),
                 lab check is reference only, lab reset, the shell-runtime example pack, the legacy `lab 04`
                 path next to the runner, and the tutor's probe path through the runner
"""
import argparse
import http.client
import json
import subprocess
import sys
import time
import uuid

LESSON = 'network/12'
RUNNER_STATE = '/var/lib/netlab/runner/state.json'


def lxc(*args, timeout=120):
    p = subprocess.run(['lxc', *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def lab_cli(instance, command, timeout=600):
    """Run a `lab` shell command as the learner, exactly like the terminal."""
    return lxc('exec', instance, '--', 'runuser', '-u', 'lab', '--', 'bash', '-c',
               '. /etc/lab-shell.sh; ' + command, timeout=timeout)


class Labd:
    def __init__(self, account):
        self.account = account
        with open('/etc/netlab/proxy.secret') as f:
            self.secret = f.read().strip()

    def call(self, method, path, body=None):
        headers = {'X-Lab-Proxy-Auth': self.secret, 'X-Lab-User': self.account,
                   'Host': '127.0.0.1', 'Origin': 'http://127.0.0.1', 'Content-Type': 'application/json'}
        conn = http.client.HTTPConnection('127.0.0.1', 9000, timeout=600)
        conn.request(method, '/' + path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        try:
            return r.status, json.loads(data)
        except ValueError:
            return r.status, data.decode(errors='replace')

    def state(self):
        code, body = self.call('GET', 'lesson/state')
        assert code == 200, (code, body)
        return body

    def token(self):
        return self.state()['environment']['observed']

    def submit(self, path, expected=None, request_id=None):
        body = dict(request_id=request_id or str(uuid.uuid4()), expected=expected or self.token())
        return self.call('POST', path, body)

    def wait(self, accepted, limit=600):
        code, body = accepted
        assert code in (200, 202), accepted
        job_id = body['job_id']
        deadline = time.time() + limit
        while time.time() < deadline:
            code, job = self.call('GET', 'lesson/jobs/' + job_id)
            assert code == 200, (code, job)
            if job['state'] not in ('queued', 'running'):
                return job
            time.sleep(1)
        raise AssertionError('job did not finish: ' + job_id)

    def act(self, action, lesson=LESSON, **kw):
        return self.wait(self.submit(f'lesson/{lesson}/{action}', **kw))

    def check(self, lesson=LESSON, step='verify', **kw):
        return self.wait(self.submit(f'lesson/{lesson}/step/{step}/check', **kw))

    def check_until(self, lesson, want, step='verify', limit=150):
        """Re-check (routing protocols converge in tens of seconds) until `want` or the limit."""
        deadline = time.time() + limit
        while True:
            job = self.check(lesson, step)
            status = ((job['result'].get('runner') or {}).get('result') or {}).get('status')
            if status == want or time.time() > deadline:
                return job, status
            time.sleep(5)


def step(name):
    print(f'· {name}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account', required=True)
    parser.add_argument('--with-cli', action='store_true')
    parser.add_argument('--with-reboot', action='store_true')
    parser.add_argument('--m2', action='store_true')
    args = parser.parse_args()
    config = json.load(open('/etc/netlab/config.json'))
    if config.get('environment') != 'development':
        sys.exit('개발 환경에서만 실행합니다.')
    state = json.load(open('/var/lib/netlab/state.json'))
    instance = (state['users'].get(args.account) or {}).get('instance')
    if not instance:
        sys.exit(f'{args.account} 에 할당된 환경이 없습니다.')
    rc, out, _ = lxc('config', 'get', instance, 'user.netlab.verify')
    if out.strip() != 'true':
        sys.exit(f'{instance} 는 전용 검증 VM 이 아닙니다(user.netlab.verify=true 없음).')
    labd = Labd(args.account)
    started = time.time()

    env = labd.state()['environment']
    if env['state'] != 'idle':
        step(f"정리: 기존 상태 {env['state']} → stop")
        labd.act('stop')

    step('M1-01 start')
    job = labd.act('start')
    assert job['state'] == 'done', job
    view = labd.state()
    gen_after_start = view['environment']['observed']['generation']
    assert view['environment']['state'] == 'running' and view['attempt'], view

    step('M1-02 같은 레슨 start 는 no-op')
    rc, before, _ = lxc('exec', instance, '--', 'docker', 'ps', '-q', '--filter', 'name=clab-12-mtu-pmtud')
    job = labd.act('start')
    assert job['result']['runner']['noop'], job
    rc, after, _ = lxc('exec', instance, '--', 'docker', 'ps', '-q', '--filter', 'name=clab-12-mtu-pmtud')
    assert sorted(before.split()) == sorted(after.split()) and before.strip(), (before, after)
    assert labd.token()['generation'] == gen_after_start

    step('M1-01 check → 미통과')
    job = labd.check()
    assert job['result']['runner']['result']['status'] == 'fail', job
    assert not job['result']['progress']['stale']

    step('M1-03 예전 토큰은 실행 전 거부')
    old = dict(labd.token(), generation=gen_after_start - 1)
    job = labd.act('reset', expected=old)
    assert job['result']['error'] == 'conflict' and job['http_status'] == 409, job
    assert labd.token()['generation'] == gen_after_start

    step('M1-01 수동 수정 (fix.sh, lab 사용자)')
    rc, out, err = lxc('exec', instance, '--', 'runuser', '-u', 'lab', '--', 'bash',
                       '/home/lab/network-lab/12-mtu-pmtud/fix.sh')
    assert rc == 0, err
    job = labd.check()
    assert job['result']['runner']['result']['status'] == 'pass', job
    assert job['result']['progress']['completed_now'], job
    view = labd.state()
    completed_at = view['attempt']['completed_at']
    assert completed_at and view['attempt']['completions'] >= 1 and not view['shared'], view

    step('M1-10 완료 후 reset → 미통과, 완료 기록 유지')
    job = labd.act('reset')
    assert job['state'] == 'done', job
    view = labd.state()
    assert view['attempt']['completed_at'] == completed_at and view['attempt']['steps']['verify'] == 'untested', view
    job = labd.check()
    assert job['result']['runner']['result']['status'] == 'fail', job

    step('M1-05 check 도중 reset: check 는 취소(또는 먼저 끝남), reset 은 그 뒤 실행')
    token = labd.token()
    check_job = labd.submit(f'lesson/{LESSON}/step/verify/check', expected=token)
    time.sleep(0.5)
    reset_job = labd.submit(f'lesson/{LESSON}/reset', expected=token)
    reset = labd.wait(reset_job)
    checked = labd.wait(check_job)
    assert reset['state'] == 'done', reset
    status = (checked['result'].get('runner') or {}).get('result', {}) or {}
    assert checked['result']['progress'].get('stale') or status.get('status') in ('fail', 'cancelled'), checked
    print(f"    check 결과: {status.get('status')} (stale={checked['result']['progress'].get('stale')})")
    assert labd.state()['attempt']['steps']['verify'] == 'untested'

    step('M1-07 runner 상태 손상 → 복구 필요, 자동 철거 없음')
    lxc('exec', instance, '--', 'cp', '-p', RUNNER_STATE, RUNNER_STATE + '.m1bak')
    lxc('exec', instance, '--', 'sh', '-c', f'printf "{{broken" > {RUNNER_STATE}')
    try:
        env = labd.state()['environment']
        assert env['error'] == 'recovery_required' and env['observed'] is None, env
        job = labd.act('stop', expected=token)
        assert job['result']['error'] == 'recovery_required', job
        rc, still, _ = lxc('exec', instance, '--', 'docker', 'ps', '-q', '--filter', 'name=clab-12-mtu-pmtud')
        assert still.strip(), 'containers must not be torn down automatically'
    finally:
        lxc('exec', instance, '--', 'mv', RUNNER_STATE + '.m1bak', RUNNER_STATE)

    if args.with_cli:
        step('CLI: 터미널에서 lab 04 로 바꾸면 웹은 다른 레슨으로 판정하지 않는다')
        rc, out, err = lxc('exec', instance, '--', 'runuser', '-u', 'lab', '--', 'bash', '-c',
                           '. /etc/lab-shell.sh; lab 04', timeout=600)
        assert rc == 0, (out[-500:], err[-500:])
        view = labd.state()
        env = view['environment']
        # M1 lab-shell destroys the runner's topology behind its back (state running, runtime mismatch);
        # M2 lab-shell first stops the runner's lesson through the runner (state idle, attempt closed).
        assert env['runtime']['matches'] is False and env['state'] in ('running', 'idle'), env
        if env['state'] == 'idle':
            assert view['attempt'] is None, view
        job = labd.check()
        assert job['result']['error'] == 'conflict', job
        step('CLI: 웹 start 는 터미널 실습을 내리고 12장을 다시 올린다')
        job = labd.act('start')
        assert job['state'] == 'done' and not job['result']['runner']['noop'], job
        rc, out, _ = lxc('exec', instance, '--', 'docker', 'ps', '--format', '{{.Names}}')
        assert 'clab-04' not in out and 'clab-12-mtu-pmtud-h1' in out, out
        rc, out, err = lxc('exec', instance, '--', 'runuser', '-u', 'lab', '--', 'bash', '-c',
                           '. /etc/lab-shell.sh; lab 12')
        assert rc == 0 and '이미 실행 중' in out, (out[-300:], err[-300:])

    if args.with_reboot:
        step('재부팅: 복구 필요 → reset 으로 복구')
        rc, _, err = lxc('restart', instance, timeout=300)
        assert rc == 0, err
        for _ in range(90):
            if lxc('exec', instance, '--', 'true', timeout=10)[0] == 0:
                break
            time.sleep(2)
        env = labd.state()['environment']
        assert env['state'] == 'recovery_required' and env['recovery']['reason'] == 'reboot', env
        assert set(env['recovery']['allowed']) == {'reset', 'stop'}, env
        job = labd.check()
        assert job['result']['error'] == 'recovery_required', job
        job = labd.act('reset')
        assert job['state'] == 'done', job
        assert labd.state()['environment']['state'] == 'running'

    if args.m2:
        run_m2(labd, instance)

    step('M1-10 stop')
    if labd.state()['environment']['state'] != 'idle':
        job = labd.act('stop')
        assert job['state'] == 'done', job
    view = labd.state()
    assert view['environment']['state'] == 'idle' and view['attempt'] is None, view
    rc, left, _ = lxc('exec', instance, '--', 'docker', 'ps', '-q', '--filter', 'name=clab-12-mtu-pmtud')
    assert not left.strip()
    print(f'OK — M1 실기 검사 통과 ({time.time() - started:.0f}s, {instance})')


def status_of(job):
    return ((job['result'].get('runner') or {}).get('result') or {}).get('status')


def events(account, kind):
    import sqlite3
    db = sqlite3.connect('/var/lib/netlab/learning/progress.sqlite3')
    return db.execute('SELECT count(*) FROM step_events e JOIN attempts a USING(attempt_id) '
                      'WHERE a.account=? AND e.kind=?', (account, kind)).fetchone()[0]


def run_m2(labd, instance):
    step('M2 12장 해설 적용 → 재확인 통과, 도움 사용 기록')
    if labd.state()['environment']['state'] != 'idle':
        labd.act('stop')
    labd.act('start')
    assert status_of(labd.check()) == 'fail'
    job = labd.act('solve')
    assert job['state'] == 'done', job
    view = labd.state()['attempt']
    assert view['assisted'] and view['steps']['verify'] == 'untested', view
    job = labd.check()
    assert status_of(job) == 'pass' and job['result']['progress']['completed_now'], job
    labd.act('stop')

    step('M2 터미널 lab 12 → 웹이 다시 배포하지 않고 회차로 이어 받음')
    rc, out, err = lab_cli(instance, 'lab 12')
    assert rc == 0 and '준비됐습니다' in out, (out[-400:], err[-400:])
    view = labd.state()
    assert view['environment']['lesson'] == LESSON and view['attempt']['origin'] == 'adopted_cli', view
    generation = view['environment']['observed']['generation']
    assert status_of(labd.check()) == 'fail'
    before = events(labd.account, 'check')
    rc, out, _ = lab_cli(instance, 'lab check')
    assert '참고용' in out and events(labd.account, 'check') == before, out
    rc, out, _ = lab_cli(instance, 'lab 12')
    assert '이미 실행 중' in out and labd.token()['generation'] == generation, out
    rc, out, _ = lab_cli(instance, 'lab reset')
    assert rc == 0, out
    view = labd.state()
    assert view['environment']['observed']['generation'] == generation + 1, view
    assert view['attempt']['generation'] == generation + 1 and view['attempt']['steps']['verify'] == 'untested', view

    for variant, lesson in (('ospf', 'network/17-ospf'), ('bgp', 'network/17-bgp')):
        step(f'M2 터미널 lab 17 {variant} → 이어 받기, 해설 적용 후 수렴 대기')
        rc, out, err = lab_cli(instance, f'lab 17 {variant}')
        assert rc == 0, (out[-400:], err[-400:])
        view = labd.state()
        assert view['environment']['lesson'] == lesson and view['attempt']['origin'] == 'adopted_cli', view
        assert status_of(labd.check(lesson)) == 'fail'
        assert labd.act('solve', lesson=lesson)['state'] == 'done'
        job, status = labd.check_until(lesson, 'pass')
        assert status == 'pass', job
        print(f'    {lesson}: 해설 적용 후 통과')

    step('M2 03장 복합 검증 두 조건')
    labd.act('start', lesson='network/03')
    job = labd.check('network/03')
    items = job['result']['runner']['result']['items']
    assert status_of(job) == 'fail' and len(items) == 2, job
    labd.act('solve', lesson='network/03')
    job, status = labd.check_until('network/03', 'pass', limit=60)
    assert status == 'pass' and [i['result'] for i in job['result']['runner']['result']['items']] == ['pass', 'pass'], job

    step('M2 shell 런타임 예제 팩: 훅·스크립트 확인·수동 수정')
    shell = 'example-shell/permissions'
    job = labd.act('start', lesson=shell)
    assert job['state'] == 'done', job
    job = labd.check(shell, 'fix')
    assert status_of(job) == 'fail' and '644' in (job['result']['runner']['result']['items'][0].get('message') or ''), job
    rc, out, err = lxc('exec', instance, '--', 'runuser', '-u', 'lab', '--', 'chmod', '600',
                       '/home/lab/shell-lab/secret.txt')
    assert rc == 0, err
    job = labd.check(shell, 'fix')
    assert status_of(job) == 'pass' and job['result']['progress']['completed_now'], job

    step('M2 튜터: 현재 레슨을 runner 로 알고 등록된 probe 만 실행')
    probe = subprocess.run(['python3', '-c', '''
import sys, json
sys.path.insert(0, "/opt/netlab-admin")
from netlab import store, lessons
store.load_config(); store.load_state()
ctx = lessons.tutor_context(sys.argv[1])
got = lessons.tutor_probe(sys.argv[1], ctx, "mode")
print(json.dumps(dict(lesson=ctx["lesson_id"], step=ctx["step"], output=got.get("output"), error=got.get("error"))))
''', labd.account], capture_output=True, text=True, timeout=120)
    info = json.loads(probe.stdout.strip().splitlines()[-1])
    assert info['lesson'] == shell and '600' in (info['output'] or ''), (info, probe.stderr[-400:])

    step('M2 등록되지 않은 lab 04 는 기존 방식: 실행기 실습을 먼저 내리고 배포')
    rc, out, err = lab_cli(instance, 'lab 04')
    assert rc == 0, (out[-400:], err[-400:])
    rc, gone, _ = lxc('exec', instance, '--', 'test', '-e', '/home/lab/shell-lab')
    assert rc != 0, 'shell lesson teardown must have run'
    view = labd.state()
    assert view['environment']['state'] == 'idle' and view['attempt'] is None, view
    assert view['environment']['runtime']['matches'] is False, view
    rc, out, err = lab_cli(instance, 'unlab')
    assert rc == 0, (out[-300:], err[-300:])


if __name__ == '__main__':
    main()

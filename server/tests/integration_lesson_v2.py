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

    def act(self, action, **kw):
        return self.wait(self.submit(f'lesson/{LESSON}/{action}', **kw))

    def check(self, **kw):
        return self.wait(self.submit(f'lesson/{LESSON}/step/verify/check', **kw))


def step(name):
    print(f'· {name}', flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account', required=True)
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
    completed_at = labd.state()['attempt']['completed_at']
    assert completed_at

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

    step('M1-10 stop')
    job = labd.act('stop')
    assert job['state'] == 'done', job
    view = labd.state()
    assert view['environment']['state'] == 'idle' and view['attempt'] is None, view
    rc, left, _ = lxc('exec', instance, '--', 'docker', 'ps', '-q', '--filter', 'name=clab-12-mtu-pmtud')
    assert not left.strip()
    print(f'OK — M1 실기 검사 통과 ({time.time() - started:.0f}s, {instance})')


if __name__ == '__main__':
    main()

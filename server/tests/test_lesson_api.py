"""Lesson API and progress records with a fake agent client (no LXD). M1-04, M1-08, M1-10."""
import http.client
from http.server import ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).parents[1]))
from netlab import agent_client, lesson_progress, lessons, paths, store  # noqa: E402

INC = '11111111-2222-4333-8444-555555555555'
LESSON = 'network/12'


class FakeClient:
    """Mimics AgentClient: per-account instances, a mutable runner state, scripted check results."""

    def __init__(self):
        self.owned = {'alice': 'vm-alice', 'bob': 'vm-bob', 'guest': 'vm-guest'}
        self.missing = set()
        self.calls = []
        self.check_result = {'status': 'fail', 'items': []}
        self.envs = {account: {'state': 'idle', 'lesson': None, 'generation': 0} for account in self.owned}
        self.check_generation = None      # override the executed generation of the next check
        self.after_call = None

    @property
    def env(self):
        return self.envs['alice']

    def instance_for(self, account):
        if account not in self.owned:
            raise agent_client.AgentError('no_environment', '실습 환경이 없습니다.')
        return self.owned[account]

    def bundle(self):
        return lessons.CATALOG['packs']['network']['bundle']['revision']

    def status(self, account, fresh=False):
        self.instance_for(account)
        if account in self.missing:
            raise agent_client.RunnerMissing()
        env = self.envs[account]
        return dict(ok=True, state=env['state'], lesson=env['lesson'],
                    observed=dict(incarnation=INC, generation=env['generation']),
                    bundle=self.bundle() if env['lesson'] else None, ready=env['state'] == 'running',
                    capabilities=['status', 'start', 'reset', 'stop', 'solve', 'check', 'probe'],
                    installed={'network': self.bundle()}, runtime={'topologies': [], 'matches': True},
                    recovery=None, failure=None)

    def call(self, account, request):
        try:
            return self._call(account, request)
        finally:
            if self.after_call:
                self.after_call(request)

    def _call(self, account, request):
        self.calls.append(request)
        env = self.envs[account]
        token = dict(incarnation=INC, generation=env['generation'])
        if request['expected'] != token:
            return dict(ok=False, error='conflict', observed=token, executed=None, state=env['state'],
                        lesson=env['lesson'], message='conflict')
        action = request['action']
        if action == 'check':
            generation = self.check_generation if self.check_generation is not None else env['generation']
            return dict(ok=True, state='running', lesson=env['lesson'], observed=token,
                        executed=dict(incarnation=INC, generation=generation), bundle=self.bundle(),
                        result=dict(self.check_result))
        if action == 'probe':
            return dict(ok=True, state='running', lesson=env['lesson'], observed=token, executed=token,
                        bundle=self.bundle(), result=dict(status='ok', rc=0, node='r1', output='probe output',
                                                          truncated=False, items=[]))
        if action == 'start' and env['state'] == 'running' and env['lesson'] == request['target_lesson']:
            return dict(ok=True, noop=True, state='running', lesson=env['lesson'], observed=token,
                        executed=None, bundle=self.bundle())
        env['generation'] += 1
        if action == 'stop':
            env.update(state='idle', lesson=None)
        elif action != 'solve':
            env.update(state='running', lesson=request.get('target_lesson') or request['lesson'])
        new = dict(incarnation=INC, generation=env['generation'])
        return dict(ok=True, state=env['state'], lesson=env['lesson'], observed=new, executed=new,
                    bundle=self.bundle())


class SyncPool:
    def __init__(self, defer=False):
        self.defer, self.pending = defer, []

    def submit(self, fn, *args):
        if self.defer:
            self.pending.append((fn, args))
        else:
            fn(*args)

    def drain(self):
        while self.pending:
            fn, args = self.pending.pop(0)
            fn(*args)


class LessonApiCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = FakeClient()
        self.pool = SyncPool()
        for target, value in ((paths, ('LEARNING_DB', self.tmp.name + '/progress.sqlite3')),
                              (lessons, ('CLIENT', self.client)), (lessons, ('_POOL', self.pool))):
            p = patch.object(target, *value)
            p.start()
            self.addCleanup(p.stop)
        store.CONFIG.clear()
        store.CONFIG.update(store.DEFAULTS)

    def token(self, account='alice'):
        env = self.client.envs.get(account, {'generation': 0})   # unknown accounts still send a well-formed token
        return dict(incarnation=INC, generation=env['generation'])

    def post(self, account, path, body):
        return lessons.handle_post(account, path.split('/'), body)

    def act(self, action, account='alice', request_id=None, expected=None):
        body = dict(request_id=request_id or str(uuid.uuid4()), expected=expected or self.token(account))
        path = f'lesson/network/12/{action}' if action != 'check' else 'lesson/network/12/step/verify/check'
        return self.post(account, path, body)

    def job(self, account, response):
        return lessons.handle_get(account, ['lesson', 'jobs', response[1]['job_id']])

    def state(self, account='alice'):
        return lessons.handle_get(account, ['lesson', 'state'])


class LessonApiTests(LessonApiCase):
    def test_public_lesson_hides_private_data(self):
        code, body = lessons.handle_get('alice', ['lesson', 'network', '12'])
        self.assertEqual(code, 200)
        text = json.dumps(body, ensure_ascii=False)
        lesson = lessons.CATALOG['lessons'][LESSON]
        self.assertNotIn(lesson['tutor']['fault'], text)
        for hint in lesson['steps'][1]['hints']:
            self.assertNotIn(hint, text)
        self.assertNotIn('argv', text)
        step = body['lesson']['steps'][0]
        self.assertIn('<h1', step['html'])
        self.assertEqual(body['lesson']['steps'][1]['hints'], 3)

    def test_same_request_id_is_one_job_and_other_content_is_refused(self):
        rid = str(uuid.uuid4())
        first = self.act('start', request_id=rid)
        self.assertEqual(first[0], 202)
        again = self.act('start', request_id=rid, expected=dict(incarnation=INC, generation=0))
        self.assertEqual(again[1]['job_id'], first[1]['job_id'])               # M1-04
        self.assertEqual(len([c for c in self.client.calls if c['action'] == 'start']), 1)
        other = self.act('reset', request_id=rid)
        self.assertEqual((other[0], other[1]['error']), (409, 'conflict'))

    def test_jobs_and_environments_of_other_accounts_are_invisible(self):
        started = self.act('start')
        self.assertEqual(self.job('bob', started)[0], 404)                     # M1-08
        self.assertEqual(self.job('alice', started)[0], 200)
        code, body = self.act('start', account='mallory')
        self.assertEqual((code, body['error']), (409, 'no_environment'))
        bob_view = self.state('bob')[1]
        self.assertIsNone(bob_view['attempt'])

    def test_completion_is_recorded_once_and_environment_stays_controllable(self):
        self.act('start')
        self.client.check_result = {'status': 'fail', 'items': []}
        failed = self.job('alice', self.act('check'))[1]
        self.assertEqual(failed['result']['progress']['status'], 'fail')
        self.client.check_result = {'status': 'pass', 'items': []}
        passed = self.job('alice', self.act('check'))[1]
        self.assertTrue(passed['result']['progress']['completed_now'])
        again = self.job('alice', self.act('check'))[1]
        self.assertFalse(again['result']['progress']['completed_now'])
        view = self.state()[1]['attempt']
        self.assertEqual((view['completions'], view['steps']['verify']), (1, 'pass'))
        completed_at = view['completed_at']
        self.assertEqual(self.job('alice', self.act('reset'))[1]['state'], 'done')   # M1-10
        view = self.state()[1]['attempt']
        self.assertEqual(view['steps']['verify'], 'untested')                   # re-check needed
        self.assertEqual(view['completed_at'], completed_at)                     # history kept
        self.assertEqual(self.job('alice', self.act('stop'))[1]['state'], 'done')
        self.assertIsNone(self.state()[1]['attempt'])
        with lesson_progress.connect(immediate=False) as c:
            self.assertEqual(c.execute('SELECT completions FROM lesson_summary').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT count(*) FROM completion_events').fetchone()[0], 1)

    def test_stale_and_cancelled_results_do_not_count(self):
        self.act('start')
        self.client.check_result = {'status': 'pass', 'items': []}
        self.client.check_generation = 0                     # executed before the start
        stale = self.job('alice', self.act('check'))[1]
        self.assertTrue(stale['result']['progress']['stale'])
        self.client.check_generation = None
        self.client.check_result = {'status': 'cancelled', 'items': []}
        cancelled = self.job('alice', self.act('check'))[1]
        self.assertTrue(cancelled['result']['progress']['stale'])
        view = self.state()[1]['attempt']
        self.assertEqual((view['steps']['verify'], view['completed_at']), ('untested', None))

    def test_late_answer_never_moves_generation_back(self):
        self.act('start')
        self.act('reset')
        attempt = lesson_progress.current_attempt('alice', 'vm-alice')
        self.assertEqual(attempt['generation'], 2)
        lesson_progress.sync('alice', 'vm-alice', dict(ok=True, state='running', lesson=LESSON,
                                                        observed=dict(incarnation=INC, generation=1)))
        self.assertEqual(lesson_progress.current_attempt('alice', 'vm-alice')['generation'], 2)

    def test_conflict_is_reported_not_retried(self):
        self.act('start')
        old = dict(incarnation=INC, generation=0)
        result = self.job('alice', self.act('reset', expected=old))[1]
        self.assertEqual((result['state'], result['http_status'], result['result']['error']), ('failed', 409, 'conflict'))
        self.assertEqual([c['action'] for c in self.client.calls], ['start', 'reset'])

    def test_runner_missing_is_410_and_queues_nothing(self):
        self.client.missing.add('alice')
        self.assertEqual(self.state()[0], 410)
        code, body = self.act('start')
        self.assertEqual((code, body['error']), (410, 'runner_missing'))
        self.assertEqual(self.client.calls, [])

    def test_busy_and_merging_rules(self):
        self.pool.defer = True
        change = self.act('start')
        self.assertEqual(change[0], 202)
        self.assertEqual(self.act('reset')[1]['error'], 'busy')
        self.assertEqual(self.act('check')[1]['error'], 'busy')
        self.pool.drain()
        first = self.act('check')
        same = self.act('check')
        self.assertEqual(same[1]['job_id'], first[1]['job_id'])                # tabs share a running check
        self.pool.drain()

    def test_hints_open_in_order(self):
        self.assertEqual(self.post('alice', 'lesson/network/12/step/verify/hint', {'n': 1})[1]['error'], 'no_attempt')
        self.act('start')
        self.assertEqual(self.post('alice', 'lesson/network/12/step/verify/hint', {'n': 2})[0], 409)
        code, body = self.post('alice', 'lesson/network/12/step/verify/hint', {'n': 1})
        self.assertEqual(code, 200)
        self.assertEqual(body['text'], lessons.CATALOG['lessons'][LESSON]['steps'][1]['hints'][0])
        self.assertEqual(self.post('alice', 'lesson/network/12/step/verify/hint', {'n': 2})[0], 200)
        self.assertEqual(self.post('alice', 'lesson/network/12/step/verify/hint', {'n': 9})[0], 400)
        self.assertEqual(self.state()[1]['attempt']['hints_opened'], {'verify': [1, 2]})

    def test_solve_marks_help_and_assisted_completion(self):
        self.act('start')
        job = self.job('alice', self.act('solve'))[1]
        self.assertEqual(job['state'], 'done')
        view = self.state()[1]['attempt']
        self.assertTrue(view['assisted'])
        self.assertEqual(view['steps']['verify'], 'untested')            # solve bumped the generation
        self.client.check_result = {'status': 'pass', 'items': []}
        self.assertTrue(self.job('alice', self.act('check'))[1]['result']['progress']['completed_now'])
        with lesson_progress.connect(immediate=False) as c:
            self.assertEqual(c.execute('SELECT assisted FROM completion_events').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT assisted_completions FROM lesson_summary').fetchone()[0], 1)

    def test_solve_after_completion_keeps_the_unassisted_record(self):
        self.act('start')
        self.client.check_result = {'status': 'pass', 'items': []}
        self.job('alice', self.act('check'))
        self.job('alice', self.act('solve'))
        with lesson_progress.connect(immediate=False) as c:
            self.assertEqual(c.execute('SELECT assisted FROM completion_events').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT assisted FROM attempts').fetchone()[0], 0)

    def test_terminal_started_lesson_is_adopted_without_a_start(self):
        self.client.env.update(state='running', lesson=LESSON, generation=4)
        view = self.state()[1]['attempt']
        self.assertEqual((view['origin'], view['generation']), ('adopted_cli', 4))
        self.assertEqual(self.client.calls, [])                          # nothing was (re)deployed
        self.client.check_result = {'status': 'pass', 'items': []}
        self.assertTrue(self.job('alice', self.act('check'))[1]['result']['progress']['completed_now'])
        self.client.env.update(lesson='network/03', generation=5)        # terminal switched lessons
        view = self.state()[1]['attempt']
        self.assertEqual((view['lesson_id'], view['origin']), ('network/03', 'adopted_cli'))
        with lesson_progress.connect(immediate=False) as c:
            self.assertEqual(c.execute("SELECT end_reason FROM attempts WHERE lesson_id=?", (LESSON,)).fetchone()[0],
                             'superseded')

    def test_public_lesson_reports_solvability(self):
        self.assertTrue(lessons.handle_get('alice', ['lesson', 'network', '12'])[1]['lesson']['solvable'])
        shell = lessons.handle_get('alice', ['lesson', 'example-shell', 'permissions'])[1]['lesson']
        self.assertTrue(shell['solvable'])
        self.assertIn('파일 권한', shell['steps'][0]['html'])            # format-2 read text comes from the catalog

    def test_tutor_uses_registered_probes_and_never_records_checks(self):
        self.act('start')
        ctx = lessons.tutor_context('alice')
        self.assertEqual((ctx['chapter'], ctx['lesson_id'], ctx['step']), ('12', LESSON, 'verify'))
        probe = lessons.tutor_probe('alice', ctx, 'probe-1')
        self.assertEqual(probe['output'], 'probe output')
        self.assertEqual(self.client.calls[-1]['probe'], 'probe-1')
        self.assertNotIn('argv', self.client.calls[-1])
        self.client.check_result = {'status': 'pass', 'items': []}
        result = lessons.tutor_check('alice', ctx)
        self.assertTrue(result['passed'] and not result['recorded_as_progress'])
        self.assertIsNone(self.state()[1]['attempt']['completed_at'])     # diagnostics are not progress
        self.assertFalse(lessons.tutor_changed('alice', ctx))
        self.act('reset')
        self.assertTrue(lessons.tutor_changed('alice', ctx))
        self.client.missing.add('bob')
        self.assertIsNone(lessons.tutor_context('bob'))                   # v1 VM: tutor keeps its old path

    def test_read_step_view(self):
        self.act('start')
        self.assertEqual(self.post('alice', 'lesson/network/12/step/guide/view', {})[0], 200)
        self.assertEqual(self.state()[1]['attempt']['steps']['guide'], 'done')

    def test_shared_environment_is_not_personal_achievement(self):
        store.CONFIG['auth_mode'] = 'open'
        self.act('start', account='guest')
        self.client.check_result = {'status': 'pass', 'items': []}
        self.assertTrue(self.job('guest', self.act('check', account='guest'))[1]['result']['progress']['completed_now'])
        self.assertTrue(self.state('guest')[1]['shared'])
        with lesson_progress.connect(immediate=False) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM lesson_summary').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT shared FROM completion_events').fetchone()[0], 1)
        self.assertFalse(self.state('alice')[1]['shared'])       # own VM on the same open-mode server

    def test_environment_change_elsewhere_closes_the_attempt(self):
        self.act('start')
        self.client.env.update(state='idle', lesson=None, generation=5)
        self.assertIsNone(self.state()[1]['attempt'])

    def test_unfinished_jobs_are_interrupted_at_startup(self):
        self.pool.defer = True
        started = self.act('start')
        lessons.startup()
        result = self.job('alice', started)[1]
        self.assertEqual((result['state'], result['result']['error']), ('interrupted', 'interrupted'))

    def test_malformed_tokens_are_rejected(self):
        for expected in ({'incarnation': INC}, {'incarnation': INC, 'generation': 1.0},
                         {'incarnation': 'ABCDEF01-2222-4333-8444-555555555555', 'generation': 1},
                         {'incarnation': INC, 'generation': True}, {'incarnation': INC, 'generation': -1}):
            code, body = self.post('alice', 'lesson/network/12/start', dict(request_id=str(uuid.uuid4()), expected=expected))
            self.assertEqual(code, 400, expected)
        code, _ = self.post('alice', 'lesson/network/12/start', dict(request_id='x', expected=self.token()))
        self.assertEqual(code, 400)


class AgentClientTests(unittest.TestCase):
    """The real client: fixed command, ownership from the state record, v1 VMs probed rarely."""

    def setUp(self):
        self.calls = []
        store.STATE.clear()
        store.STATE.update({'users': {'alice': {'instance': 'netlab-a', 'status': 'running'},
                                      'bob': {'instance': 'netlab-b', 'status': 'stopped'}}, 'pool': []})

    def lxc(self, *args, timeout=30, stdin=None):
        self.calls.append(args)
        if args[:2] == ('config', 'get'):
            return 0, INC.encode() + b'\n', ''
        if args[:3] == ('exec', 'netlab-a', '--') and args[3] == agent_client.RUNNER_PATH:
            return 1, b'', 'Error: no such file or directory'
        if args[:4] == ('exec', 'netlab-a', '--', 'test'):
            return 1, b'', ''
        return 0, b'', ''

    def test_missing_runner_is_cached(self):
        client = agent_client.AgentClient(lxc=self.lxc)
        with self.assertRaises(agent_client.RunnerMissing):
            client.status('alice')
        probes = len(self.calls)
        with self.assertRaises(agent_client.RunnerMissing):
            client.status('alice', fresh=True)
        self.assertEqual(len(self.calls), probes)          # no new lxc exec within the window
        client.forget('netlab-a')
        with self.assertRaises(agent_client.RunnerMissing):
            client.status('alice', fresh=True)
        self.assertGreater(len(self.calls), probes)

    def test_only_owned_running_environments(self):
        client = agent_client.AgentClient(lxc=self.lxc)
        for account in ('bob', 'mallory'):
            with self.assertRaises(agent_client.AgentError) as caught:
                client.status(account)
            self.assertEqual(caught.exception.code, 'no_environment')
        with self.assertRaises(agent_client.AgentError) as caught:
            client.call('alice', {'protocol': 1, 'action': 'stop', 'request_id': 'x'})
        self.assertEqual(caught.exception.code, 'invalid_request')
        self.assertFalse(any(c[0] == 'exec' and c[1] != 'netlab-a' for c in self.calls))


class LessonRouteTests(LessonApiCase):
    """labd routes /lesson/... only after authentication, and POSTs only as same-origin JSON."""

    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location('lessons_labd_test', Path(__file__).parents[1] / 'labd.py')
        self.labd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.labd)
        store.PROXY_SECRET = 'fixture-proxy-secret'
        store.USERS.clear(); store.USERS.update({'alice': {}})
        visits = patch.object(paths, 'VISITS_PATH', self.tmp.name + '/visits.json')
        visits.start(); self.addCleanup(visits.stop)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.labd.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))

    def call(self, method, path, body=None, ctype='application/json', user='alice'):
        headers = {'X-Lab-Proxy-Auth': 'fixture-proxy-secret', 'Host': '127.0.0.1', 'Origin': 'http://127.0.0.1'}
        if user:
            headers['X-Lab-User'] = user
        if ctype:
            headers['Content-Type'] = ctype
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        return r.status, (json.loads(data or b'{}') if 'json' in r.getheader('Content-Type', '') else data)

    def test_routes(self):
        with patch.object(store.os, 'listdir', return_value=[]):
            self.assertEqual(self.call('GET', '/lesson/state', user=None)[0], 403)
            self.assertEqual(self.call('GET', '/lesson/state')[0], 200)
            body = dict(request_id=str(uuid.uuid4()), expected=self.token())
            self.assertEqual(self.call('POST', '/lesson/network/12/start', body, ctype='text/plain')[0], 415)
            code, data = self.call('POST', '/lesson/network/12/start', body)
            self.assertEqual(code, 202)
            self.assertEqual(self.call('GET', '/lesson/jobs/' + data['job_id'])[1]['state'], 'done')
            self.assertEqual(self.call('GET', '/assets/lesson.js', ctype=None)[0], 200)


if __name__ == '__main__':
    unittest.main()

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
        self.env = {'state': 'idle', 'lesson': None, 'generation': 0}
        self.check_generation = None      # override the executed generation of the next check

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
        return dict(ok=True, state=self.env['state'], lesson=self.env['lesson'],
                    observed=dict(incarnation=INC, generation=self.env['generation']),
                    bundle=self.bundle() if self.env['lesson'] else None, ready=self.env['state'] == 'running',
                    capabilities=['status', 'start', 'reset', 'stop', 'check'],
                    installed={'network': self.bundle()}, runtime=None, recovery=None, failure=None)

    def call(self, account, request):
        self.calls.append(request)
        token = dict(incarnation=INC, generation=self.env['generation'])
        if request['expected'] != token:
            return dict(ok=False, error='conflict', observed=token, executed=None, state=self.env['state'],
                        lesson=self.env['lesson'], message='conflict')
        action = request['action']
        if action == 'check':
            generation = self.check_generation if self.check_generation is not None else self.env['generation']
            return dict(ok=True, state='running', lesson=self.env['lesson'], observed=token,
                        executed=dict(incarnation=INC, generation=generation), bundle=self.bundle(),
                        result=dict(self.check_result))
        if action == 'start' and self.env['state'] == 'running' and self.env['lesson'] == request['target_lesson']:
            return dict(ok=True, noop=True, state='running', lesson=self.env['lesson'], observed=token,
                        executed=None, bundle=self.bundle())
        self.env['generation'] += 1
        if action == 'stop':
            self.env.update(state='idle', lesson=None)
        else:
            self.env.update(state='running', lesson=request.get('target_lesson') or request['lesson'])
        new = dict(incarnation=INC, generation=self.env['generation'])
        return dict(ok=True, state=self.env['state'], lesson=self.env['lesson'], observed=new, executed=new,
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

    def token(self):
        return dict(incarnation=INC, generation=self.client.env['generation'])

    def post(self, account, path, body):
        return lessons.handle_post(account, path.split('/'), body)

    def act(self, action, account='alice', request_id=None, expected=None):
        body = dict(request_id=request_id or str(uuid.uuid4()), expected=expected or self.token())
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

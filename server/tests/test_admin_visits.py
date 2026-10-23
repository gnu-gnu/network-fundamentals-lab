"""Admin console shows when each account was last seen, whatever the login path was."""
from datetime import timedelta
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

sys.path.insert(0, str(Path(__file__).parents[1]))
from netlab import paths, store, util  # noqa: E402


class VisitStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = patch.object(paths, 'VISITS_PATH', self.tmp.name + '/visits.json')
        override.start(); self.addCleanup(override.stop)
        store.VISITS.clear()
        store.USERS.clear()

    def test_first_visit_is_written_and_repeat_polls_are_throttled(self):
        store.touch_visit('alice')
        first = json.loads(Path(paths.VISITS_PATH).read_text())
        self.assertIn('alice', first)
        store.touch_visit('alice')
        self.assertEqual(json.loads(Path(paths.VISITS_PATH).read_text()), first)
        later = util.now() + timedelta(seconds=store.VISIT_WRITE_INTERVAL + 1)
        with patch.object(store, 'now', return_value=later):
            store.touch_visit('alice')
        self.assertEqual(json.loads(Path(paths.VISITS_PATH).read_text())['alice'], util.iso(later))

    def test_corrupt_file_does_not_block_startup(self):
        Path(paths.VISITS_PATH).write_text('{broken')
        store.load_visits()
        self.assertEqual(store.VISITS, {})
        store.touch_visit('bob')
        store.load_visits()
        self.assertIn('bob', store.VISITS)

    def test_last_visit_prefers_the_later_of_access_login_and_request(self):
        store.USERS['alice'] = {'last_login': '2026-10-23T01:00:00+00:00'}
        store.VISITS['alice'] = '2026-10-23T02:00:00+00:00'
        self.assertEqual(store.last_visit('alice'), '2026-10-23T02:00:00+00:00')
        store.VISITS['alice'] = '2026-10-22T02:00:00+00:00'
        self.assertEqual(store.last_visit('alice'), '2026-10-23T01:00:00+00:00')
        self.assertIsNone(store.last_visit('nobody'))


class VisitHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = patch.object(paths, 'VISITS_PATH', self.tmp.name + '/visits.json')
        override.start(); self.addCleanup(override.stop)
        spec = importlib.util.spec_from_file_location('visits_labd_test', Path(__file__).parents[1] / 'labd.py')
        self.labd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.labd)
        store.PROXY_SECRET = 'fixture-proxy-secret'
        store.USERS.clear(); store.USERS.update({'alice': {}, 'carol': {'role': 'admin'}})
        store.CONFIG.clear(); store.CONFIG.update(store.DEFAULTS)
        store.STATE.clear(); store.STATE.update({'users': {'alice': {'status': 'stopped', 'instance': 'netlab-aaaaaaaaaa'}}, 'pool': []})
        store.VISITS.clear()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.labd.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def call(self, method, path, user='alice', admin=None):
        headers = {'X-Lab-Proxy-Auth': 'fixture-proxy-secret'}
        if user:
            headers['X-Lab-User'] = user
        if admin:
            headers['X-Lab-Admin'] = admin
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request(method, path, None, headers)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def test_panel_and_status_record_a_visit_but_assets_do_not(self):
        with patch.object(store.os, 'listdir', return_value=[]):
            self.assertEqual(self.call('GET', '/assets/theme.js')[0], 200)
            self.assertNotIn('alice', store.VISITS)
            self.assertEqual(self.call('GET', '/')[0], 200)
            self.assertIn('alice', store.VISITS)
            status, body = self.call('GET', '/admin/list', user=None, admin='carol')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data['visits']['alice'], store.VISITS['alice'])
        self.assertIn('carol', data['visits'])
        self.assertEqual(data['users'][0]['last_visit'], store.VISITS['alice'])

    def test_unauthenticated_requests_leave_no_trace(self):
        with patch.object(store.os, 'listdir', return_value=[]):
            self.assertEqual(self.call('GET', '/', user=None)[0], 403)
        self.assertEqual(store.VISITS, {})


if __name__ == '__main__':
    unittest.main()

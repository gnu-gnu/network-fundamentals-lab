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
from netlab import store  # noqa: E402
import notice_board as board


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = patch.object(board, 'DB', self.tmp.name + '/notice/notice.sqlite3')
        override.start()
        self.addCleanup(override.stop)
        clock = patch.object(board.time, 'time', return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)

    def get(self, owner='alice', client='a'*32):
        return board.handle(owner, dict(client=client))

    def read(self, owner='alice', client='a'*32, notice=None):
        notice = notice if notice is not None else self.get(owner, client)['notice']['id']
        return board.handle(owner, dict(client=client, action='read', id=notice))

    def test_empty_board(self):
        self.assertEqual(self.get(), dict(ok=True, notice=None, read=False, read_at=None))
        self.assertEqual(board.status(['alice'])['readers'], [])
        with self.assertRaises(ValueError):
            self.read(notice=1)

    def test_only_latest_is_shown_and_receipts_restart(self):
        board.publish('admin', dict(title='첫 공지', body='본문'))
        self.read()
        self.assertTrue(self.get()['read'])
        board.publish('admin', dict(title='둘째 공지', body='<b>plain</b>'))
        data = self.get()
        self.assertEqual(data['notice']['title'], '둘째 공지')
        self.assertEqual(data['notice']['body'], '<b>plain</b>')
        self.assertFalse(data['read'])
        self.assertNotIn('author', data['notice'])

    def test_admin_sees_readers_and_unread_accounts(self):
        board.publish('admin', dict(title='공지', body='본문'))
        self.read('alice')
        self.read('guest', '1'*32)
        self.read('alice')  # idempotent
        status = board.status(['alice', 'bob', 'guest'])
        self.assertEqual([r['reader'] for r in status['readers']], ['alice', 'guest-111111'])
        self.assertEqual(status['unread'], ['bob'])

    def test_stale_read_is_rejected(self):
        board.publish('admin', dict(title='공지', body='본문'))
        old = self.get()['notice']['id']
        board.publish('admin', dict(title='새 공지', body='본문'))
        with self.assertRaisesRegex(ValueError, '바뀌었'):
            self.read(notice=old)

    def test_identity_comes_from_server(self):
        board.publish('admin', dict(title='공지', body='본문'))
        board.handle('alice', dict(client='a'*32, action='read', id=1, reader='bob', owner='bob'))
        self.assertEqual([r['reader'] for r in board.status([])['readers']], ['alice'])

    def test_invalid_input(self):
        for body in ([], {'action': 'delete'}):
            with self.assertRaises(ValueError):
                board.handle('alice', body)
        with self.assertRaises(ValueError):
            board.handle('guest', {'client': '../x'})
        for title, body in ((' ', 'x'), ('x'*101, 'x'), ('x', 'x'*5001), ('x', 'bad\x00')):
            with self.assertRaises(ValueError):
                board.publish('admin', dict(title=title, body=body))

    def test_retention(self):
        for i in range(board.KEEP_NOTICES + 5):
            board.publish('admin', dict(title=str(i), body='x'))
        with board.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM notices').fetchone()[0], board.KEEP_NOTICES)


class NoticeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = patch.object(board, 'DB', self.tmp.name + '/notice.sqlite3')
        override.start(); self.addCleanup(override.stop)
        spec = importlib.util.spec_from_file_location('notice_labd_test', Path(__file__).parents[1] / 'labd.py')
        self.labd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.labd)
        store.PROXY_SECRET = 'fixture-proxy-secret'
        store.USERS.clear(); store.USERS.update({'alice': {}, 'bob': {}, 'carol': {'role': 'admin'}})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.labd.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def call(self, method, path, payload=None, user='alice', admin=None, **extra):
        headers = {'Content-Type': 'application/json', 'X-Lab-Proxy-Auth': 'fixture-proxy-secret'}
        if user:
            headers['X-Lab-User'] = user
        if admin:
            headers['X-Lab-Admin'] = admin
        headers.update(extra)
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_publish_read_and_admin_view(self):
        self.assertEqual(self.call('POST', '/admin/notice', dict(title='t', body='b'), user=None)[0], 403)
        self.assertEqual(self.call('POST', '/admin/notice', dict(title='t', body='b'), user=None, admin='alice')[0], 403)
        status, data = self.call('POST', '/admin/notice', dict(title='점검 안내', body='내일 점검'), user=None, admin='carol')
        self.assertEqual(status, 200)
        status, data = self.call('POST', '/u/alice/ctl/notice', dict(client='a'*32))
        self.assertEqual((status, data['notice']['title'], data['read']), (200, '점검 안내', False))
        status, data = self.call('POST', '/u/alice/ctl/notice', dict(client='a'*32, action='read', id=data['notice']['id']))
        self.assertTrue(data['read'])
        status, data = self.call('GET', '/admin/notice', user=None, admin='carol')
        self.assertEqual([r['reader'] for r in data['readers']], ['alice'])
        self.assertEqual(data['unread'], ['bob', 'carol'])

    def test_cross_site_and_bad_requests(self):
        self.assertEqual(self.call('POST', '/u/alice/ctl/notice', dict(client='a'*32), Origin='https://evil.invalid')[0], 403)
        self.assertEqual(self.call('POST', '/admin/notice', dict(title='t', body='b'), user=None, admin='carol',
                                   **{'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.call('POST', '/u/alice/ctl/notice', dict(client='a'*32), **{'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.call('POST', '/admin/notice', dict(title='', body='b'), user=None, admin='carol')[0], 400)
        self.assertEqual(self.call('POST', '/u/alice/ctl/notice', dict(client='a'*32), user=None)[0], 403)

    def test_panel_and_assets(self):
        html = self.labd.render_panel('alice')
        for needle in ('id=noticelauncher', 'assets/notice.js', 'assets/notice.css', 'id=noticepanel'):
            self.assertIn(needle, html)
        self.assertIn('id=noticeform', self.labd.ADMIN)


if __name__ == '__main__':
    unittest.main()

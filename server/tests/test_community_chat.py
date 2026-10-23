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
import community_chat as chat


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = patch.object(chat, 'DB', self.tmp.name + '/community/chat.sqlite3')
        override.start()
        self.addCleanup(override.stop)
        clock = patch.object(chat.time, 'time', return_value=1200)
        clock.start()
        self.addCleanup(clock.stop)

    def poll(self, owner='alice', client='a'*32):
        return chat.handle(owner, dict(client=client))

    def send(self, owner='alice', client='a'*32, request='1'*32, text='안녕하세요', **extra):
        return chat.handle(owner, dict(client=client, action='send', request=request, text=text, **extra))

    def test_identity_is_server_owned_and_text_is_plain(self):
        self.send(author='admin', owner_override='admin', text='<img src=x onerror=alert(1)>')
        data = self.poll('bob')
        self.assertEqual(data['messages'][0]['author'], 'alice')
        self.assertEqual(data['messages'][0]['text'], '<img src=x onerror=alert(1)>')
        self.assertNotIn('client', data['messages'][0])

    def test_guest_names_and_online_tabs_are_deduplicated(self):
        self.poll('alice', 'b'*32)
        self.poll('alice', 'c'*32)
        self.assertEqual(self.poll()['online'], ['alice'])
        self.poll('guest', '1'*32)
        self.assertIn('guest-111111', self.poll('guest', '2'*32)['online'])
        with patch.object(chat.time, 'time', return_value=1291):
            self.assertEqual(self.poll('bob')['online'], ['bob'])

    def test_retry_is_idempotent_and_conflict_rejected(self):
        self.send()
        self.assertEqual(len(self.send()['messages']), 1)
        with self.assertRaises(ValueError):
            self.send(text='다른 메시지')

    def test_rate_limit_applies_across_named_user_browsers(self):
        for i in range(5):
            self.send(client=f'{i:032x}', request=f'{i:032x}')
        with self.assertRaisesRegex(ValueError, '너무 많'):
            self.send(client='f'*32, request='f'*32)
        # Rejected requests do not create a message or consume another user's quota.
        self.assertEqual(len(self.send(owner='bob')['messages']), 6)

    def test_retention_is_100_messages_and_global_rate_is_bounded(self):
        for i in range(120):
            self.send(owner='user'+str(i), request=f'{i:032x}', text=str(i))
        messages = self.poll()['messages']
        self.assertEqual(len(messages), 100)
        self.assertEqual(messages[0]['text'], '20')
        with self.assertRaisesRegex(ValueError, '너무 많'):
            self.send(owner='one-more')

    def test_invalid_input(self):
        for body in ([], {}, {'client': '../etc/passwd'}, {'client': 'a'*32, 'action': 'delete'}):
            with self.assertRaises(ValueError):
                chat.handle('alice', body)
        for text in (' ', 'x'*1001, 'bad\x00text'):
            with self.assertRaises(ValueError):
                self.send(text=text)


class CommunityHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = patch.object(chat, 'DB', self.tmp.name + '/chat.sqlite3')
        override.start(); self.addCleanup(override.stop)
        spec = importlib.util.spec_from_file_location('community_labd_test', Path(__file__).parents[1] / 'labd.py')
        self.labd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.labd)
        store.PROXY_SECRET = 'fixture-proxy-secret'
        store.USERS.clear(); store.USERS.update({'alice': {}, 'bob': {}})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self.labd.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)

    def request(self, payload=None, authenticated=True, **extra):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers.update({'X-Lab-Proxy-Auth': 'fixture-proxy-secret', 'X-Lab-User': 'alice'})
        headers.update(extra)
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        conn.request('POST', '/community', json.dumps(payload or {'client': 'a'*32}), headers)
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_authentication_and_shared_identity(self):
        self.assertEqual(self.request(authenticated=False)[0], 403)
        status, data = self.request(dict(client='a'*32, action='send', request='1'*32, text='hello', author='admin'))
        self.assertEqual(status, 200)
        self.assertEqual(data['messages'][0]['author'], 'alice')

    def test_cross_origin_and_non_json_requests_are_rejected(self):
        self.assertEqual(self.request(Origin='https://other.invalid')[0], 403)
        self.assertEqual(self.request(**{'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(self.request(**{'Sec-Fetch-Site': 'same-site'})[0], 403)
        self.assertEqual(self.request(**{'Content-Type': 'text/plain'})[0], 415)

    def test_same_origin_lan_proxy_without_host_port(self):
        self.assertEqual(self.request(Origin='http://127.0.0.1:18088', Host='127.0.0.1',
                                      **{'Sec-Fetch-Site': 'same-origin'})[0], 200)

    def test_chat_does_not_require_a_running_vm(self):
        store.STATE.clear(); store.STATE.update({'users': {}, 'pool': []})
        self.assertEqual(self.request()[0], 200)

    def test_socket_grant_uses_authenticated_identity_and_same_origin(self):
        import community_auth
        status,data=self.request(dict(action='ticket',client='a'*32,owner='admin'),Origin='http://127.0.0.1')
        self.assertEqual(status,200)
        claims=community_auth.verify('fixture-proxy-secret',data['ticket'],'http://127.0.0.1')
        self.assertEqual(claims['owner'],'alice')
        self.assertFalse(claims['admin'])
        self.assertEqual(self.request(dict(action='ticket',client='a'*32),authenticated=False)[0],403)
        self.assertEqual(self.request(dict(action='ticket',client='a'*32),Origin='https://evil.example')[0],403)

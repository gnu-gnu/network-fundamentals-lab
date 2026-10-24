import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from netlab import nginx, paths, store  # noqa: E402


class OpenModeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('labd_open_test', Path(__file__).parents[1] / 'labd.py')
        self.labd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.labd)
        # store 는 프로세스 전역이므로 테스트마다 초기화한다.
        store.USERS.clear(); store.USERS.update({'alice': {'role': 'user'}})
        store.CONFIG.clear(); store.CONFIG.update(store.DEFAULTS)
        store.STATE.clear(); store.STATE.update({'users': {}, 'pool': []})

    def test_legacy_default_stays_protected(self):
        with patch.object(store.os, 'listdir', return_value=[]):
            self.assertNotIn('guest', self.labd.accounts())

    def test_shared_vm_notice_only_for_guest(self):
        guest = self.labd.render_panel('guest')
        named = self.labd.render_panel('alice')
        self.assertIn('실습 VM이 다른 방문자와 공유됩니다', guest)
        self.assertNotIn('실습 VM이 다른 방문자와 공유됩니다', named)
        self.assertIn('질문과 실험 결과를 함께 살펴봅니다.', named)

    def test_panel_template_has_no_unrendered_placeholders(self):
        panel = self.labd.render_panel('alice')
        self.assertNotIn('__USER__', panel)
        self.assertNotIn('__TUTOR_SHARED_NOTICE__', panel)
        self.assertIn('<title>alice — 실습 환경</title>', panel)
        self.assertNotIn('{{', panel)

    def test_theme_toggle_is_accessible_and_initialized_before_styles(self):
        panel = self.labd.render_panel('alice')
        self.assertIn('id=themetoggle', panel)
        self.assertIn('aria-label="다크 테마" aria-pressed=false', panel)
        self.assertLess(panel.index('assets/theme.js'), panel.index('<style>'))

    def test_terminal_palette_options_and_early_initialization(self):
        panel = self.labd.render_panel('alice')
        for theme in ('current', 'solarized-dark', 'solarized-light', 'ubuntu'):
            self.assertIn('option value=' + theme, panel)
        self.assertIn('for=terminaltheme', panel)
        self.assertLess(panel.index('assets/terminal-theme.js'), panel.index('id=termframe'))

    def test_open_routes_only_shared_guest_and_strip_admin_header(self):
        store.CONFIG['auth_mode'] = 'open'
        store.STATE['users'] = {'guest': {'status': 'running', 'ip': '10.210.0.20'}}
        writes = {}
        with patch.object(store.os, 'listdir', return_value=[]), \
                patch.object(nginx, 'write_atomic', side_effect=lambda p, text, mode: writes.update({p: text})), \
                patch.object(nginx.shutil, 'chown'), patch.object(nginx.subprocess, 'run') as run, \
                patch.object(nginx.time, 'sleep'):
            run.return_value.returncode = 0
            self.assertTrue(self.labd.regen_routing())
        routes = writes[paths.NGINX_LAN]
        self.assertIn('/u/guest/ctl/', routes)
        self.assertIn('http://10.210.0.20:7681/', routes)
        self.assertNotIn('/u/alice/', routes)
        self.assertNotIn('auth_basic', routes)
        self.assertNotIn('auth_request', routes)
        self.assertIn('proxy_set_header X-Lab-Admin "";', routes)

    def test_open_template_blocks_admin_and_fixes_identity(self):
        template = (Path(__file__).parents[1] / 'nginx-open.conf').read_text()
        self.assertIn('listen 8088 default_server;', template)
        self.assertIn('location /admin { return 403; }', template)
        self.assertIn('proxy_set_header X-Lab-User "guest";', template)

    def test_tunnel_authorization_uses_static_location_owner(self):
        routes = nginx.render('tunnel', ['alice'])
        self.assertIn('auth_request /_netlab_access_alice;', routes)
        self.assertIn('location = /_netlab_access_alice {', routes)
        self.assertIn('proxy_set_header X-Lab-Target-User "alice";', routes)
        self.assertNotIn('/_access?target=', routes)
        template = (Path(__file__).parents[1] / 'nginx-netlab.conf').read_text()
        self.assertIn('proxy_set_header X-Lab-Target-User "";', template)
        self.assertNotIn('$arg_target', template)
        self.assertEqual(template.count('proxy_set_header X-Lab-Auth-Scheme target;'), 2)
        self.assertNotIn('X-Original-URI', template)

    def test_outdated_site_fails_closed_instead_of_skipping_the_owner_check(self):
        handler = self.labd.Handler.__new__(self.labd.Handler)
        handler.headers = {'Cf-Access-Jwt-Assertion': 'signed', 'X-Original-URI': '/u/alice/term/'}
        handler._send = unittest.mock.Mock(return_value='denied')
        with patch.object(self.labd, 'verify_access_jwt', return_value='alice@example.test'), \
                patch.object(self.labd, 'lookup_or_create',
                             return_value=('alice', store.USERS['alice'])):
            self.assertEqual(handler._access_check(), 'denied')
        handler._send.assert_called_once_with(403, '', 'text/plain')

    def test_access_rejects_trusted_target_mismatch_without_parsing_raw_uri(self):
        handler = self.labd.Handler.__new__(self.labd.Handler)
        handler.headers = {
            'Cf-Access-Jwt-Assertion': 'signed',
            'X-Lab-Auth-Scheme': 'target',
            'X-Lab-Target-User': 'bob',
            'X-Original-URI': '/u/alice/../bob/term/',
        }
        handler._send = unittest.mock.Mock(return_value='denied')
        with patch.object(self.labd, 'verify_access_jwt', return_value='alice@example.test'), \
                patch.object(self.labd, 'lookup_or_create',
                             return_value=('alice', store.USERS['alice'])):
            self.assertEqual(handler._access_check(), 'denied')
        handler._send.assert_called_once_with(403, '', 'text/plain')


if __name__ == '__main__':
    unittest.main()

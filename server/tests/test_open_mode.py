import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


class OpenModeTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('labd_open_test', Path(__file__).parents[1] / 'labd.py')
        self.labd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.labd)
        self.labd.USERS = {'alice': {'role': 'user'}}

    def test_legacy_default_stays_protected(self):
        with patch.object(self.labd.os, 'listdir', return_value=[]):
            self.assertNotIn('guest', self.labd.accounts())

    def test_shared_vm_notice_only_for_guest(self):
        guest = self.labd.render_panel('guest')
        named = self.labd.render_panel('alice')
        self.assertIn('실습 VM이 다른 방문자와 공유됩니다', guest)
        self.assertNotIn('실습 VM이 다른 방문자와 공유됩니다', named)
        self.assertIn('질문과 실험 결과를 함께 살펴봅니다.', named)

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
        labd = self.labd
        labd.CONFIG['auth_mode'] = 'open'
        labd.STATE = {'users': {'guest': {'status': 'running', 'ip': '10.210.0.20'}}, 'pool': []}
        writes = {}
        with patch.object(labd.os, 'listdir', return_value=[]), \
                patch.object(labd, 'write_atomic', side_effect=lambda p, text, mode: writes.update({p: text})), \
                patch.object(labd.shutil, 'chown'), patch.object(labd.subprocess, 'run') as run, \
                patch.object(labd.time, 'sleep'):
            run.return_value.returncode = 0
            self.assertTrue(labd.regen_routing())
        routes = writes[labd.NGINX_LAN]
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


if __name__ == '__main__':
    unittest.main()

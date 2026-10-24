import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from netlab import access, nginx, paths, store  # noqa: E402

SPEC = importlib.util.spec_from_file_location('deploy', Path(__file__).parents[1] / 'deploy.py')
deploy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(deploy)


class SourceTests(unittest.TestCase):
    def test_source_and_secret_placeholder(self):
        deploy.validate_source()

    def test_placeholder_rejects_literal_secret(self):
        original = Path.read_text

        def read(p, *args, **kwargs):
            text = original(p, *args, **kwargs)
            if p.name == 'nginx-netlab.conf':
                return text.replace('__NETLAB_PROXY_SECRET__', 'accidental-live-secret')
            return text

        with patch.object(Path, 'read_text', read):
            with self.assertRaisesRegex(RuntimeError, 'placeholders only'):
                deploy.validate_source()

    def test_live_site_must_use_target_based_auth_request(self):
        template = (deploy.SOURCE / 'nginx-netlab.conf').read_text()
        deploy.check_site(template)
        deploy.check_site((deploy.SOURCE / 'nginx-open.conf').read_text())
        with self.assertRaisesRegex(RuntimeError, 'predates'):
            deploy.check_site(template.replace('X-Lab-Auth-Scheme target', 'X-Lab-Auth-Scheme legacy'))
        # A rolling update may leave the old header next to the new ones.
        deploy.check_site(template.replace('proxy_set_header X-Lab-Auth-Scheme target;',
                                           'proxy_set_header X-Lab-Auth-Scheme target;\n        proxy_set_header X-Original-URI $request_uri;'))

    def test_deploy_list_covers_every_package_module_and_template(self):
        for path in sorted((deploy.SOURCE / 'netlab').glob('*.py')) + sorted((deploy.SOURCE / 'templates').glob('*.html')):
            self.assertIn(str(path.relative_to(deploy.SOURCE)), deploy.FILES)
        self.assertNotIn('deploy.py', deploy.FILES)
        self.assertFalse(any(f.startswith('tests/') for f in deploy.FILES))

    def test_access_new_accounts_never_become_admin(self):
        store.USERS.clear()
        store.USERS_LOADED = True
        with patch.object(store, 'save_users'), patch.object(nginx, 'regen_routing'), \
                patch.object(access.os.path, 'exists', return_value=False):
            _, record = access.lookup_or_create('new@example.org')
        self.assertEqual(record['role'], 'user')

    def test_access_corrupt_table_denies_login(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'users.json'
            p.write_text('{broken')
            with patch.object(paths, 'USERS_PATH', str(p)):
                store.load_users()
            self.assertEqual(access.lookup_or_create('new@example.org'), (None, None))
        store.USERS_LOADED = True


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dest = root / 'code'
        self.dest.mkdir()
        (self.dest / 'labd.py').write_text('old code')
        self.unit = root / 'labd.service'
        self.unit.write_text('old unit')
        self.bins = root / 'bin'
        self.bins.mkdir()
        for name in deploy.CLI:
            (self.bins / name).write_text('old ' + name)
        self.backups = root / 'backups'
        original_path = Path

        def mapped(value):
            return {'/var/backups/netlab': self.backups,
                    '/usr/local/bin': self.bins}.get(str(value), original_path(value))

        for mocker in (patch.object(deploy, 'DEST', self.dest),
                       patch.object(deploy, 'UNIT', self.unit),
                       patch.object(deploy, 'Path', side_effect=mapped),
                       patch.object(deploy, 'preflight'),
                       patch.object(deploy, 'check_user_routes'),
                       patch.object(deploy, 'ensure_community_runtime'),
                       patch.object(deploy, 'run', return_value='active')):
            mocker.start()
            self.addCleanup(mocker.stop)

    def install(self):
        (self.dest / 'labd.py').write_text('new code')
        self.unit.write_text('new unit')
        for name in deploy.CLI:
            (self.bins / name).write_text('new ' + name)

    def test_failure_restores_old_executables(self):
        with patch.object(deploy, 'install_code', side_effect=self.install), \
                patch.object(deploy, 'health', side_effect=RuntimeError('bad health')):
            with self.assertRaisesRegex(RuntimeError, 'bad health'):
                deploy.deploy()
        self.assertEqual((self.dest / 'labd.py').read_text(), 'old code')
        self.assertEqual(self.unit.read_text(), 'old unit')
        for name in deploy.CLI:
            self.assertEqual((self.bins / name).read_text(), 'old ' + name)

    def test_success_keeps_backup_and_manifest(self):
        with patch.object(deploy, 'install_code', side_effect=self.install), \
                patch.object(deploy, 'health'), patch.object(deploy, 'FILES', ('labd.py',)):
            deploy.deploy()
        backup = next(self.backups.iterdir())
        self.assertEqual((backup / 'code/labd.py').read_text(), 'old code')
        self.assertEqual((self.dest / 'labd.py').read_text(), 'new code')
        self.assertIn('labd.py', json.loads((self.dest / 'release-manifest.json').read_text()))

    def test_broken_user_route_rolls_back(self):
        with patch.object(deploy, 'install_code', side_effect=self.install), patch.object(deploy, 'health'), \
                patch.object(deploy, 'check_user_routes', side_effect=RuntimeError('User route check failed')):
            with self.assertRaisesRegex(RuntimeError, 'User route'):
                deploy.deploy()
        self.assertEqual((self.dest / 'labd.py').read_text(), 'old code')
        self.assertFalse((self.dest / 'release-manifest.json').exists())


class RunningSourceTests(unittest.TestCase):
    """Deploying must not silently drop code that runs in production but is not in this source."""

    SOURCE = {'commit': 'b' * 40, 'branch': 'main', 'dirty': False}

    def running(self, **extra):
        return dict({'commit': 'a' * 40, 'branch': 'main', 'dirty': False, 'deployed': '2026-10-24T03:17:12Z'}, **extra)

    def test_uncommitted_running_code_blocks_deploy(self):
        with patch.object(deploy, 'source_contains', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'uncommitted'):
                deploy.check_running_source(self.running(dirty=True), self.SOURCE)

    def test_running_commit_must_be_in_source(self):
        with patch.object(deploy, 'source_contains', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'not in this source'):
                deploy.check_running_source(self.running(), self.SOURCE)
        with patch.object(deploy, 'source_contains', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 're-stamp'):
                deploy.check_running_source(self.running(), self.SOURCE)
        with patch.object(deploy, 'source_contains', return_value=True):
            deploy.check_running_source(self.running(), self.SOURCE)

    def test_source_itself_must_be_committed(self):
        for source in (None, dict(self.SOURCE, dirty=True)):
            with self.assertRaisesRegex(RuntimeError, 'uncommitted'):
                deploy.check_running_source(self.running(), source)

    def test_install_without_source_record_is_allowed(self):
        deploy.check_running_source(None, self.SOURCE)

    def test_tar_copy_uses_stamped_ancestors(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, '.release-source.json').write_text(json.dumps({'commit': 'b' * 40, 'ancestors': ['b' * 40, 'a' * 40]}))
            with patch.object(deploy, 'SOURCE', Path(d)):
                self.assertTrue(deploy.source_contains('a' * 40))
                self.assertFalse(deploy.source_contains('c' * 40))
            Path(d, '.release-source.json').write_text(json.dumps({'commit': 'b' * 40}))
            with patch.object(deploy, 'SOURCE', Path(d)):
                self.assertIsNone(deploy.source_contains('a' * 40))

    def test_git_checkout_answers_from_history(self):
        head = deploy.run('git', '-C', str(deploy.SOURCE), 'rev-parse', 'HEAD')
        first = deploy.run('git', '-C', str(deploy.SOURCE), 'rev-list', '--max-parents=0', 'HEAD').split()[0]
        self.assertTrue(deploy.source_contains(head))
        self.assertTrue(deploy.source_contains(first))
        self.assertFalse(deploy.source_contains('0' * 40))


class UserRouteTests(unittest.TestCase):
    def test_targets_follow_the_live_site_layout(self):
        tunnel = deploy.user_route_targets((deploy.SOURCE / 'nginx-netlab.conf').read_text())
        self.assertEqual(tunnel, [('127.0.0.1:8088', '/etc/nginx/netlab.d/tunnel-users.conf'),
                                  ('127.0.0.2:8088', '/etc/nginx/netlab.d/lan-users.conf')])
        self.assertEqual(deploy.user_route_targets((deploy.SOURCE / 'nginx-open.conf').read_text()),
                         [('127.0.0.1:8088', '/etc/nginx/netlab.d/lan-users.conf')])

    def probe(self, status, mode='tunnel'):
        """Serve one fixed status for every request and run the check against a site pointing at it."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                self.send_response(status)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        d = Path(self.enterContext(tempfile.TemporaryDirectory()))
        snippet = d / f'{mode}-users.conf'
        user = 'guest' if mode == 'open' else 'alice'
        (d / user).write_text('htpasswd entry')             # lan routes only accounts with a password
        with patch.dict(store.STATE, {'users': {}, 'pool': []}), patch.object(paths, 'HTPASSWD_DIR', str(d)):
            snippet.write_text(nginx.render(mode, [user]))
        site = d / 'site'
        site.write_text(f'server {{\n    listen 127.0.0.1:{server.server_port};\n    include {snippet};\n}}\n')
        deploy.check_user_routes(site, attempts=2)
        return seen

    def test_auth_challenge_or_open_panel_passes(self):
        self.assertEqual(self.probe(401), ['/u/alice/ctl/status'])
        self.assertEqual(self.probe(401, 'lan'), ['/u/alice/ctl/status'])
        self.assertEqual(self.probe(200, 'open'), ['/u/guest/ctl/status'])

    def test_anything_but_the_expected_answer_fails(self):
        # A broken auth subrequest surfaces as 500 (missing location) or nginx's own 403 (unreadable root).
        for status in (500, 502, 404, 403, 200):
            with self.assertRaisesRegex(RuntimeError, f'-> {status} \\(expected 401\\)'):
                self.probe(status)
        with self.assertRaisesRegex(RuntimeError, 'expected 200'):
            self.probe(401, 'open')


if __name__ == '__main__':
    unittest.main()

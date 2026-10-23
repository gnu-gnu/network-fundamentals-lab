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
            deploy.check_site(template.replace('X-Lab-Target-User $arg_target', 'X-Original-URI $request_uri'))
        with self.assertRaisesRegex(RuntimeError, 'predates'):
            deploy.check_site(template.replace('X-Lab-Auth-Scheme target', 'X-Lab-Auth-Scheme legacy'))

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


if __name__ == '__main__':
    unittest.main()

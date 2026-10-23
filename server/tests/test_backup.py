import argparse
import importlib.machinery
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('backup', str(Path(__file__).parents[1] / 'netlab-backup'))
spec = importlib.util.spec_from_loader(loader.name, loader)
backup = importlib.util.module_from_spec(spec)
loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, name, kind=tarfile.REGTYPE):
        archive = self.root / 'test.tar'
        with tarfile.open(archive, 'w') as tar:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = '/etc/passwd' if kind == tarfile.SYMTYPE else ''
            tar.addfile(info, io.BytesIO(b''))
        return archive

    def test_reject_traversal_absolute_symlink_device(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('/etc/evil', tarfile.REGTYPE),
                           ('files/link', tarfile.SYMTYPE), ('files/dev', tarfile.CHRTYPE)]:
            with self.subTest(name=name):
                with self.assertRaises(RuntimeError):
                    backup.unpack(self.archive(name, kind), self.root / 'out')

    def test_reject_non_managed_names_and_duplicates(self):
        for names in [['production-vm'], ['netlab-abcdef0123'] * 2]:
            with self.assertRaises(RuntimeError):
                backup.instances({'users': {}, 'pool': [{'instance': n} for n in names]})

    def test_checksum_mismatch(self):
        folder = self.root / 'payload'
        folder.mkdir()
        (folder / 'manifest.json').write_text(json.dumps({'version': 1, 'mode': 'config', 'sha256': {'files/evil': 'x'}}))
        archive = self.root / 'data.tar'
        with tarfile.open(archive, 'w') as tar:
            tar.add(folder / 'manifest.json', arcname='manifest.json')
        with self.assertRaisesRegex(RuntimeError, 'checksum'):
            backup.unpack(archive, self.root / 'out')

    def test_encryption_roundtrip_and_wrong_password(self):
        home = self.root / 'gnupg'
        home.mkdir(mode=0o700)
        source = self.root / 'plain'
        source.write_text('secret fixture')
        backup.crypt(source, self.root / 'encrypted', 'long-test-passphrase', home)
        self.assertNotIn(b'secret fixture', (self.root / 'encrypted').read_bytes())
        backup.crypt(self.root / 'encrypted', self.root / 'restored', 'long-test-passphrase', home, True)
        self.assertEqual((self.root / 'restored').read_text(), 'secret fixture')
        with self.assertRaises(RuntimeError):
            backup.crypt(self.root / 'encrypted', self.root / 'wrong', 'wrong-test-passphrase', home, True)

    def test_restore_rejects_populated_target(self):
        with patch.object(backup, 'read', side_effect=[{}, {'users': {'guest': {}}, 'pool': []}]), \
                patch.object(backup, 'infrastructure', return_value={}), \
                patch.object(backup, 'digest', return_value='code'):
            with self.assertRaisesRegex(RuntimeError, 'not empty'):
                backup.preflight_restore(self.root, {'infrastructure': {}, 'code_sha256': 'code'})

    def test_restore_rejects_incompatible_infrastructure(self):
        with patch.object(backup, 'read', return_value={}), \
                patch.object(backup, 'infrastructure', return_value={'network': 'different'}):
            with self.assertRaisesRegex(RuntimeError, 'mismatch'):
                backup.preflight_restore(self.root, {'infrastructure': {}})

    def test_restore_preserves_existing_tutor_conversations(self):
        with patch.object(backup, 'read', side_effect=[{}, {'users': {}, 'pool': []}, {}]), \
                patch.object(backup, 'infrastructure', return_value={}), \
                patch.object(backup, 'digest', return_value='code'), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'exists', autospec=True,
                             side_effect=lambda path: str(path) == '/var/lib/netlab/tutor'), \
                patch.object(Path, 'iterdir', autospec=True,
                             side_effect=lambda path: iter([Path('chat.sqlite3')]) if str(path) == '/var/lib/netlab/tutor' else iter([])):
            with self.assertRaisesRegex(RuntimeError, 'tutor conversations'):
                backup.preflight_restore(self.root, {'infrastructure': {}, 'code_sha256': 'code'})

    def test_restore_preserves_existing_community_chat(self):
        with patch.object(backup, 'read', side_effect=[{}, {'users': {}, 'pool': []}, {}]), \
                patch.object(backup, 'infrastructure', return_value={}), \
                patch.object(backup, 'digest', return_value='code'), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'exists', autospec=True,
                             side_effect=lambda path: str(path) == '/var/lib/netlab/community'), \
                patch.object(Path, 'iterdir', autospec=True,
                             side_effect=lambda path: iter([Path('chat.sqlite3')]) if str(path) == '/var/lib/netlab/community' else iter([])):
            with self.assertRaisesRegex(RuntimeError, 'community chat'):
                backup.preflight_restore(self.root, {'infrastructure': {}, 'code_sha256': 'code'})

    def test_restore_rejects_image_alias_conflict(self):
        manifest = {'mode': 'full', 'infrastructure': {}, 'code_sha256': 'code',
                    'image_alias': 'netlab', 'image_fingerprint': 'expected'}
        with patch.object(backup, 'read', side_effect=[{}, {'users': {}, 'pool': []}, {}]), \
                patch.object(backup, 'infrastructure', return_value={}), \
                patch.object(backup, 'digest', return_value='code'), \
                patch.object(Path, 'exists', return_value=False), \
                patch.object(Path, 'is_symlink', return_value=False), \
                patch.object(Path, 'iterdir', return_value=iter([])), \
                patch.object(backup, 'query', side_effect=[[], [{'name': 'netlab', 'target': 'different'}]]):
            with self.assertRaisesRegex(RuntimeError, 'alias conflicts'):
                backup.preflight_restore(self.root, manifest)

    def test_copy_rejects_symlink(self):
        (self.root / 'link').symlink_to('/etc/passwd')
        with self.assertRaises(RuntimeError):
            backup.copy_tree(self.root / 'link', self.root / 'copy')

    def test_copy_rejects_destination_symlink(self):
        (self.root / 'source').write_text('new')
        (self.root / 'original').write_text('keep')
        (self.root / 'link').symlink_to(self.root / 'original')
        with self.assertRaises(RuntimeError):
            backup.copy_tree(self.root / 'source', self.root / 'link')
        self.assertEqual((self.root / 'original').read_text(), 'keep')

    def test_dry_run_has_no_service_or_file_mutations(self):
        with patch.object(backup, 'preflight_restore', return_value={}), \
                patch.object(backup, 'run') as run, patch.object(backup, 'copy_tree') as copy:
            backup.restore(argparse.Namespace(dry_run=True), self.root, {})
        run.assert_not_called()
        copy.assert_not_called()

    def test_failed_vm_export_restarts_source(self):
        name = 'netlab-abcdef0123'
        calls = []
        def run(*args):
            calls.append(args)
            if args[:2] == ('lxc', 'export'):
                raise RuntimeError('export failed')
            return ''
        args = argparse.Namespace(output=str(self.root / 'backup.gpg'), mode='full')
        temp = self.root / 'temp'
        temp.mkdir()
        state = {'users': {'guest': {'instance': name, 'status': 'running'}}, 'pool': []}
        with patch.object(backup, 'read', side_effect=[{}, state]), \
                patch.object(backup, 'infrastructure', return_value={}), \
                patch.object(backup.subprocess, 'run') as proc, \
                patch.object(backup, 'run', side_effect=run), \
                patch.object(backup, 'query', side_effect=[{'type': 'virtual-machine', 'status': 'Running'}, {'status': 'Stopped'}]):
            proc.return_value.returncode = 0
            with self.assertRaisesRegex(RuntimeError, 'export failed'):
                backup.create(args, temp, 'password123456')
        self.assertIn(('lxc', 'start', name), calls)
        self.assertEqual(calls[-1], ('systemctl', 'start', 'labd'))
        self.assertFalse((self.root / 'backup.gpg').exists())

import argparse
import importlib.util
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SERVER = Path(__file__).parents[1]
sys.path.insert(0, str(SERVER))
import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_curriculum_archive_excludes_server_and_git(self):
        with tempfile.TemporaryDirectory() as d:
            archive = Path(d) / 'labs.tar'
            bootstrap.pack_labs(archive)
            with tarfile.open(archive) as f:
                names = f.getnames()
        self.assertIn('01-l2-arp/01-l2-arp.clab.yml', names)
        self.assertIn('17-dynamic-routing/bgp.clab.yml', names)
        self.assertFalse(any(n.startswith(('server/', '.git/', 'assets/')) for n in names))

    def test_network_conflict_does_not_overwrite(self):
        calls = []

        def run(*args, **kwargs):
            calls.append(args)
            if args[-1] == '/1.0/storage-pools/labpool':
                return json.dumps({'driver': 'zfs'})
            if args[-1] == '/1.0/networks/lxdbr0':
                return json.dumps({'config': {'ipv4.address': '192.168.1.1/24'}})
            return ''

        args = argparse.Namespace(storage_size='100GiB', subnet='10.200.0.1/24')
        with patch.object(bootstrap, 'exists', return_value=True), \
                patch.object(bootstrap, 'run', side_effect=run), \
                patch.object(bootstrap.subprocess, 'run') as proc:
            proc.return_value.returncode = 0
            with self.assertRaisesRegex(RuntimeError, 'refusing to change'):
                bootstrap.ensure_lxd(args)
        self.assertFalse(any('set' in c or 'create' in c or 'delete' in c for c in calls))

    def test_existing_image_is_reused(self):
        with patch.object(bootstrap, 'exists', return_value=True), \
                patch.object(bootstrap, 'run', side_effect=[
                    json.dumps({'target': 'fingerprint'}),
                    json.dumps({'type': 'virtual-machine'})]) as run:
            bootstrap.build_image(argparse.Namespace())
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[1] == 'query' for call in run.call_args_list))

    def test_container_image_not_accepted_as_vm(self):
        with patch.object(bootstrap, 'exists', return_value=True), \
                patch.object(bootstrap, 'run', side_effect=[
                    json.dumps({'target': 'fingerprint'}),
                    json.dumps({'type': 'container'})]):
            with self.assertRaisesRegex(RuntimeError, 'not a VM'):
                bootstrap.build_image(argparse.Namespace())

    def test_download_checksum_rejects_bad_binary(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            def run(*args, **kwargs):
                if '-o' in args:
                    (d / 'containerlab.deb').write_bytes(b'wrong content')
                    return ''
                return '0' * 64 + '  containerlab_0.74.2_linux_amd64.deb\n'
            with patch.object(bootstrap, 'run', side_effect=run):
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    bootstrap.download_deb('0.74.2', d)

    def test_new_firewall_does_not_flush_docker_or_global_chains(self):
        script = (SERVER / 'netguard.sh').read_text()
        self.assertNotIn('-F DOCKER-USER', script)
        self.assertNotIn('-F FORWARD', script)
        self.assertNotIn('-F INPUT', script)
        self.assertIn('--noflush', script)


if __name__ == '__main__':
    unittest.main()

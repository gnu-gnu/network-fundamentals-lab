"""Development-only install: target refusals (M1-12) and the in-VM atomic switch."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import io
import tempfile
import unittest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
import packctl  # noqa: E402
import pack_format  # noqa: E402

VM = 'netlab-abcdef0123'


class FakeLxc:
    def __init__(self, verify='true', running=True, runner=None):
        self.verify, self.running, self.runner = verify, running, runner
        self.calls = []

    def __call__(self, *args, timeout=120, stdin=None):
        self.calls.append(args)
        if args[:2] == ('config', 'get'):
            key = args[3]
            return 0, (self.verify if key == packctl.VERIFY_KEY else 'inc') + '\n', ''
        if args[0] == 'list':
            return 0, json.dumps([{'name': VM, 'status': 'Running' if self.running else 'Stopped'}]), ''
        if args[:4] == ('exec', VM, '--', 'test'):
            return (0 if self.runner else 1), '', ''
        if args[:3] == ('exec', VM, '--') and args[3] == packctl.RUNNER_PATH and len(args) == 4:
            return 0, json.dumps(self.runner or {}), ''
        if args[:3] == ('exec', VM, '--') and args[-1] == '--init':
            return 0, '{"ok": true, "state": "idle", "damaged": false}', ''
        return 0, '{"ok": true}', ''

    def destructive(self):
        return [c for c in self.calls if c[0] in ('file',) or (c[0] == 'exec' and 'python3' in c)]


class InstallTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / 'config.json'
        self.state = Path(self.tmp.name) / 'state.json'
        self.write(environment='development', users={}, pool=[{'instance': VM, 'ready': True, 'claim': None}])
        catalog = pack_format.build(SERVER.parent)
        self.catalog = Path(self.tmp.name) / 'catalog.json'
        self.catalog.write_text(json.dumps(catalog))
        self.bundle = Path(self.tmp.name) / 'bundle.tar'
        pack_format.write_bundle(SERVER.parent, catalog, 'network', self.bundle)

    def write(self, environment, users, pool):
        cfg = {'auth_mode': 'open'}
        if environment:
            cfg['environment'] = environment
        self.config.write_text(json.dumps(cfg))
        self.state.write_text(json.dumps({'users': users, 'pool': pool}))

    def install(self, lxc):
        return packctl.install(VM, self.bundle, run=lxc, catalog_path=self.catalog,
                               config_path=self.config, state_path=self.state)

    def refused(self, lxc, pattern):
        with self.assertRaisesRegex(packctl.InstallRefused, pattern):
            self.install(lxc)
        self.assertEqual(lxc.destructive(), [])          # nothing pushed or executed

    def test_production_server_is_refused(self):
        self.write(environment=None, users={}, pool=[{'instance': VM, 'ready': True}])
        self.refused(FakeLxc(), '개발 환경')
        self.write(environment='production', users={}, pool=[{'instance': VM, 'ready': True}])
        self.refused(FakeLxc(), '개발 환경')

    def test_vm_without_label_is_refused(self):
        self.refused(FakeLxc(verify=''), '라벨')

    def test_assigned_or_unmanaged_vm_is_refused(self):
        self.write('development', {'alice': {'instance': VM, 'status': 'running'}}, [])
        self.refused(FakeLxc(), '할당')
        self.write('development', {}, [])
        self.refused(FakeLxc(), '관리')
        self.write('development', {}, [{'instance': VM, 'ready': True, 'claim': 'bob'}])
        self.refused(FakeLxc(), '예약')

    def test_stopped_vm_and_busy_runner_are_refused(self):
        self.refused(FakeLxc(running=False), '실행 중이 아닙니다')
        self.refused(FakeLxc(runner={'state': 'running', 'check_running': False}), '실행 중인 실습')
        self.refused(FakeLxc(runner={'state': 'idle', 'check_running': True}), '확인 작업')

    def test_stale_bundle_is_refused(self):
        catalog = json.loads(self.catalog.read_text())
        catalog['packs']['network']['bundle']['revision'] = 'f' * 64
        self.catalog.write_text(json.dumps(catalog))
        lxc = FakeLxc()
        with self.assertRaises(pack_format.PackError):
            self.install(lxc)
        self.assertEqual(lxc.destructive(), [])

    def test_eligible_vm_gets_one_release(self):
        lxc = FakeLxc()
        result = self.install(lxc)
        self.assertTrue(result['ok'])
        self.assertEqual([c[0] for c in lxc.destructive()], ['file', 'exec'])


class GuestSwitchTests(unittest.TestCase):
    """GUEST_INSTALL verifies everything before switching `current`; failures keep the old release."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name) / 'opt'
        catalog = pack_format.build(SERVER.parent)
        bundle = Path(self.tmp.name) / 'b.tar'
        pack_format.write_bundle(SERVER.parent, catalog, 'network', bundle)
        self.parts = pack_format.read_bundle(bundle)

    def release(self, tamper=None):
        release_id, data = packctl.release_archive(SERVER, *self.parts)
        if tamper:
            data = tamper(data)
        path = Path(self.tmp.name) / f'{release_id}.tar'
        path.write_bytes(data)
        return release_id, path

    def run_guest(self, release_id, path):
        return subprocess.run([sys.executable, '-c', packctl.GUEST_INSTALL, str(path), release_id, '2', str(self.base)],
                              capture_output=True, text=True)

    def test_switch_and_rollback_safety(self):
        rid, path = self.release()
        r = self.run_guest(rid, path)
        self.assertEqual(r.returncode, 0, r.stderr)
        current = self.base / 'current'
        self.assertTrue((current / 'runner/netlab-runner').is_file())
        self.assertTrue(os.access(current / 'runner/netlab-runner', os.X_OK))
        self.assertTrue((current / 'packs/network/files/12-mtu-pmtud/fix.sh').is_file())
        self.assertFalse((current / 'packs/network/files/12-mtu-pmtud/lesson.toml').exists())
        before = os.readlink(current)

        def corrupt(data):
            src = tarfile.open(fileobj=io.BytesIO(data))
            out = io.BytesIO()
            with tarfile.open(fileobj=out, mode='w') as dst:
                for m in src.getmembers():
                    body = src.extractfile(m).read()
                    if m.name.endswith('fix.sh'):
                        body += b'#tampered'
                        m.size = len(body)
                    dst.addfile(m, io.BytesIO(body))
            return out.getvalue()

        bad_id, bad = self.release(corrupt)
        r = self.run_guest('bad0000000000000', bad)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(os.readlink(current), before)               # previous install kept
        self.assertFalse(any(p.name.startswith('.staging') for p in (self.base / 'releases').iterdir()))


if __name__ == '__main__':
    unittest.main()

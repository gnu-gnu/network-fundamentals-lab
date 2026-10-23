from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from netlab import instances, store  # noqa: E402


class GuestPermissionTests(unittest.TestCase):
    def setUp(self):
        store.CONFIG.clear(); store.CONFIG.update(store.DEFAULTS)
        store.STATE.clear(); store.STATE.update({'users': {}, 'pool': []})

    def test_failed_permission_setup_is_not_ready(self):
        with patch.object(instances, 'lxc', return_value=(1, '', 'agent unavailable')):
            self.assertFalse(instances.ensure_lab_permissions('netlab-abcdef0123'))

    def test_shell_helper_is_synced_and_failure_is_not_ready(self):
        with patch.object(instances, 'lxc', side_effect=[(0, '', ''), (1, '', 'push failed')]) as run:
            self.assertFalse(instances.ensure_lab_permissions('netlab-abcdef0123'))
            self.assertEqual(run.call_args.args[:3], ('file', 'push', '--mode=0644'))
            self.assertEqual(run.call_args.args[-1], 'netlab-abcdef0123/etc/lab-shell.sh')

    def test_start_does_not_mark_vm_running_when_permissions_fail(self):
        store.STATE['users'] = {'guest': {'instance': 'netlab-abcdef0123', 'ip': '10.210.0.20'}}
        store.CONFIG['ready_timeout'] = 1
        with patch.object(instances, 'save_state'), patch.object(instances, 'regen_routing'), \
                patch.object(instances, 'instance_state', return_value='Running'), \
                patch.object(instances, 'ttyd_ready', return_value=True), \
                patch.object(instances, 'ensure_lab_permissions', return_value=False), \
                patch.object(instances.time, 'time', side_effect=[0, 0, 2]), patch.object(instances.time, 'sleep'):
            instances.start_for('guest')
        self.assertEqual(store.STATE['users']['guest']['status'], 'error')

    def test_direct_creations_never_publish_another_users_vm_to_pool(self):
        alice_waiting = threading.Event()
        release_alice = threading.Event()
        errors = []

        def ready(name, _ip):
            if name == 'netlab-aaaaaaaaaa':
                alice_waiting.set()
                return release_alice.wait(2)
            return True

        def claim(user):
            try:
                instances.claim_for(user)
            except Exception as exc:  # surface thread failures in the test
                errors.append(exc)

        ids = [SimpleNamespace(hex='aaaaaaaaaa0'), SimpleNamespace(hex='bbbbbbbbbb0')]
        with patch.object(instances.uuid, 'uuid4', side_effect=ids), \
                patch.object(instances, 'lxc', return_value=(0, '', '')), \
                patch.object(instances, '_wait_ready', side_effect=ready), \
                patch.object(instances, 'save_state'), patch.object(instances, 'regen_routing'):
            alice = threading.Thread(target=claim, args=('alice',))
            alice.start(); self.assertTrue(alice_waiting.wait(2))
            # Alice's in-progress VM is reserved, not visible as a ready warm VM.
            self.assertEqual(store.STATE['pool'][0]['claim'], 'alice')
            self.assertFalse(store.STATE['pool'][0]['ready'])
            bob = threading.Thread(target=claim, args=('bob',))
            bob.start(); bob.join(2)
            release_alice.set(); alice.join(2)

        self.assertFalse(errors)
        self.assertFalse(alice.is_alive() or bob.is_alive())
        self.assertNotEqual(store.STATE['users']['alice']['instance'],
                            store.STATE['users']['bob']['instance'])
        self.assertEqual(store.STATE['pool'], [])

    def test_reserved_vm_does_not_count_as_a_warm_spare(self):
        from netlab import maintainer
        store.CONFIG['pool_size'] = 1
        store.STATE['pool'] = [{'instance': 'netlab-aaaaaaaaaa', 'ip': '10.210.0.20', 'ready': False, 'claim': 'alice'}]
        with patch.object(maintainer, 'create_instance') as create:
            maintainer.fill_pool()
        create.assert_called_once()
        store.STATE['pool'] = [{'instance': 'netlab-bbbbbbbbbb', 'ip': '10.210.0.21', 'ready': True}]
        with patch.object(maintainer, 'create_instance') as create:
            maintainer.fill_pool()
        create.assert_not_called()


if __name__ == '__main__':
    unittest.main()

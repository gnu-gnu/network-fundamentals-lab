from pathlib import Path
import sys
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


if __name__ == '__main__':
    unittest.main()

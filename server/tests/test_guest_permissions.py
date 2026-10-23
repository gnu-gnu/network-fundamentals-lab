import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


class GuestPermissionTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('permissions_labd', Path(__file__).parents[1] / 'labd.py')
        self.labd = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.labd)

    def test_failed_permission_setup_is_not_ready(self):
        with patch.object(self.labd, 'lxc', return_value=(1, '', 'agent unavailable')):
            self.assertFalse(self.labd.ensure_lab_permissions('netlab-abcdef0123'))

    def test_shell_helper_is_synced_and_failure_is_not_ready(self):
        with patch.object(self.labd, 'lxc', side_effect=[(0, '', ''), (1, '', 'push failed')]) as run:
            self.assertFalse(self.labd.ensure_lab_permissions('netlab-abcdef0123'))
            self.assertEqual(run.call_args.args[:3], ('file', 'push', '--mode=0644'))
            self.assertEqual(run.call_args.args[-1], 'netlab-abcdef0123/etc/lab-shell.sh')

    def test_start_does_not_mark_vm_running_when_permissions_fail(self):
        m = self.labd
        m.STATE = {'users': {'guest': {'instance': 'netlab-abcdef0123', 'ip': '10.210.0.20'}}, 'pool': []}
        m.CONFIG['ready_timeout'] = 1
        with patch.object(m, 'save_state'), patch.object(m, 'regen_routing'), \
                patch.object(m, 'instance_state', return_value='Running'), \
                patch.object(m, 'ttyd_ready', return_value=True), \
                patch.object(m, 'ensure_lab_permissions', return_value=False), \
                patch.object(m.time, 'time', side_effect=[0, 0, 2]), patch.object(m.time, 'sleep'):
            m.start_for('guest')
        self.assertEqual(m.STATE['users']['guest']['status'], 'error')

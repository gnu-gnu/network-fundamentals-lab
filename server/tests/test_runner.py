"""VM runner state machine, tokens, recovery and check cancellation (fake runtime, no LXD/docker)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runner import store  # noqa: E402
from runner.executor import Runner  # noqa: E402
import fake_runner_adapter  # noqa: E402

INC = '11111111-2222-4333-8444-555555555555'
BUNDLE = 'b' * 64
RECIPE = {'format': 1, 'protocol': {'min': 1, 'max': 1}, 'pack': 'network', 'lessons': {
    'network/12': {'dir': '12-mtu-pmtud', 'runtime': 'containerlab', 'definition': '12.clab.yml',
                   'prefix': 'clab-12', 'nodes': ['h1', 'r1'], 'timeouts': {'setup': 5, 'teardown': 5, 'check': 5},
                   'steps': {'verify': {'check': {'all': [
                       {'node': 'h1', 'argv': ['ping'], 'expect': {'regex': r'mtu\s*=\s*1400'}, 'timeout': 5,
                        'fallback': {'node': 'h1', 'argv': ['ip'], 'expect': {'regex': r'\bmtu 1400\b', 'stream': 'stdout', 'rc': [0]},
                                     'timeout': 5, 'label': 'PMTU route cache'}}]}}},
                   'probes': {}},
    'network/04': {'dir': '04-l3-routing', 'runtime': 'containerlab', 'definition': '04.clab.yml',
                   'prefix': 'clab-04', 'nodes': ['h1'], 'timeouts': {}, 'steps': {}, 'probes': {}}}}


class RunnerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = self.tmp.name
        os.makedirs(self.prefix + '/test')
        Path(self.prefix + '/test/incarnation').write_text(INC)
        Path(self.prefix + '/test/boot_id').write_text('boot-1')
        self.paths = store.Paths(self.prefix)
        pack = Path(self.paths.packs) / 'network'
        pack.mkdir(parents=True)
        (pack / 'recipe.json').write_text(json.dumps(RECIPE))
        (pack / 'manifest.json').write_text(json.dumps({'pack': 'network', 'bundle': BUNDLE}))
        from runner.main import init
        init(self.paths)
        self.adapter = fake_runner_adapter.Adapter(self.paths)
        self.runner = Runner(self.paths, {'containerlab': self.adapter})

    def fake(self, **cfg):
        current = self.adapter.cfg()
        current.update(cfg)
        self.adapter.save(current)

    def calls(self):
        try:
            return [json.loads(l) for l in Path(self.prefix + '/test/calls.log').read_text().splitlines()]
        except FileNotFoundError:
            return []

    def send(self, action, **fields):
        req = {'protocol': 1, 'action': action, **fields}
        if action != 'status':
            req.setdefault('request_id', str(uuid.uuid4()))
        return self.runner.handle(json.dumps(req))

    def token(self):
        return self.send('status')['observed']

    def start(self, lesson='network/12', expected=None):
        return self.send('start', expected=expected or self.token(), target_lesson=lesson, expected_bundle=BUNDLE)


class LifecycleTests(RunnerCase):
    def test_status_reports_identity_and_capabilities(self):
        s = self.send('status')
        self.assertTrue(s['ok'])
        self.assertEqual(s['observed'], {'incarnation': INC, 'generation': 0})
        self.assertEqual(s['state'], 'idle')
        self.assertIn('check', s['capabilities'])
        self.assertEqual(s['installed'], {'network': BUNDLE})

    def test_start_then_same_lesson_start_is_a_noop(self):
        r = self.start()
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['executed'], {'incarnation': INC, 'generation': 1})
        before = len(self.calls())
        again = self.start()
        self.assertTrue(again['ok'] and again.get('noop'))
        self.assertEqual(again['observed']['generation'], 1)       # M1-02: generation kept
        self.assertEqual(len(self.calls()), before)                 # nothing torn down or redeployed

    def test_old_tokens_are_refused_before_anything_runs(self):
        first = self.token()
        self.start(expected=first)
        before = len(self.calls())
        for action, extra in (('start', {'target_lesson': 'network/04', 'expected_bundle': BUNDLE}),
                              ('reset', {'lesson': 'network/12', 'expected_bundle': BUNDLE}),
                              ('stop', {'lesson': 'network/12'})):
            r = self.send(action, expected=first, **extra)
            self.assertEqual(r['error'], 'conflict', action)        # M1-03
            self.assertIsNone(r['executed'])
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(self.send('status')['state'], 'running')

    def test_reset_and_stop_advance_generation(self):
        self.start()
        r = self.send('reset', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE)
        self.assertTrue(r['ok'])
        self.assertEqual(r['executed']['generation'], 2)
        r = self.send('stop', expected=self.token(), lesson='network/12')
        self.assertTrue(r['ok'])
        s = self.send('status')
        self.assertEqual((s['state'], s['lesson'], s['observed']['generation']), ('idle', None, 3))

    def test_switching_lesson_tears_down_the_previous_one(self):
        self.start()
        r = self.start('network/04')
        self.assertTrue(r['ok'])
        self.assertEqual([c[0] for c in self.calls()[-2:]], ['down_all', 'up'])
        self.assertEqual(self.send('status')['lesson'], 'network/04')

    def test_request_id_resend_is_answered_not_executed(self):
        token = self.token()
        rid = str(uuid.uuid4())
        r1 = self.send('start', request_id=rid, expected=token, target_lesson='network/12', expected_bundle=BUNDLE)
        before = len(self.calls())
        r2 = self.send('start', request_id=rid, expected=token, target_lesson='network/12', expected_bundle=BUNDLE)
        self.assertTrue(r2.get('replayed'))
        self.assertEqual(r2['executed'], r1['executed'])
        self.assertEqual(len(self.calls()), before)

    def test_bundle_mismatch_and_unknown_lesson(self):
        r = self.send('start', expected=self.token(), target_lesson='network/12', expected_bundle='c' * 64)
        self.assertEqual(r['error'], 'bundle_mismatch')
        r = self.send('start', expected=self.token(), target_lesson='network/99', expected_bundle=BUNDLE)
        self.assertEqual(r['error'], 'bundle_mismatch')
        self.assertEqual(self.calls(), [])

    def test_failed_start_is_reported_by_stage_and_can_be_stopped(self):
        self.fake(up_fails=True)
        r = self.start()
        self.assertFalse(r['ok'])
        self.assertEqual((r['error'], r['stage']), ('failed', 'runtime_up'))
        s = self.send('status')
        self.assertEqual(s['state'], 'start_failed')
        self.assertEqual(s['failure']['stage'], 'runtime_up')
        self.fake(up_fails=False)
        self.assertTrue(self.send('stop', expected=s['observed'], lesson='network/12')['ok'])

    def test_failed_stop_is_not_idle(self):
        self.start()
        self.fake(down_fails=True)
        r = self.send('stop', expected=self.token(), lesson='network/12')
        self.assertFalse(r['ok'])
        s = self.send('status')
        self.assertEqual(s['state'], 'stop_failed')
        r = self.start(expected=s['observed'])
        self.assertEqual(r['error'], 'conflict')                    # stop_failed is never treated as idle

    def test_unsupported_actions(self):
        r = self.send('solve', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE)
        self.assertEqual(r['error'], 'unsupported_action')

    def test_concurrent_change_is_busy(self):
        self.start()
        state, _ = store.load_state(self.paths)
        state['op'] = dict(request_id=str(uuid.uuid4()), action='reset', pid=os.getpid(),
                           ticks=__import__('runner.proc', fromlist=['x']).start_time(os.getpid()))
        store.save_state(self.paths, state)
        r = self.send('reset', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE)
        self.assertEqual(r['error'], 'busy')


class RecoveryTests(RunnerCase):
    def test_reboot_requires_explicit_recovery(self):
        self.start()
        Path(self.prefix + '/test/boot_id').write_text('boot-2')
        s = self.send('status')
        self.assertEqual(s['state'], 'recovery_required')
        self.assertEqual(s['recovery']['reason'], 'reboot')
        self.assertEqual(s['observed']['generation'], 2)            # bumped once
        self.assertEqual(self.send('status')['observed']['generation'], 2)
        c = self.send('check', expected=s['observed'], lesson='network/12', expected_bundle=BUNDLE, step='verify')
        self.assertEqual(c['error'], 'recovery_required')
        r = self.send('reset', expected=s['observed'], lesson='network/12', expected_bundle=BUNDLE)
        self.assertTrue(r['ok'])

    def test_damaged_state_is_never_cleaned_up_automatically(self):
        self.start()
        Path(self.paths.state).write_text('{broken')
        before = len(self.calls())
        s = self.send('status')
        self.assertFalse(s['ok'])
        self.assertEqual(s['error'], 'recovery_required')           # M1-07
        self.assertIsNone(s['observed'])
        self.assertEqual(s['recovery']['reason'], 'state_damaged')
        r = self.send('stop', expected={'incarnation': INC, 'generation': 1}, lesson='network/12')
        self.assertEqual(r['error'], 'recovery_required')
        self.assertEqual(len(self.calls()), before)                 # no teardown
        self.assertEqual(Path(self.paths.state).read_text(), '{broken')

    def test_missing_state_after_install_counts_as_damaged(self):
        os.unlink(self.paths.state)
        s = self.send('status')
        self.assertEqual(s['error'], 'recovery_required')
        self.assertFalse(os.path.exists(self.paths.state))

    def test_new_incarnation_voids_old_tokens(self):
        self.start()
        old = self.token()
        Path(self.prefix + '/test/incarnation').write_text('99999999-2222-4333-8444-555555555555')
        s = self.send('status')
        self.assertEqual(s['observed']['generation'], 0)
        r = self.send('stop', expected=old, lesson='network/12')
        self.assertEqual(r['error'], 'conflict')

    def test_interrupted_change_is_not_rerun(self):
        self.start()
        rid = str(uuid.uuid4())
        state, _ = store.load_state(self.paths)
        state.update(state='resetting', op=dict(request_id=rid, action='reset', pid=2 ** 22 + 7, ticks=1))
        store.save_state(self.paths, state)
        s = self.send('status')
        self.assertEqual((s['state'], s['recovery']['reason']), ('recovery_required', 'interrupted'))
        r = self.send('reset', request_id=rid, expected=s['observed'], lesson='network/12', expected_bundle=BUNDLE)
        self.assertEqual(r['error'], 'interrupted')
        self.assertTrue(r.get('replayed'))


class CheckTests(RunnerCase):
    def check(self):
        return self.send('check', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE, step='verify')

    def test_pass_fail_and_fallback(self):
        self.start()
        self.fake(exec={'ping': {'rc': 1, 'out': '1 packets transmitted, 0 received'},
                        'ip': {'rc': 0, 'out': '10.12.2.10 via 10.12.1.1 dev eth1 mtu 1400'}})
        r = self.check()
        self.assertEqual(r['result']['status'], 'pass')
        self.assertTrue(r['result']['items'][0]['fallback_used'])
        self.assertEqual(r['executed'], {'incarnation': INC, 'generation': 1})
        self.fake(exec={'ping': {'rc': 1, 'out': '0 received'}, 'ip': {'rc': 0, 'out': 'mtu 1500'}})
        self.assertEqual(self.check()['result']['status'], 'fail')

    def test_transport_and_timeout_are_not_learning_failures_and_skip_fallback(self):
        self.start()
        self.fake(exec={'ping': {'rc': 1, 'err': 'Error: No such container', 'transport': True}})
        r = self.check()
        self.assertEqual(r['result']['status'], 'unverifiable')
        self.assertFalse(r['result']['items'][0]['fallback_used'])
        self.fake(exec={'ping': {'timed_out': True}})
        self.assertEqual(self.check()['result']['status'], 'error')
        self.assertNotIn(['exec', 'h1', ['ip']], [c[:3] for c in self.calls()])

    def test_check_requires_running_lesson_and_current_tokens(self):
        r = self.send('check', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE, step='verify')
        self.assertEqual(r['error'], 'conflict')
        self.start()
        stale = {'incarnation': INC, 'generation': 0}
        r = self.send('check', expected=stale, lesson='network/12', expected_bundle=BUNDLE, step='verify')
        self.assertEqual(r['error'], 'conflict')

    def test_environment_changed_from_terminal(self):
        self.start()
        self.fake(deployed='network/04')
        r = self.check()
        self.assertEqual(r['error'], 'conflict')


class CancelTests(RunnerCase):
    """A change cancels a running check (separate process) and starts only once it has ended."""

    @staticmethod
    def finish(child):
        data = child.stdout.read()
        child.stdout.close()
        child.wait(5)
        return data

    def spawn_check(self):
        req = dict(protocol=1, action='check', request_id=str(uuid.uuid4()), expected=self.token(),
                   lesson='network/12', expected_bundle=BUNDLE, step='verify')
        env = dict(os.environ, NETLAB_RUNNER_PREFIX=self.prefix,
                   NETLAB_RUNNER_TEST_ADAPTER=str(Path(__file__).resolve().parent / 'fake_runner_adapter.py'))
        child = subprocess.Popen([sys.executable, str(SERVER / 'netlab-runner')], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, env=env)
        child.stdin.write(json.dumps(req).encode())
        child.stdin.close()
        for _ in range(200):
            state, _ = store.load_state(self.paths)
            if state.get('check') and any(c[0] == 'exec' for c in self.calls()):
                return child
            time.sleep(0.02)
        child.kill()
        self.fail('check did not start')

    def test_reset_cancels_check_and_waits_for_it(self):
        self.start()
        self.fake(exec={'ping': {'delay': 30}})
        child = self.spawn_check()
        mark = len(self.calls())
        began = time.monotonic()
        r = self.send('reset', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE)
        out = json.loads(self.finish(child))
        self.assertTrue(r['ok'], r)                                 # M1-05
        self.assertLess(time.monotonic() - began, 8)
        self.assertEqual(out['result']['status'], 'cancelled')
        names = [c[0] for c in self.calls()[mark:]]
        self.assertLess(names.index('cancelled'), names.index('down_all'))   # ended before the change
        self.assertIsNone(store.load_state(self.paths)[0]['check'])

    def test_unconfirmed_cancel_blocks_the_change(self):
        self.start()
        self.fake(exec={'ping': {'delay': 30}}, confirm=False)
        child = self.spawn_check()
        before = [c[0] for c in self.calls()].count('down_all')
        r = self.send('stop', expected=self.token(), lesson='network/12')
        self.finish(child)
        self.assertEqual(r['error'], 'cancel_failed')               # M1-06
        self.assertEqual([c[0] for c in self.calls()].count('down_all'), before)
        s = self.send('status')
        self.assertEqual((s['state'], s['recovery']['reason']), ('recovery_required', 'exec_unconfirmed'))

    def test_second_check_while_one_runs_is_busy(self):
        self.start()
        self.fake(exec={'ping': {'delay': 30}})
        child = self.spawn_check()
        try:
            r = self.send('check', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE, step='verify')
            self.assertEqual(r['error'], 'busy')
        finally:
            self.send('stop', expected=self.token(), lesson='network/12')
            self.finish(child)


if __name__ == '__main__':
    unittest.main()

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
from runner.shell import Shell  # noqa: E402
from runner import cli  # noqa: E402
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
                   'hooks': {'solve': 'fix.sh'},
                   'probes': {'probe-1': {'node': 'h1', 'argv': ['ip', 'route', 'get', '10.12.2.10']}}},
    'network/04': {'dir': '04-l3-routing', 'runtime': 'containerlab', 'definition': '04.clab.yml',
                   'prefix': 'clab-04', 'nodes': ['h1'], 'timeouts': {}, 'steps': {}, 'probes': {}},
    'network/17-ospf': {'dir': '17-dynamic-routing', 'runtime': 'containerlab', 'definition': 'ospf.clab.yml',
                        'prefix': 'clab-17-ospf', 'nodes': ['h1'], 'timeouts': {}, 'steps': {}, 'probes': {}},
    'network/17-bgp': {'dir': '17-dynamic-routing', 'runtime': 'containerlab', 'definition': 'bgp.clab.yml',
                       'prefix': 'clab-17-bgp', 'nodes': ['h1'], 'timeouts': {}, 'steps': {}, 'probes': {}}}}
SHELL_DIR = 'packs/example-shell/permissions'
SHELL_RECIPE = {'format': 1, 'protocol': {'min': 1, 'max': 1}, 'pack': 'example-shell', 'lessons': {
    'example-shell/permissions': {'dir': SHELL_DIR, 'runtime': 'shell', 'definition': None, 'prefix': None,
                                  'nodes': ['host'], 'timeouts': {'setup': 10, 'teardown': 10, 'solve': 10},
                                  'hooks': {'setup': 'env/setup.sh', 'solve': 'env/solve.sh', 'teardown': 'env/teardown.sh'},
                                  'steps': {'fix': {'check': {'script': 'checks/fix.sh', 'timeout': 10}}},
                                  'probes': {'mode': {'node': 'host', 'argv': ['cat', 'MODE']}}}}}


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
        fix = pack / 'files/12-mtu-pmtud/fix.sh'
        fix.parent.mkdir(parents=True)
        fix.write_text(f'#!/bin/sh\necho solved >> {self.prefix}/test/solved\n')
        fix.chmod(0o755)
        self.shell_pack()
        from runner.main import init
        init(self.paths)
        self.adapter = fake_runner_adapter.Adapter(self.paths)
        self.runner = Runner(self.paths, {'containerlab': self.adapter, 'shell': Shell(self.paths)})

    def shell_pack(self):
        """A shell-runtime lesson whose hooks and check script work inside the test prefix."""
        pack = Path(self.paths.packs) / 'example-shell'
        base = pack / 'files' / SHELL_DIR
        (base / 'env').mkdir(parents=True)
        (base / 'checks').mkdir()
        self.mode = Path(self.prefix) / 'shell-lab' / 'MODE'
        scripts = {
            'env/setup.sh': f'mkdir -p {self.mode.parent} && echo 644 > {self.mode} && echo setup >> {self.prefix}/test/hooks',
            'env/solve.sh': f'echo 600 > {self.mode}',
            'env/teardown.sh': f'rm -rf {self.mode.parent}; echo teardown >> {self.prefix}/test/hooks; '
                               f'[ ! -f {self.prefix}/test/teardown_fails ]',
            'checks/fix.sh': f'[ -f {self.mode} ] || exit 2; m=$(cat {self.mode}); '
                             'if [ -f "$NETLAB_WORK_DIR/claim" ]; then echo \'{"status": "pass"}\' > "$NETLAB_RESULT"; exit 1; fi; '
                             '[ "$m" = 600 ] && { echo \'{"message": "ok"}\' > "$NETLAB_RESULT"; exit 0; }; '
                             'echo "{\\"message\\": \\"mode $m\\"}" > "$NETLAB_RESULT"; exit 1',
        }
        for name, body in scripts.items():
            (base / name).write_text('#!/bin/bash\n' + body + '\n')
            (base / name).chmod(0o755)
        (pack / 'recipe.json').write_text(json.dumps(SHELL_RECIPE))
        (pack / 'manifest.json').write_text(json.dumps({'pack': 'example-shell', 'bundle': 'c' * 64}))

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
        self.assertEqual(s['installed'], {'network': BUNDLE, 'example-shell': 'c' * 64})

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

    def test_solve_runs_the_hook_and_needs_a_retest(self):
        self.start()
        token = self.token()
        r = self.send('solve', expected=token, lesson='network/12', expected_bundle=BUNDLE)
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['executed']['generation'], token['generation'] + 1)   # old check results are void
        self.assertEqual(Path(self.prefix + '/test/solved').read_text(), 'solved\n')
        s = self.send('status')
        self.assertEqual((s['state'], s['ready']), ('running', True))

    def test_solve_without_hook_is_unsupported(self):
        self.start('network/04')
        r = self.send('solve', expected=self.token(), lesson='network/04', expected_bundle=BUNDLE)
        self.assertEqual(r['error'], 'unsupported_action')
        self.assertIsNone(r['executed'])

    def test_probe_runs_only_registered_ids(self):
        self.start()
        self.fake(exec={'ip': {'rc': 0, 'out': '10.12.2.10 dev eth1 mtu 1400'}})
        r = self.send('probe', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE, probe='probe-1')
        self.assertEqual((r['result']['status'], r['result']['output']), ('ok', '10.12.2.10 dev eth1 mtu 1400'))
        r = self.send('probe', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE, probe='probe-9')
        self.assertEqual(r['error'], 'invalid_request')

    def test_concurrent_change_is_busy(self):
        self.start()
        state, _ = store.load_state(self.paths)
        state['op'] = dict(request_id=str(uuid.uuid4()), action='reset', pid=os.getpid(),
                           ticks=__import__('runner.proc', fromlist=['x']).start_time(os.getpid()))
        store.save_state(self.paths, state)
        r = self.send('reset', expected=self.token(), lesson='network/12', expected_bundle=BUNDLE)
        self.assertEqual(r['error'], 'busy')


class ShellRuntimeTests(RunnerCase):
    LESSON = 'example-shell/permissions'

    def shell(self, action, **extra):
        return self.send(action, expected=self.token(), lesson=self.LESSON, expected_bundle='c' * 64, **extra)

    def hooks(self):
        try:
            return Path(self.prefix + '/test/hooks').read_text().split()
        except FileNotFoundError:
            return []

    def test_lifecycle_with_hooks_and_script_check(self):
        r = self.send('start', expected=self.token(), target_lesson=self.LESSON, expected_bundle='c' * 64)
        self.assertTrue(r['ok'], r)
        self.assertEqual(self.mode.read_text().strip(), '644')
        r = self.shell('check', step='fix')
        self.assertEqual(r['result']['status'], 'fail')
        self.assertEqual(r['result']['items'][0]['message'], 'mode 644')
        self.mode.write_text('600\n')
        r = self.shell('check', step='fix')
        self.assertEqual((r['result']['status'], r['result']['items'][0]['message']), ('pass', 'ok'))
        self.assertTrue(self.shell('reset')['ok'])                 # no reset hook: teardown + setup
        self.assertEqual(self.mode.read_text().strip(), '644')
        self.assertEqual(self.hooks(), ['setup', 'teardown', 'setup'])
        self.assertTrue(self.shell('solve')['ok'])
        self.assertEqual(self.shell('check', step='fix')['result']['status'], 'pass')
        self.assertEqual(self.shell('probe', probe='mode')['result']['status'], 'ok')
        self.assertTrue(self.send('stop', expected=self.token(), lesson=self.LESSON)['ok'])
        self.assertFalse(self.mode.exists())

    def test_script_verdict_is_the_exit_code(self):
        self.send('start', expected=self.token(), target_lesson=self.LESSON, expected_bundle='c' * 64)
        work = Path(store.work_dir(self.paths, {'pack': 'example-shell', 'dir': SHELL_DIR}))
        work.mkdir(parents=True, exist_ok=True)
        (work / 'claim').write_text('x')          # script claims pass in its result file but exits 1
        self.assertEqual(self.shell('check', step='fix')['result']['status'], 'error')
        (work / 'claim').unlink()
        self.mode.unlink()
        self.assertEqual(self.shell('check', step='fix')['result']['status'], 'unverifiable')   # exit 2

    def test_shell_lesson_survives_reboot(self):
        self.send('start', expected=self.token(), target_lesson=self.LESSON, expected_bundle='c' * 64)
        Path(self.prefix + '/test/boot_id').write_text('boot-2')
        s = self.send('status')
        self.assertEqual((s['state'], s['observed']['generation']), ('running', 2))

    def test_switching_away_runs_the_previous_teardown_and_reports_its_failure(self):
        self.send('start', expected=self.token(), target_lesson=self.LESSON, expected_bundle='c' * 64)
        Path(self.prefix + '/test/teardown_fails').write_text('x')
        r = self.start('network/12')
        self.assertFalse(r['ok'])
        self.assertEqual(r['stage'], 'teardown')
        s = self.send('status')
        self.assertEqual((s['state'], s['lesson']), ('stop_failed', self.LESSON))   # old lesson, new never started
        self.assertNotIn('up', [c[0] for c in self.calls()])
        os.unlink(self.prefix + '/test/teardown_fails')
        self.assertTrue(self.send('stop', expected=s['observed'], lesson=self.LESSON)['ok'])
        self.assertTrue(self.start('network/12')['ok'])


class CliTests(RunnerCase):
    def test_lab_names_map_like_the_shell_command(self):
        self.assertEqual(cli.resolve(self.runner, '12'), 'network/12')
        self.assertEqual(cli.resolve(self.runner, '17'), 'network/17-bgp')       # first topology file, as `lab 17`
        self.assertEqual(cli.resolve(self.runner, '17', 'ospf'), 'network/17-ospf')
        self.assertEqual(cli.resolve(self.runner, '17', 'isis'), None)
        self.assertIsNone(cli.resolve(self.runner, '09'))
        self.assertEqual(cli.resolve(self.runner, 'permissions'), 'example-shell/permissions')


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

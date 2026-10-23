"""Fake containerlab adapter for runner tests (in-process, or via NETLAB_RUNNER_TEST_ADAPTER).

Behaviour comes from <prefix>/test/fake.json; every call is appended to <prefix>/test/calls.log so
tests can assert what did (and did not) happen — e.g. that nothing was torn down.
"""
import json
import os
import time

from runner.containerlab import ExecResult


class Adapter:
    name = 'containerlab'

    def __init__(self, paths):
        self.paths = paths
        self.dir = os.path.join(paths.state_dir.split('/var/lib/netlab/runner')[0], 'test')

    # ── helpers ──
    def cfg(self):
        try:
            with open(os.path.join(self.dir, 'fake.json')) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save(self, cfg):
        with open(os.path.join(self.dir, 'fake.json'), 'w') as f:
            json.dump(cfg, f)

    def log(self, *call):
        with open(os.path.join(self.dir, 'calls.log'), 'a') as f:
            f.write(json.dumps(call) + '\n')

    # ── adapter interface ──
    def ensure_work_copy(self, ldef, bundle_dir):
        return True

    def running_topologies(self):
        return self.cfg().get('topologies', [])

    def current(self, ldef):
        cfg = self.cfg()
        return cfg.get('deployed') == ldef['id']

    def ready(self, ldef):
        return self.current(ldef) and self.cfg().get('ready', True)

    def up(self, ldef, timeout):
        self.log('up', ldef['id'])
        cfg = self.cfg()
        if cfg.get('up_fails'):
            return False, 'deploy failed'
        cfg['deployed'] = ldef['id']
        cfg['topologies'] = [os.path.join(self.paths.work_root, ldef['dir'], ldef['definition'])]
        self.save(cfg)
        return True, 'deployed'

    def down_all(self, timeout, keep=None):
        self.log('down_all')
        cfg = self.cfg()
        if cfg.get('down_fails'):
            return False, 'destroy failed'
        cfg['deployed'] = None
        cfg['topologies'] = []
        self.save(cfg)
        return True, 'destroyed'

    def exec(self, ldef, node, argv, timeout, cancel=None, track=None):
        self.log('exec', node, argv)
        spec = self.cfg().get('exec', {}).get(argv[0], {'rc': 0, 'out': ''})
        if track:
            track(ldef['prefix'] + '-' + node, '/tmp/.fake.pid')
        delay = spec.get('delay', 0)
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                self.log('cancelled', node)
                return ExecResult(None, '', '', cancelled=True, confirmed=self.cfg().get('confirm', True))
            if time.monotonic() - (deadline - delay) >= timeout:
                return ExecResult(None, '', '', timed_out=True, confirmed=self.cfg().get('confirm', True))
            time.sleep(0.05)
        if track:
            track(None, None)
        return ExecResult(spec.get('rc', 0), spec.get('out', ''), spec.get('err', ''),
                          transport=spec.get('transport', False), timed_out=spec.get('timed_out', False))

    def stop_inner(self, container, pidfile, wait=3.0):
        self.log('stop_inner', container)
        return self.cfg().get('confirm', True)

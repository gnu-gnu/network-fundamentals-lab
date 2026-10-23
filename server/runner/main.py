"""Entry point: one JSON request on stdin, one JSON response on stdout (contract "요청 접수와 결과")."""
import json
import os
import sys

from . import store
from .executor import Runner
from .protocol import MAX_REQUEST


def init(paths):
    """Prepare the state directory after installation. Never resets an existing valid state."""
    os.makedirs(paths.state_dir, mode=0o700, exist_ok=True)
    os.makedirs(paths.checks, mode=0o700, exist_ok=True)
    state, damaged = store.load_state(paths)
    if state is None and not damaged:
        store.save_state(paths, store.initial_state(None, store.read_boot_id(paths)))
    if not os.path.exists(paths.installed):
        store.fsync_write(paths.installed, json.dumps({'installed': True}).encode())
    state, damaged = store.load_state(paths)
    return dict(ok=not damaged, state=(state or {}).get('state'), damaged=damaged)


def test_adapters(paths):
    """Unit tests replace the runtime with a fake. Only honoured when NETLAB_RUNNER_PREFIX relocates
    every path, so a real installation can never load it."""
    module_path = os.environ.get('NETLAB_RUNNER_TEST_ADAPTER')
    if not (paths.test and module_path):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location('netlab_runner_test_adapter', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {'containerlab': module.Adapter(paths)}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    paths = store.Paths()
    if argv == ['--init']:
        result = init(paths)
    else:
        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        try:
            result = Runner(paths, test_adapters(paths)).handle(raw)
        except Exception as exc:          # never leak a traceback as the protocol answer
            result = dict(protocol=1, ok=False, error='runner_error', message=type(exc).__name__ + ': ' + str(exc)[:300])
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + '\n')
    sys.stdout.flush()
    return 0

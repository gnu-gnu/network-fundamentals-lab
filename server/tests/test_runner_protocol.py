"""The hand-written runner validator accepts exactly what the published JSON Schema accepts, plus the
parse-level rules the schema cannot express (duplicate keys, strict integers, size limit).

The schema cross-check runs when `jsonschema` is importable (it is not a runtime dependency)."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'server'))
from runner.protocol import MAX_REQUEST, ProtocolError, parse, validate  # noqa: E402

U = '20d41a0c-b096-4f66-927e-22152ed8f9ba'
R = 'b0d6148d-a065-49ec-a315-34021b402ee5'
B = '0' * 64
E = {'incarnation': U, 'generation': 7}
VALID = [
    {'protocol': 1, 'action': 'status'},
    {'protocol': 1, 'request_id': R, 'action': 'start', 'expected': E, 'target_lesson': 'network/12', 'expected_bundle': B},
    {'protocol': 1, 'request_id': R, 'action': 'start', 'expected': dict(E, generation=0), 'target_lesson': 'network/12', 'expected_bundle': B},
    {'protocol': 1, 'request_id': R, 'action': 'reset', 'expected': E, 'lesson': 'network/12', 'expected_bundle': B},
    {'protocol': 1, 'request_id': R, 'action': 'solve', 'expected': E, 'lesson': 'network/12', 'expected_bundle': B},
    {'protocol': 1, 'request_id': R, 'action': 'stop', 'expected': E, 'lesson': 'network/12'},
    {'protocol': 1, 'request_id': R, 'action': 'check', 'expected': E, 'lesson': 'network/12', 'expected_bundle': B, 'step': 'verify'},
    {'protocol': 1, 'request_id': R, 'action': 'probe', 'expected': E, 'lesson': 'network/17-bgp', 'expected_bundle': B, 'probe': 'probe-1'},
]
INVALID = {
    'old stop without expected': {'protocol': 1, 'request_id': R, 'action': 'stop', 'lesson': 'network/12'},
    'extra argv': dict(VALID[6], argv=['ls']),
    'negative generation': dict(VALID[5], expected=dict(E, generation=-1)),
    'generation too large': dict(VALID[5], expected=dict(E, generation=2 ** 53)),
    'start missing target': {k: v for k, v in VALID[1].items() if k != 'target_lesson'},
    'protocol 2': {'protocol': 2, 'action': 'status'},
    'status with request_id': {'protocol': 1, 'action': 'status', 'request_id': R},
    'expected.state present': dict(VALID[5], expected=dict(E, state='running')),
    'uppercase uuid': dict(VALID[5], request_id=R.upper()),
    'lesson without pack': dict(VALID[6], lesson='12'),
    'stop with expected_bundle': dict(VALID[5], expected_bundle=B),
    'start with lesson': dict(VALID[1], lesson='network/12'),
    'generation bool': dict(VALID[5], expected=dict(E, generation=True)),
    'unknown action': {'protocol': 1, 'request_id': R, 'action': 'exec', 'expected': E, 'lesson': 'network/12'},
    'step with path': dict(VALID[6], step='../x'),
    'short digest': dict(VALID[3], expected_bundle='0' * 63),
    'protocol string': {'protocol': '1', 'action': 'status'},
    'not an object': ['status'],
}


class ProtocolTests(unittest.TestCase):
    def test_valid_requests(self):
        for request in VALID:
            self.assertEqual(validate(request), request)

    def test_invalid_requests(self):
        for name, request in INVALID.items():
            with self.assertRaises(ProtocolError, msg=name):
                validate(request)

    def test_parse_rules_beyond_the_schema(self):
        with self.assertRaises(ProtocolError):
            parse('{"protocol":1,"action":"status","action":"stop"}')       # duplicate key
        stop = json.dumps(VALID[5]).replace('"generation": 7', '"generation": 7.0')
        with self.assertRaises(ProtocolError):
            parse(stop)                                                       # 7.0 is not an integer literal
        with self.assertRaises(ProtocolError):
            parse('{"protocol":1,"action":"status","x":NaN}')
        with self.assertRaises(ProtocolError):
            parse(b'{"protocol":1,"action":"status"}' + b' ' * MAX_REQUEST)  # over 16KiB
        with self.assertRaises(ProtocolError):
            parse(b'\xff\xfe')
        self.assertEqual(parse(json.dumps(VALID[6]))['step'], 'verify')

    def test_matches_published_schema(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest('jsonschema not installed')
        schema = json.loads((ROOT / 'docs/schemas/netlab-runner-request.schema.json').read_text())
        Draft202012Validator.check_schema(schema)
        checker = Draft202012Validator(schema)
        for request in VALID:
            self.assertTrue(checker.is_valid(request), request)
        for name, request in INVALID.items():
            self.assertFalse(checker.is_valid(request), name)


if __name__ == '__main__':
    unittest.main()

"""Runner protocol 1 request validation.

Equivalent to docs/schemas/netlab-runner-request.schema.json (tests/test_runner_protocol.py runs the
same cases against both), plus what JSON Schema cannot express: duplicate keys are rejected at parse
time, integers must be JSON integer literals (7.0 and true are refused), and the body is at most 16KiB.
Structure only: ownership, registered ids, tokens, bundles and capabilities are checked separately.
"""
import json
import re

PROTOCOL = 1
MAX_REQUEST = 16 * 1024
MAX_GENERATION = 9007199254740991
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
DIGEST_RE = re.compile(r'^[0-9a-f]{64}$')
ID_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}$')
LESSON_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}/[a-z0-9][a-z0-9-]{0,39}$')

ACTIONS = {
    'status': (),
    'start': ('request_id', 'expected', 'target_lesson', 'expected_bundle'),
    'reset': ('request_id', 'expected', 'lesson', 'expected_bundle'),
    'solve': ('request_id', 'expected', 'lesson', 'expected_bundle'),
    'stop': ('request_id', 'expected', 'lesson'),
    'check': ('request_id', 'expected', 'lesson', 'expected_bundle', 'step'),
    'probe': ('request_id', 'expected', 'lesson', 'expected_bundle', 'probe'),
}


class ProtocolError(ValueError):
    code = 'invalid_request'


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ProtocolError(f'duplicate key: {key}')
        seen[key] = value
    return seen


def _reject_constant(name):
    raise ProtocolError(f'invalid number: {name}')


def loads(raw):
    """Parse a request body (bytes or str) strictly."""
    if isinstance(raw, str):
        raw = raw.encode()
    if len(raw) > MAX_REQUEST:
        raise ProtocolError('request too large')
    try:
        text = raw.decode('utf-8')
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except ProtocolError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f'malformed JSON: {exc}') from None


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _string(value, pattern, name):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProtocolError(f'invalid {name}')


def validate(request):
    """Return the request if its shape is valid for protocol 1, else raise ProtocolError."""
    if not isinstance(request, dict):
        raise ProtocolError('request must be an object')
    if request.get('protocol') != PROTOCOL or not _is_int(request.get('protocol')):
        raise ProtocolError('unsupported protocol')
    action = request.get('action')
    if action not in ACTIONS:
        raise ProtocolError('unknown action')
    allowed = ('protocol', 'action') + ACTIONS[action]
    extra = sorted(set(request) - set(allowed))
    if extra:
        raise ProtocolError('unexpected fields: ' + ', '.join(extra))
    missing = [name for name in ACTIONS[action] if name not in request]
    if missing:
        raise ProtocolError('missing fields: ' + ', '.join(missing))
    if 'request_id' in request:
        _string(request['request_id'], UUID_RE, 'request_id')
    if 'expected' in request:
        expected = request['expected']
        if not isinstance(expected, dict) or set(expected) != {'incarnation', 'generation'}:
            raise ProtocolError('expected must hold exactly incarnation and generation')
        _string(expected['incarnation'], UUID_RE, 'expected.incarnation')
        generation = expected['generation']
        if not _is_int(generation) or not 0 <= generation <= MAX_GENERATION:
            raise ProtocolError('invalid expected.generation')
    for name in ('target_lesson', 'lesson'):
        if name in request:
            _string(request[name], LESSON_RE, name)
    if 'expected_bundle' in request:
        _string(request['expected_bundle'], DIGEST_RE, 'expected_bundle')
    for name in ('step', 'probe'):
        if name in request:
            _string(request[name], ID_RE, name)
    return request


def parse(raw):
    return validate(loads(raw))

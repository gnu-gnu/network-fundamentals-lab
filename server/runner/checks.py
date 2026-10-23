"""Declarative check evaluation (contract "검증 결과와 훅").

  pass          expect satisfied
  fail          ran normally, expect not satisfied (a non-zero rc alone is not a transport failure)
  unverifiable  node missing / exec transport failure
  error         timeout, signal, internal error
  cancelled     stopped by a pending change; never counted as a learning result

All items run in declaration order; the aggregate is the highest of error > unverifiable > fail > pass.
A fallback runs once, only when its primary item is a plain `fail`, so timeouts and environment
failures are never hidden behind it.
"""
import re

ORDER = {'pass': 0, 'fail': 1, 'unverifiable': 2, 'error': 3}
DETAIL = 600


def passes(expect, rc, out, err):
    """Same matching rule as the v1 tutor (netlab_tutor._passes)."""
    text = out if expect.get('stream') == 'stdout' else out + err
    if 'rc' in expect and rc not in expect['rc']:
        return False
    if 'contains' in expect:
        return expect['contains'] in text
    return bool(re.search(expect['regex'], text, re.I))


def classify(item, result):
    if result.cancelled:
        return 'cancelled'
    if result.timed_out:
        return 'error'
    if result.transport:
        return 'unverifiable'
    if result.rc is not None and result.rc < 0:
        return 'error'                 # killed by a signal
    return 'pass' if passes(item['expect'], result.rc, result.out, result.err) else 'fail'


class Cancelled(Exception):
    def __init__(self, confirmed):
        super().__init__('cancelled')
        self.confirmed = confirmed


def run_item(adapter, ldef, item, cancel, track):
    r = adapter.exec(ldef, item['node'], item['argv'], item.get('timeout', 30), cancel=cancel, track=track)
    status = classify(item, r)
    if status == 'cancelled' or (cancel is not None and cancel.is_set()):
        raise Cancelled(r.confirmed)
    if (r.timed_out or r.cancelled) and not r.confirmed:
        raise Cancelled(False) if r.cancelled else RuntimeError('inner process did not stop after timeout')
    return status, r


def evaluate(adapter, ldef, check, cancel=None, track=None):
    """Return {'status', 'items': [...]} or raise Cancelled."""
    items = check['all'] if 'all' in check else [check]
    results = []
    for index, item in enumerate(items):
        status, r = run_item(adapter, ldef, item, cancel, track)
        detail = r.out + r.err
        fallback_used = False
        if status == 'fail' and item.get('fallback'):
            fallback = item['fallback']
            fb_status, fb = run_item(adapter, ldef, fallback, cancel, track)
            detail += '\n[' + fallback.get('label', '보조 확인') + ']\n' + fb.out + fb.err
            status, fallback_used = fb_status, True
        results.append(dict(index=index, label=item.get('label'), node=item['node'], result=status,
                            rc=r.rc, detail=detail[-DETAIL:], fallback_used=fallback_used,
                            truncated=r.truncated))
    worst = max((r['result'] for r in results), key=ORDER.get, default='pass')
    return dict(status=worst, items=results)

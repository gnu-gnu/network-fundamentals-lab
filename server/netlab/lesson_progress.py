"""Learning pack v2 progress: attempts, append-only step events, jobs, completion (M1).

Rules (docs/PACK_IMPLEMENTATION_CONTRACTS.md "회차와 완료 기록", "중복 방지와 잠금"):
  · one open attempt per (account, instance); an attempt's generation only ever increases
  · a result counts for the current state only if its executed token equals the attempt's current
    (incarnation, generation); otherwise it is stored as stale history
  · completion is recorded once per attempt and never closes it; reset/stop stay possible
  · a request_id is bound to its request content; the same id with other content is refused
No external command runs while a write transaction is open.
"""
import hashlib
import json
import uuid

from . import db, paths
from .util import iso

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS attempts(
  attempt_id TEXT PRIMARY KEY, account TEXT NOT NULL, instance TEXT NOT NULL, incarnation TEXT NOT NULL,
  lesson_id TEXT NOT NULL, pack_revision TEXT NOT NULL, bundle TEXT NOT NULL, generation INTEGER NOT NULL,
  shared INTEGER NOT NULL DEFAULT 0, assisted INTEGER NOT NULL DEFAULT 0, origin TEXT NOT NULL DEFAULT 'web',
  started_at TEXT NOT NULL, completed_at TEXT, ended_at TEXT, end_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS attempts_open ON attempts(account, instance) WHERE ended_at IS NULL;
CREATE TABLE IF NOT EXISTS lesson_jobs(
  job_id TEXT PRIMARY KEY, account TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
  action TEXT NOT NULL, lesson_id TEXT NOT NULL, step_id TEXT, instance TEXT,
  expected_incarnation TEXT, expected_generation INTEGER,
  state TEXT NOT NULL, http_status INTEGER, result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(account, request_id));
CREATE TABLE IF NOT EXISTS step_events(
  event_id INTEGER PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id), step_id TEXT,
  kind TEXT NOT NULL, status TEXT, requested_incarnation TEXT, requested_generation INTEGER,
  executed_incarnation TEXT, executed_generation INTEGER, bundle TEXT, stale INTEGER NOT NULL DEFAULT 0,
  request_id TEXT, job_id TEXT REFERENCES lesson_jobs(job_id), message TEXT, payload TEXT, created_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS step_events_job ON step_events(job_id) WHERE job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS step_events_attempt ON step_events(attempt_id, step_id, kind);
CREATE TABLE IF NOT EXISTS completion_events(
  attempt_id TEXT PRIMARY KEY REFERENCES attempts(attempt_id), account TEXT NOT NULL, lesson_id TEXT NOT NULL,
  pack_revision TEXT NOT NULL, assisted INTEGER NOT NULL, shared INTEGER NOT NULL, completed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lesson_summary(
  account TEXT NOT NULL, lesson_id TEXT NOT NULL, completions INTEGER NOT NULL DEFAULT 0,
  assisted_completions INTEGER NOT NULL DEFAULT 0, first_completed_at TEXT, last_completed_at TEXT,
  last_completed_revision TEXT, PRIMARY KEY(account, lesson_id));
"""

CHANGE_ACTIONS = ('start', 'reset', 'stop', 'solve')
ACTIVE = ('queued', 'running')
JOB_RETENTION_DAYS = 7


class Refused(Exception):
    def __init__(self, code, message, http=409):
        super().__init__(message)
        self.code, self.message, self.http = code, message, http


def connect(immediate=True):
    return db.connection(paths.LEARNING_DB, SCHEMA, immediate=immediate)


def request_hash(action, lesson_id, step_id, expected):
    body = json.dumps(dict(action=action, lesson=lesson_id, step=step_id, expected=expected),
                      sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(body.encode()).hexdigest()


def _row(row):
    return dict(row) if row is not None else None


# ─────────────────────────────── jobs ───────────────────────────────

def admit(account, request_id, action, lesson_id, step_id, expected, instance):
    """Register a job or return the existing one. Returns (job, created)."""
    digest = request_hash(action, lesson_id, step_id, expected)
    with connect() as c:
        same = c.execute('SELECT * FROM lesson_jobs WHERE account=? AND request_id=?',
                         (account, request_id)).fetchone()
        if same:
            if same['request_hash'] != digest:
                raise Refused('conflict', '같은 요청 ID 로 다른 내용을 보냈습니다. 새로고침한 뒤 다시 시도하세요.')
            return _row(same), False
        active = [dict(r) for r in c.execute(
            "SELECT * FROM lesson_jobs WHERE account=? AND state IN ('queued','running')", (account,))]
        if any(job['action'] in CHANGE_ACTIONS for job in active):
            raise Refused('busy', '실습 환경을 바꾸는 작업이 진행 중입니다. 끝난 뒤 다시 시도하세요.')
        if action == 'check':
            for job in active:
                if job['action'] == 'check':
                    if (job['lesson_id'], job['step_id'], job['expected_incarnation'], job['expected_generation']) == \
                            (lesson_id, step_id, expected['incarnation'], expected['generation']):
                        return job, False         # another tab is already checking the same thing
                    raise Refused('busy', '다른 확인이 진행 중입니다. 끝난 뒤 다시 시도하세요.')
        now = iso()
        job = dict(job_id=uuid.uuid4().hex, account=account, request_id=request_id, request_hash=digest,
                   action=action, lesson_id=lesson_id, step_id=step_id, instance=instance,
                   expected_incarnation=expected['incarnation'], expected_generation=expected['generation'],
                   state='queued', http_status=None, result=None, created_at=now, updated_at=now)
        c.execute('INSERT INTO lesson_jobs(' + ','.join(job) + ') VALUES(' + ','.join('?' * len(job)) + ')',
                  tuple(job.values()))
        return job, True


def set_job(job_id, state, result=None, http_status=None):
    with connect() as c:
        c.execute('UPDATE lesson_jobs SET state=?, result=?, http_status=?, updated_at=? WHERE job_id=?',
                  (state, json.dumps(result, ensure_ascii=False) if result is not None else None,
                   http_status, iso(), job_id))


def job(account, job_id):
    """A job of this account only; other accounts' jobs do not exist for the caller."""
    with connect(immediate=False) as c:
        row = c.execute('SELECT * FROM lesson_jobs WHERE job_id=? AND account=?', (job_id, account)).fetchone()
    if row is None:
        return None
    out = dict(row)
    out['result'] = json.loads(out['result']) if out['result'] else None
    return out


def active_jobs(account):
    with connect(immediate=False) as c:
        return [dict(r) for r in c.execute(
            "SELECT job_id, action, lesson_id, step_id, state FROM lesson_jobs WHERE account=? AND state IN ('queued','running')",
            (account,))]


def interrupt_unfinished():
    """At labd start: jobs that were queued/running cannot be resumed. Never re-run them."""
    with connect() as c:
        c.execute("UPDATE lesson_jobs SET state='interrupted', http_status=409, updated_at=?, "
                  "result=? WHERE state IN ('queued','running')",
                  (iso(), json.dumps(dict(ok=False, error='interrupted',
                                          message='서버가 다시 시작되어 작업 결과를 확인하지 못했습니다. 상태를 새로 확인하세요.'))))
        c.execute("DELETE FROM lesson_jobs WHERE state IN ('done','failed') AND updated_at < datetime('now', ?) "
                  "AND job_id NOT IN (SELECT job_id FROM step_events WHERE job_id IS NOT NULL)",
                  (f'-{JOB_RETENTION_DAYS} days',))


# ─────────────────────────────── attempts ───────────────────────────────

def open_attempt(c, account, instance):
    return _row(c.execute('SELECT * FROM attempts WHERE account=? AND instance=? AND ended_at IS NULL',
                          (account, instance)).fetchone())


def current_attempt(account, instance):
    with connect(immediate=False) as c:
        return open_attempt(c, account, instance)


def _close(c, attempt, reason):
    c.execute('UPDATE attempts SET ended_at=?, end_reason=? WHERE attempt_id=? AND ended_at IS NULL',
              (iso(), reason, attempt['attempt_id']))


def _advance(c, attempt, observed):
    """Monotonic generation update: a late answer never moves an attempt back."""
    if observed and observed['incarnation'] == attempt['incarnation'] and observed['generation'] > attempt['generation']:
        c.execute('UPDATE attempts SET generation=? WHERE attempt_id=? AND generation<?',
                  (observed['generation'], attempt['attempt_id'], observed['generation']))
        attempt['generation'] = observed['generation']
    return attempt


def _new_attempt(c, account, instance, lesson_id, observed, bundle, pack_revision, shared, origin='web'):
    attempt = dict(attempt_id=uuid.uuid4().hex, account=account, instance=instance,
                   incarnation=observed['incarnation'], lesson_id=lesson_id, pack_revision=pack_revision,
                   bundle=bundle or '', generation=observed['generation'], shared=int(shared), assisted=0,
                   origin=origin, started_at=iso(), completed_at=None, ended_at=None, end_reason=None)
    c.execute('INSERT INTO attempts(' + ','.join(attempt) + ') VALUES(' + ','.join('?' * len(attempt)) + ')',
              tuple(attempt.values()))
    return attempt


def sync(account, instance, status, adopt=None):
    """Reconcile the open attempt with a fresh runner status. Never starts, stops or redeploys.

    Attempts that no longer describe the environment are closed. A running lesson without an attempt
    (started from the terminal, or a start whose answer was lost) is connected to a new attempt with
    origin 'adopted_cli' when `adopt(lesson_id)` returns (pack_revision, shared) for it.
    """
    observed = status.get('observed')
    if not status.get('ok') or not observed:
        return current_attempt(account, instance)
    with connect() as c:
        attempt = open_attempt(c, account, instance)
        if attempt:
            if observed['incarnation'] != attempt['incarnation']:
                _close(c, attempt, 'instance_replaced')
                attempt = None
            elif status.get('state') == 'idle':
                _close(c, attempt, 'environment_idle')
                return None
            elif status.get('lesson') != attempt['lesson_id']:
                _close(c, attempt, 'superseded')
                attempt = None
            else:
                return _advance(c, attempt, observed)
        lesson_id = status.get('lesson')
        if (adopt and lesson_id and status.get('state') == 'running' and status.get('ready')
                and status.get('bundle')):
            found = adopt(lesson_id)
            if found:
                pack_revision, shared = found
                return _new_attempt(c, account, instance, lesson_id, observed, status['bundle'],
                                    pack_revision, shared, origin='adopted_cli')
        return None


def _event(c, attempt, kind, step_id=None, status=None, request=None, response=None, job_id=None,
           stale=0, payload=None, message=None):
    expected = (request or {}).get('expected') or {}
    executed = (response or {}).get('executed') or {}
    c.execute('INSERT INTO step_events(attempt_id, step_id, kind, status, requested_incarnation, requested_generation,'
              ' executed_incarnation, executed_generation, bundle, stale, request_id, job_id, message, payload, created_at)'
              ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (attempt['attempt_id'], step_id, kind, status, expected.get('incarnation'), expected.get('generation'),
               executed.get('incarnation'), executed.get('generation'), (response or {}).get('bundle'), int(stale),
               (request or {}).get('request_id'), job_id, message,
               json.dumps(payload, ensure_ascii=False) if payload is not None else None, iso()))


def record_change(account, instance, lesson, request, response, job_id, pack_revision, shared):
    """Apply a start/reset/stop answer to the attempts. Returns the open attempt (or None)."""
    action = request['action']
    observed = response.get('observed')
    with connect() as c:
        attempt = open_attempt(c, account, instance)
        if not response.get('ok'):
            if attempt and observed and response.get('executed'):
                # The environment did change (failed stage): the old attempt no longer matches it.
                if action == 'start' or observed['incarnation'] != attempt['incarnation']:
                    _close(c, attempt, 'superseded')
                    return None
                _advance(c, attempt, observed)
                if action == 'solve':
                    _assisted(c, attempt)          # attempted counts as help used, even if it failed
                _event(c, attempt, action, status='failed', request=request, response=response, job_id=job_id,
                       message=response.get('message'))
            elif attempt and observed:
                _advance(c, attempt, observed)
            return attempt
        if action == 'solve':
            if attempt and attempt['lesson_id'] == lesson['id']:
                _advance(c, attempt, observed)
                _assisted(c, attempt)
                _event(c, attempt, 'solve', status='ok', request=request, response=response, job_id=job_id)
            return attempt
        if action == 'stop':
            if attempt:
                _advance(c, attempt, observed)
                _event(c, attempt, 'stop', status='ok', request=request, response=response, job_id=job_id)
                _close(c, attempt, 'stopped')
            return None
        same = attempt and attempt['lesson_id'] == lesson['id'] and observed and \
            attempt['incarnation'] == observed['incarnation']
        if attempt and not same:
            _close(c, attempt, 'instance_replaced' if observed and attempt['incarnation'] != observed['incarnation']
                   else 'superseded')
            attempt = None
        if attempt is None:
            attempt = _new_attempt(c, account, instance, lesson['id'], observed, response.get('bundle'),
                                   pack_revision, shared)
        else:
            _advance(c, attempt, observed)
        _event(c, attempt, action, status='noop' if response.get('noop') else 'ok', request=request,
               response=response, job_id=job_id)
        return attempt


def _assisted(c, attempt):
    """Help used before completion. A completion already recorded keeps its own assisted value."""
    if not attempt['completed_at']:
        c.execute('UPDATE attempts SET assisted=1 WHERE attempt_id=? AND completed_at IS NULL',
                  (attempt['attempt_id'],))
        attempt['assisted'] = 1


def _task_status(c, attempt, step_id):
    row = c.execute("SELECT status FROM step_events WHERE attempt_id=? AND step_id=? AND kind='check' AND stale=0"
                    " AND status != 'cancelled' AND executed_incarnation=? AND executed_generation=?"
                    " ORDER BY event_id DESC LIMIT 1",
                    (attempt['attempt_id'], step_id, attempt['incarnation'], attempt['generation'])).fetchone()
    return row['status'] if row else 'untested'


def _read_status(c, attempt, step_id):
    row = c.execute("SELECT 1 FROM step_events WHERE attempt_id=? AND step_id=? AND kind='view' AND status='done' LIMIT 1",
                    (attempt['attempt_id'], step_id)).fetchone()
    return 'done' if row else 'unread'


def step_states(c, attempt, lesson):
    states = {}
    for step in lesson['steps']:
        if step['kind'] == 'task':
            states[step['id']] = _task_status(c, attempt, step['id'])
        elif step['kind'] == 'read':
            states[step['id']] = _read_status(c, attempt, step['id'])
    return states


def _satisfied(step, status):
    return {'task': 'pass', 'read': 'done'}.get(step['kind']) == status


def _maybe_complete(c, attempt, lesson):
    if attempt['completed_at'] or lesson.get('completion') != 'required-steps':
        return False
    states = step_states(c, attempt, lesson)
    required = [step for step in lesson['steps'] if step.get('required')]
    if not required or not all(_satisfied(step, states.get(step['id'])) for step in required):
        return False
    now = iso()
    updated = c.execute('UPDATE attempts SET completed_at=? WHERE attempt_id=? AND completed_at IS NULL',
                        (now, attempt['attempt_id'])).rowcount
    if not updated:
        return False
    attempt['completed_at'] = now
    c.execute('INSERT INTO completion_events(attempt_id, account, lesson_id, pack_revision, assisted, shared, completed_at)'
              ' VALUES(?,?,?,?,?,?,?)', (attempt['attempt_id'], attempt['account'], attempt['lesson_id'],
                                         attempt['pack_revision'], attempt['assisted'], attempt['shared'], now))
    if not attempt['shared']:
        c.execute('INSERT INTO lesson_summary(account, lesson_id, completions, assisted_completions,'
                  ' first_completed_at, last_completed_at, last_completed_revision) VALUES(?,?,1,?,?,?,?)'
                  ' ON CONFLICT(account, lesson_id) DO UPDATE SET completions=completions+1,'
                  ' assisted_completions=assisted_completions+excluded.assisted_completions,'
                  ' last_completed_at=excluded.last_completed_at, last_completed_revision=excluded.last_completed_revision',
                  (attempt['account'], attempt['lesson_id'], attempt['assisted'], now, now, attempt['pack_revision']))
    return True


def record_check(account, instance, lesson, request, response, job_id):
    """Store a check answer. Returns dict(attempt, status, stale, completed_now)."""
    with connect() as c:
        attempt = open_attempt(c, account, instance)
        result = response.get('result') or {}
        status = result.get('status') or ('error' if not response.get('ok') else None)
        if not attempt or attempt['lesson_id'] != lesson['id']:
            return dict(attempt=None, status=status, stale=True, completed_now=False)
        _advance(c, attempt, response.get('observed'))
        executed = response.get('executed')
        stale = (not response.get('ok') or not executed or status in (None, 'cancelled')
                 or executed['incarnation'] != attempt['incarnation']
                 or executed['generation'] != attempt['generation'])
        payload = dict(items=result.get('items') or []) if result else dict(error=response.get('error'))
        _event(c, attempt, 'check', step_id=request['step'], status=status or response.get('error'),
               request=request, response=response, job_id=job_id, stale=stale, payload=payload,
               message=response.get('message'))
        completed = (not stale and status == 'pass') and _maybe_complete(c, attempt, lesson)
        return dict(attempt=attempt, status=status, stale=stale, completed_now=completed)


def record_view(account, instance, lesson, step_id):
    with connect() as c:
        attempt = open_attempt(c, account, instance)
        if not attempt or attempt['lesson_id'] != lesson['id']:
            raise Refused('no_attempt', '이 실습을 먼저 시작하세요.')
        if _read_status(c, attempt, step_id) != 'done':
            _event(c, attempt, 'view', step_id=step_id, status='done')
            _maybe_complete(c, attempt, lesson)
        return attempt


def record_hint(account, instance, lesson, step_id, n):
    """Hints open in order within an attempt: hint n requires hint n-1 to have been opened."""
    with connect() as c:
        attempt = open_attempt(c, account, instance)
        if not attempt or attempt['lesson_id'] != lesson['id']:
            raise Refused('no_attempt', '이 실습을 먼저 시작하세요.')
        opened = {json.loads(r['payload'])['n'] for r in c.execute(
            "SELECT payload FROM step_events WHERE attempt_id=? AND step_id=? AND kind='hint'",
            (attempt['attempt_id'], step_id))}
        if n > 1 and (n - 1) not in opened:
            raise Refused('conflict', '앞 단계 힌트를 먼저 여세요.')
        if n not in opened:
            _event(c, attempt, 'hint', step_id=step_id, status='opened', payload=dict(n=n))
        return sorted(opened | {n})


def view(account, instance, lesson):
    """Attempt summary and step states for the panel."""
    with connect(immediate=False) as c:
        attempt = open_attempt(c, account, instance)
        if not attempt or attempt['lesson_id'] != lesson['id']:
            return None
        states = step_states(c, attempt, lesson)
        hints = {}
        for r in c.execute("SELECT step_id, payload FROM step_events WHERE attempt_id=? AND kind='hint'",
                           (attempt['attempt_id'],)):
            hints.setdefault(r['step_id'], set()).add(json.loads(r['payload'])['n'])
        last = {}
        for step in lesson['steps']:
            if step['kind'] != 'task':
                continue
            row = c.execute("SELECT status, stale, payload, created_at FROM step_events WHERE attempt_id=? AND step_id=?"
                            " AND kind='check' ORDER BY event_id DESC LIMIT 1",
                            (attempt['attempt_id'], step['id'])).fetchone()
            if row:
                last[step['id']] = dict(status=row['status'], stale=bool(row['stale']), at=row['created_at'],
                                        items=(json.loads(row['payload']) or {}).get('items', []) if row['payload'] else [])
        summary = c.execute('SELECT completions, first_completed_at FROM lesson_summary WHERE account=? AND lesson_id=?',
                            (account, lesson['id'])).fetchone()
        return dict(attempt_id=attempt['attempt_id'], lesson_id=attempt['lesson_id'], generation=attempt['generation'],
                    origin=attempt['origin'], assisted=bool(attempt['assisted']),
                    incarnation=attempt['incarnation'], started_at=attempt['started_at'],
                    completed_at=attempt['completed_at'], shared=bool(attempt['shared']), steps=states,
                    hints_opened={k: sorted(v) for k, v in hints.items()}, last_check=last,
                    completions=summary['completions'] if summary else 0)

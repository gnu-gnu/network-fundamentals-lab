"""Learning pack v2 lesson API for the learner panel (M1: start/reset/stop/check, read, hints).

labd authenticates the account and applies the same-origin JSON check, then calls handle_get /
handle_post here. Users only ever see public response objects built from the catalog — never the
catalog itself, the recipe, fault text or hints they have not opened.

  GET  lesson/state                              runner status + open attempt for the current lesson
  GET  lesson/<pack>/<lesson>                    public lesson view (steps, rendered guide)
  GET  lesson/jobs/<job_id>                      own job result
  POST lesson/<pack>/<lesson>/start|reset|stop   {request_id, expected}        → 202 {job_id}
  POST lesson/<pack>/<lesson>/step/<step>/check  {request_id, expected}        → 202 {job_id}
  POST lesson/<pack>/<lesson>/step/<step>/view   {}                            read step done
  POST lesson/<pack>/<lesson>/step/<step>/hint   {n}                           hint n (in order)
"""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import guide_view
import knowledge
from runner.protocol import ID_RE, UUID_RE

from . import lesson_progress as progress
from . import paths
from .agent_client import CLIENT, AgentError
from .store import CONFIG
from .util import log

CATALOG_PATH = os.path.join(paths.CODE_DIR, 'knowledge', 'v2', 'catalog.json')
HTTP = {'conflict': 409, 'busy': 409, 'bundle_mismatch': 409, 'recovery_required': 409,
        'cancel_failed': 409, 'interrupted': 409, 'unsupported_action': 409, 'invalid_request': 400,
        'runner_missing': 410, 'runner_error': 502, 'no_environment': 409, 'failed': 200}
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='lesson-job')


def load_catalog(path=CATALOG_PATH):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return dict(format=1, packs={}, lessons={}, aliases={}, revision='')


CATALOG = load_catalog()


def lesson_of(pack, name):
    lesson_id = f'{pack}/{name}'
    if not ID_RE.fullmatch(pack or '') or not ID_RE.fullmatch(name or ''):
        return None
    return CATALOG['lessons'].get(lesson_id)


def step_of(lesson, step_id):
    return next((s for s in lesson['steps'] if s['id'] == step_id), None)


def shared(account):
    """The open-mode guest VM is shared by every visitor (same rule as the panel notice)."""
    return account == 'guest' and CONFIG.get('auth_mode') == 'open'


# ─────────────────────────────── public views ───────────────────────────────

def _render(path):
    doc = knowledge.source_by_path(path)
    if not doc:
        return ''
    html = guide_view.body(doc)
    # guide_view links are relative to the guide page (ctl/guide/<id>); the panel lives at ctl/.
    return re.sub(r'href="(?!https?:|#)', 'href="guide/', html)


def public_lesson(lesson):
    steps = []
    for step in lesson['steps']:
        item = dict(id=step['id'], kind=step['kind'], title=step.get('title'), required=bool(step.get('required')))
        if step['kind'] == 'read':
            item['html'] = _render(step['text'])
        if step['kind'] == 'task':
            item['goal'] = step.get('goal')
            item['hints'] = len(step.get('hints') or [])
        steps.append(item)
    pack = CATALOG['packs'][lesson['pack']]
    return dict(id=lesson['id'], title=lesson['title'], number=lesson['number'], completion=lesson['completion'],
                steps=steps, revision=pack['revision'], bundle=pack['bundle']['revision'])


def public_catalog():
    return [dict(id=l['id'], title=l['title'], number=l['number']) for l in CATALOG['lessons'].values()]


def environment_view(status):
    keys = ('ok', 'state', 'lesson', 'observed', 'bundle', 'ready', 'recovery', 'runtime', 'check_running',
            'capabilities', 'error', 'message')
    env = {k: status.get(k) for k in keys}
    failure = status.get('failure') or {}
    env['failure'] = dict(stage=failure.get('stage')) if failure else None
    env['bundles_current'] = {pack: status.get('installed', {}).get(pack) == entry['bundle']['revision']
                              for pack, entry in CATALOG['packs'].items()}
    return env


# ─────────────────────────────── requests ───────────────────────────────

def _expected(body):
    expected = body.get('expected') if isinstance(body, dict) else None
    if not isinstance(expected, dict) or set(expected) != {'incarnation', 'generation'}:
        raise progress.Refused('invalid_request', '현재 환경 토큰이 필요합니다. 상태를 새로 확인하세요.', 400)
    gen = expected['generation']
    if not isinstance(expected['incarnation'], str) or not UUID_RE.fullmatch(expected['incarnation']) \
            or not isinstance(gen, int) or isinstance(gen, bool) or gen < 0:
        raise progress.Refused('invalid_request', '현재 환경 토큰 형식이 올바르지 않습니다.', 400)
    return dict(incarnation=expected['incarnation'], generation=gen)


def _request_id(body):
    value = body.get('request_id') if isinstance(body, dict) else None
    if not isinstance(value, str) or not UUID_RE.fullmatch(value):
        raise progress.Refused('invalid_request', '요청 ID 형식이 올바르지 않습니다.', 400)
    return value


def runner_request(action, lesson, step_id, request_id, expected):
    bundle = CATALOG['packs'][lesson['pack']]['bundle']['revision']
    request = dict(protocol=1, request_id=request_id, action=action, expected=expected)
    if action == 'start':
        request.update(target_lesson=lesson['id'], expected_bundle=bundle)
    elif action == 'stop':
        request.update(lesson=lesson['id'])
    else:
        request.update(lesson=lesson['id'], expected_bundle=bundle)
    if action == 'check':
        request['step'] = step_id
    return request


def submit(account, action, lesson, step_id, body):
    """Admit a job (deduplicated by request_id) and run it in the background. Returns (http, obj)."""
    request_id, expected = _request_id(body), _expected(body)
    if action == 'check':
        step = step_of(lesson, step_id)
        if not step or step['kind'] != 'task':
            raise progress.Refused('invalid_request', '확인할 수 없는 단계입니다.', 400)
    instance = CLIENT.instance_for(account)
    status = CLIENT.status(account)           # 410 for VMs without a runner, before anything is queued
    if action not in status.get('capabilities', []):
        raise AgentError('unsupported_action', '이 실습 환경은 그 동작을 지원하지 않습니다.')
    request = runner_request(action, lesson, step_id, request_id, expected)
    job, created = progress.admit(account, request_id, action, lesson['id'], step_id, expected, instance)
    if created:
        _POOL.submit(_run, account, instance, lesson, request, job['job_id'])
    elif job['state'] not in progress.ACTIVE:
        stored = progress.job(account, job['job_id'])
        return 200, dict(ok=True, job_id=job['job_id'], state=stored['state'], result=stored['result'])
    return 202, dict(ok=True, job_id=job['job_id'], state=job['state'])


def _run(account, instance, lesson, request, job_id):
    progress.set_job(job_id, 'running')
    try:
        response = CLIENT.call(account, request)
    except AgentError as exc:
        progress.set_job(job_id, 'failed', dict(ok=False, error=exc.code, message=exc.message), exc.http)
        return
    except Exception as exc:
        log(f'학습 팩 작업 실패 {job_id}: {exc}')
        progress.set_job(job_id, 'failed', dict(ok=False, error='runner_error', message='작업을 처리하지 못했습니다.'), 502)
        return
    try:
        pack_revision = CATALOG['packs'][lesson['pack']]['revision']
        if request['action'] == 'check':
            outcome = progress.record_check(account, instance, lesson, request, response, job_id)
            summary = dict(status=outcome['status'], stale=outcome['stale'], completed_now=outcome['completed_now'])
        else:
            progress.record_change(account, instance, lesson, request, response, job_id, pack_revision, shared(account))
            summary = {}
    except Exception as exc:
        log(f'학습 팩 진행 기록 실패 {job_id}: {exc}')
        summary = dict(record_error=True)
    error = response.get('error')
    result = dict(ok=response['ok'], error=error, message=response.get('message'),
                  runner=dict(state=response.get('state'), lesson=response.get('lesson'),
                              observed=response.get('observed'), executed=response.get('executed'),
                              noop=response.get('noop'), stage=response.get('stage'),
                              result=response.get('result')),
                  progress=summary)
    progress.set_job(job_id, 'done' if response['ok'] else 'failed', result,
                     200 if response['ok'] else HTTP.get(error, 409))


# ─────────────────────────────── entry points ───────────────────────────────

def _fail(exc):
    return exc.http, dict(ok=False, error=exc.code, message=exc.message)


def handle_get(account, seg):
    try:
        if seg == ['lesson', 'catalog']:
            return 200, dict(ok=True, lessons=public_catalog())
        if seg == ['lesson', 'state']:
            return 200, state(account)
        if len(seg) == 3 and seg[1] == 'jobs':
            found = progress.job(account, seg[2]) if re.fullmatch(r'[0-9a-f]{32}', seg[2]) else None
            if not found:
                return 404, dict(ok=False, message='작업을 찾을 수 없습니다.')
            return 200, dict(ok=True, job_id=found['job_id'], action=found['action'], state=found['state'],
                             http_status=found['http_status'], result=found['result'])
        if len(seg) == 3:
            lesson = lesson_of(seg[1], seg[2])
            if not lesson:
                return 404, dict(ok=False, message='없는 실습입니다.')
            return 200, dict(ok=True, lesson=public_lesson(lesson))
    except (AgentError, progress.Refused) as exc:
        return _fail(exc)
    return 404, dict(ok=False, message='없는 경로')


def state(account):
    status = CLIENT.status(account, fresh=True)
    instance = CLIENT.instance_for(account)
    attempt = progress.sync(account, instance, status)
    lesson = CATALOG['lessons'].get(status.get('lesson') or '') or (
        CATALOG['lessons'].get(attempt['lesson_id']) if attempt else None)
    view = progress.view(account, instance, lesson) if lesson else None
    return dict(ok=True, environment=environment_view(status), catalog=public_catalog(),
                attempt=view, shared=shared(account), jobs=progress.active_jobs(account))


def handle_post(account, seg, body):
    try:
        if len(seg) == 4 and seg[3] in progress.CHANGE_ACTIONS:
            lesson = lesson_of(seg[1], seg[2])
            if not lesson:
                return 404, dict(ok=False, message='없는 실습입니다.')
            return submit(account, seg[3], lesson, None, body)
        if len(seg) == 6 and seg[3] == 'step':
            lesson = lesson_of(seg[1], seg[2])
            step = step_of(lesson, seg[4]) if lesson else None
            if not step:
                return 404, dict(ok=False, message='없는 단계입니다.')
            action = seg[5]
            if action == 'check':
                return submit(account, 'check', lesson, step['id'], body)
            instance = CLIENT.instance_for(account)
            if action == 'view' and step['kind'] == 'read':
                attempt = progress.record_view(account, instance, lesson, step['id'])
                return 200, dict(ok=True, completed_at=attempt['completed_at'])
            if action == 'hint' and step['kind'] == 'task':
                n = body.get('n') if isinstance(body, dict) else None
                hints = step.get('hints') or []
                if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(hints):
                    return 400, dict(ok=False, message='없는 힌트입니다.')
                opened = progress.record_hint(account, instance, lesson, step['id'], n)
                return 200, dict(ok=True, n=n, text=hints[n - 1], opened=opened, total=len(hints))
            return 404, dict(ok=False, message='없는 동작입니다.')
    except (AgentError, progress.Refused) as exc:
        return _fail(exc)
    return 404, dict(ok=False, message='없는 경로')


def startup():
    """Called once by labd: jobs from a previous process are marked interrupted, never re-run."""
    try:
        progress.interrupt_unfinished()
    except Exception as exc:
        log(f'학습 팩 작업 정리 실패: {exc}')

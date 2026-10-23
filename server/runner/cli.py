"""Terminal front-end of the runner, used by the shell `lab` command on runner-equipped VMs.

    netlab-runner --cli resolve <name> [variant]   print the lesson id `lab <name>` maps to (exit 3: none)
    netlab-runner --cli start <name> [variant]
    netlab-runner --cli reset | stop | status
    netlab-runner --cli check                      reference only: never recorded as learning progress
    netlab-runner --cli release                    stop the runner's lesson before a legacy `lab` deploy

Tokens come from the local status just before each request (contract: CLI는 바로 앞의 로컬 status에서
토큰을 얻습니다), so the CLI shares the web's locks, generations and cancellation rules.
"""
import json
import os
import uuid

from . import store
from .executor import Runner

NOT_A_LESSON = 3
STATE_TEXT = {'idle': '실습 없음', 'running': '실행 중', 'start_failed': '시작 실패', 'reset_failed': '초기화 실패',
              'stop_failed': '종료 실패', 'recovery_required': '복구 필요', 'recovering': '복구 확인 중'}


def resolve(runner, name, variant=None):
    """Map `lab <name> [variant]` to an installed lesson the way the shell command picks topologies:
    the first lesson directory starting with <name>, then the definition file containing <variant>,
    else the alphabetically first definition."""
    candidates = []
    for pack, (_, recipe) in sorted(runner.installed().items()):
        for lesson_id, ldef in recipe.get('lessons', {}).items():
            base = os.path.basename(ldef['dir'].rstrip('/'))
            if base.startswith(name) or lesson_id.partition('/')[2] == name:
                candidates.append((base, ldef.get('definition') or '', lesson_id))
    if not candidates:
        return None
    first_dir = sorted({c[0] for c in candidates})[0]
    in_dir = sorted(c for c in candidates if c[0] == first_dir)
    if variant:
        matching = [c for c in in_dir if variant.lower() in c[1].lower() or c[2].endswith(variant)]
        return matching[0][2] if matching else None
    return in_dir[0][2]


def _send(runner, request):
    request = dict(protocol=1, **request)
    if request['action'] != 'status':
        request['request_id'] = str(uuid.uuid4())
    return runner.handle(json.dumps(request))


def _status(runner):
    return _send(runner, {'action': 'status'})


def _explain(response, done):
    if response.get('ok'):
        if response.get('noop'):
            return '▶ ' + (response.get('message') or '이미 실행 중입니다.')
        return '▶ ' + done
    return '✗ ' + (response.get('message') or response.get('error') or '실패했습니다.')


def main(args):
    runner = Runner(store.Paths())
    command = args[0] if args else 'status'
    if command == 'resolve':
        found = resolve(runner, args[1], args[2] if len(args) > 2 else None) if len(args) > 1 else None
        if not found:
            return NOT_A_LESSON
        print(found)
        return 0
    status = _status(runner)
    if command == 'status':
        if not status.get('ok'):
            print('✗ ' + (status.get('message') or '상태를 확인할 수 없습니다.'))
            return 1
        print(f"{STATE_TEXT.get(status['state'], status['state'])} · {status.get('lesson') or '-'} · 세대 {status['observed']['generation']}")
        return 0
    if not status.get('ok') or not status.get('observed'):
        print('✗ ' + (status.get('message') or '실습 환경 상태를 확인할 수 없습니다. 웹 패널의 복구 안내를 따르세요.'))
        return 1
    token = status['observed']
    lesson = status.get('lesson')
    if command == 'start':
        name = args[1] if len(args) > 1 else ''
        target = resolve(runner, name, args[2] if len(args) > 2 else None)
        if not target:
            print(f'✗ 단계형 실습으로 등록되지 않은 실습입니다: {name}')
            return NOT_A_LESSON
        bundle = runner.lesson_def(target)[1]
        print(f'▶ 실습 시작: {target}')
        response = _send(runner, dict(action='start', expected=token, target_lesson=target, expected_bundle=bundle))
        print(_explain(response, '준비됐습니다. 웹 패널에서 단계와 확인 버튼을 쓸 수 있습니다.'))
        return 0 if response.get('ok') else 1
    if command in ('reset', 'stop', 'release'):
        if not lesson:
            if command == 'release':
                return 0
            print('✗ 단계형 실습이 실행 중이 아닙니다.')
            return 1
        action = 'stop' if command == 'release' else command
        request = dict(action=action, expected=token, lesson=lesson)
        if action == 'reset':
            request['expected_bundle'] = runner.lesson_def(lesson)[1] or ''
        response = _send(runner, request)
        print(_explain(response, {'reset': '처음 상태로 다시 만들었습니다.', 'stop': '실습을 종료했습니다.'}[action]))
        return 0 if response.get('ok') else 1
    if command == 'check':
        if status['state'] != 'running' or not lesson:
            print('✗ 단계형 실습이 실행 중일 때만 확인할 수 있습니다.')
            return 1
        ldef, bundle = runner.lesson_def(lesson)
        steps = list((ldef or {}).get('steps', {}))
        if not steps:
            print('✗ 이 실습에는 확인할 단계가 없습니다.')
            return 1
        print('※ 참고용 검증 결과이며 학습 진행에는 반영되지 않습니다. 기록은 웹 패널의 확인 버튼으로 남습니다.')
        code = 0
        for step in steps:
            response = _send(runner, dict(action='check', expected=token, lesson=lesson,
                                          expected_bundle=bundle, step=step))
            if not response.get('ok'):
                print('✗ ' + (response.get('message') or response.get('error') or '확인하지 못했습니다.'))
                return 1
            result = response['result']
            mark = {'pass': '✔ 통과', 'fail': '… 아직 아님', 'unverifiable': '? 판정 불가', 'error': '! 검증 오류',
                    'cancelled': '- 취소됨'}.get(result['status'], result['status'])
            print(f'{step}: {mark}')
            for item in result.get('items', []):
                if item.get('message'):
                    print('   ' + item['message'])
            code = code or (0 if result['status'] == 'pass' else 1)
        return code
    print('사용법: netlab-runner --cli resolve|start|reset|stop|check|status|release')
    return 2

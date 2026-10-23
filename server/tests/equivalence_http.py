#!/usr/bin/env python3
"""labd HTTP 동등성 검사 — 리팩토링 전후 응답을 그대로 비교한다.

두 가지 방식으로 쓴다.

  (1) 로컬 섀도 비교: 두 소스 트리의 labd 를 각각 띄워(LXD 없이, 고정 fixture) 같은 요청을 보내고 diff.
      python3 tests/equivalence_http.py local <old_server_dir> <new_server_dir>

  (2) 실서버 스냅샷: nginx :8088 뒤의 서비스에 guest 로 요청해 응답을 JSON 으로 저장하고, 두 스냅샷을 비교.
      python3 tests/equivalence_http.py snapshot http://HOST:8088 before.json
      ... 배포 ...
      python3 tests/equivalence_http.py snapshot http://HOST:8088 after.json
      python3 tests/equivalence_http.py diff before.json after.json

guest(open 모드) 기준이다. 시작/정지/파기처럼 VM 을 건드리는 동작은 보내지 않는다.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SECRET = 'equivalence-proxy-secret'
CLIENT = 'ab' * 16
JSON = {'Content-Type': 'application/json', 'Origin': 'http://127.0.0.1', 'Host': '127.0.0.1'}

# (method, path, headers, body)  — 사용자(guest) 경로는 nginx 가 /u/guest/ctl/ 접두사를 떼고 넘기므로
# 로컬 모드에서는 접두사 없이, 스냅샷 모드에서는 접두사를 붙여 보낸다.
USER_REQUESTS = [
    ('GET', '', {}, None),
    ('GET', 'panel', {}, None),
    ('GET', 'status', {}, None),
    ('GET', 'sessions', {}, None),
    ('GET', 'cast?session=none', {}, None),
    ('GET', 'timeline?session=none', {}, None),
    ('GET', 'assets/theme.js', {}, None),
    ('GET', 'assets/tutor.css', {}, None),
    ('GET', 'assets/community-presence.js', {}, None),   # 사용자 경로에서는 404 여야 한다
    ('GET', 'assets/../labd.py', {}, None),
    ('GET', 'guide/nonexistent', {}, None),
    ('GET', 'nope', {}, None),
    ('POST', 'notice', JSON, {'client': CLIENT}),
    ('POST', 'notice', {'Content-Type': 'text/plain', 'Origin': 'http://127.0.0.1', 'Host': '127.0.0.1'}, {'client': CLIENT}),
    ('POST', 'notice', dict(JSON, Origin='http://evil.example'), {'client': CLIENT}),
    ('POST', 'community', JSON, {'client': CLIENT, 'action': 'poll'}),
    ('POST', 'community', JSON, {'client': 'short', 'action': 'poll'}),
    ('POST', 'community', JSON, {'client': CLIENT, 'action': 'ticket'}),
    ('POST', 'tutor', JSON, {'action': 'models'}),
    ('POST', 'tutor', JSON, {'action': 'history'}),
    ('POST', 'help', JSON, {}),
    ('POST', 'nope', JSON, {}),
]
ADMIN_REQUESTS = [   # open 모드에서는 nginx 가 /admin 을 403 으로 막는다. 로컬 모드에서만 직접 확인.
    ('GET', 'admin/', {}, None),
    ('GET', 'admin/list', {}, None),
    ('GET', 'admin/assets/player.js', {}, None),
    ('GET', 'admin/assets/tutor.js', {}, None),
    ('GET', 'admin/sessions?user=guest', {}, None),
    ('POST', 'admin/stop', JSON, {'user': '../x'}),
]


def fetch(base, method, path, headers, body):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status, payload, ctype = r.status, r.read(), r.headers.get('Content-Type', '')
    except urllib.error.HTTPError as e:
        status, payload, ctype = e.code, e.read(), e.headers.get('Content-Type', '')
    text = payload.decode('utf-8', 'replace')
    # 시각·난수가 섞이는 필드는 정규화한다.
    if 'json' in ctype:
        try:
            obj = json.loads(text)
            for key in ('expires', 'created', 'online', 'me', 'session', 'ticket', 'messages'):
                if isinstance(obj, dict) and key in obj:
                    obj[key] = '<volatile>'
            text = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        except ValueError:
            pass
    return {'status': status, 'type': ctype, 'body': text if len(text) < 200000 else f'<{len(text)} bytes sha>'}


def snapshot(base, prefix, headers_extra=None, include_admin=False):
    out = {}
    reqs = USER_REQUESTS + (ADMIN_REQUESTS if include_admin else [])
    for method, path, headers, body in reqs:
        full = path if path.startswith('admin/') else prefix + path
        h = dict(headers)
        h.update(headers_extra or {})
        out[f'{method} {full} {json.dumps(body, sort_keys=True) if body else ""}'] = fetch(base, method, full, h, body)
    return out


def diff(a, b):
    bad = 0
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            bad += 1
            print(f'✗ {key}')
            for field in ('status', 'type', 'body'):
                if a.get(key, {}).get(field) != b.get(key, {}).get(field):
                    print(f'    {field}: {str(a.get(key, {}).get(field))[:300]!r}')
                    print(f'      -> {str(b.get(key, {}).get(field))[:300]!r}')
    print(f'{len(a)} 요청 비교, 차이 {bad}개')
    return bad


RUNNER = r'''
import json, os, sys, importlib.util, threading, time
from http.server import ThreadingHTTPServer
server_dir, port, tmp = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sys.path.insert(0, server_dir)
os.chdir(server_dir)
spec = importlib.util.spec_from_file_location('labd', os.path.join(server_dir, 'labd.py'))
labd = importlib.util.module_from_spec(spec); spec.loader.exec_module(labd)
import community_chat, notice_board, tutor_chat
community_chat.DB = tmp + '/community.sqlite3'; notice_board.DB = tmp + '/notice.sqlite3'; tutor_chat.DB = tmp + '/tutor.sqlite3'
state = {'users': {'guest': {'instance': 'netlab-fixture00', 'ip': '10.200.0.20', 'status': 'running',
                              'created': '2026-10-22T00:00:00+00:00', 'last_activity': '2026-10-22T00:00:00+00:00', 'stopped_at': None}}, 'pool': []}
if hasattr(labd, 'store'):
    s = labd.store
    s.PROXY_SECRET = %(secret)r; s.CONFIG['auth_mode'] = 'open'
    s.USERS.clear(); s.STATE.clear(); s.STATE.update(state)
    s.save_state = lambda: None
    import netlab.instances, netlab.recordings, netlab.paths
    netlab.paths.RECORD_ROOT = tmp + '/rec'
    labd.save_state = lambda: None
else:
    labd.PROXY_SECRET = %(secret)r; labd.CONFIG['auth_mode'] = 'open'
    labd.USERS = {}; labd.STATE = state
    labd.save_state = lambda: None
    labd.RECORD_ROOT = tmp + '/rec'
    labd.ASSET_DIR = os.path.join(server_dir, 'assets')   # 옛 코드는 설치 경로 고정; 호스트에서는 동일
    labd._run = lambda cmd, timeout=10: (1, '', 'no lxd in equivalence run')
import netlab_tutor if False else None
'''


def runner_source(secret):
    src = RUNNER % {'secret': secret}
    src = src.replace("import netlab_tutor if False else None\n", '')
    src += r'''
# 실제 LXD 호출은 없어야 한다. 있으면 실패로 보이게 막는다.
try:
    import netlab_tutor
    netlab_tutor.deployed_chapter = lambda inst: None
except Exception:
    pass
try:
    import netlab.procs
    netlab.procs.lxc = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('lxc called'))
except Exception:
    pass
ThreadingHTTPServer(('127.0.0.1', port), labd.Handler).serve_forever()
'''
    return src


def local(old_dir, new_dir):
    with tempfile.TemporaryDirectory() as tmp:
        procs, bases = [], []
        for i, d in enumerate((old_dir, new_dir)):
            port = 18500 + i
            sub = os.path.join(tmp, f'srv{i}'); os.makedirs(sub)
            p = subprocess.Popen([sys.executable, '-c', runner_source(SECRET), os.path.abspath(d), str(port), sub],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            procs.append(p); bases.append(f'http://127.0.0.1:{port}/')
        time.sleep(1.5)
        for p in procs:
            if p.poll() is not None:
                print(p.stderr.read()); raise SystemExit('서버 기동 실패')
        hdr = {'X-Lab-Proxy-Auth': SECRET, 'X-Lab-User': 'guest', 'X-Lab-Admin': 'admin'}
        try:
            a = snapshot(bases[0], '', hdr, include_admin=True)
            b = snapshot(bases[1], '', hdr, include_admin=True)
            # 미인증(시크릿 없음) 요청도 같은 답이어야 한다.
            a.update({'noauth ' + k: v for k, v in snapshot(bases[0], '', {}, include_admin=True).items()})
            b.update({'noauth ' + k: v for k, v in snapshot(bases[1], '', {}, include_admin=True).items()})
        finally:
            for p in procs:
                p.kill()
        return diff(a, b)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'local' and len(sys.argv) == 4:
        raise SystemExit(1 if local(sys.argv[2], sys.argv[3]) else 0)
    if mode == 'snapshot' and len(sys.argv) == 4:
        base = sys.argv[2].rstrip('/') + '/'
        data = snapshot(base, 'u/guest/ctl/', {'Origin': base.rstrip('/'), 'Host': base.split('//', 1)[1].rstrip('/')})
        with open(sys.argv[3], 'w') as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f'{len(data)} 응답 저장: {sys.argv[3]}')
        return
    if mode == 'diff' and len(sys.argv) == 4:
        raise SystemExit(1 if diff(json.load(open(sys.argv[2])), json.load(open(sys.argv[3]))) else 0)
    print(__doc__)
    raise SystemExit(2)


if __name__ == '__main__':
    main()

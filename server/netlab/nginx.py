"""계정·인스턴스 현황에 맞춰 nginx 스니펫을 다시 만들고 reload 한다."""
import os
import shutil
import subprocess
import time

from . import paths, store
from .util import log, write_atomic

WS_LOCATION = '''
    # Login is verified by a short-lived labd grant in the first WS frame.
    location = /community/ws {
        proxy_pass http://127.0.0.1:9001/community/ws;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 75s;
        proxy_send_timeout 75s;
        proxy_buffering off;
    }
'''


def _guard(mode, ht, user):
    if mode == "open":
        return ""
    if mode == "lan":
        return ('        auth_basic           "네트워크 실습 환경";\n'
                f'        auth_basic_user_file {ht};')
    # Pass the statically generated location owner in the internal subrequest.
    # Never derive authorization from $request_uri: nginx selects locations using
    # a decoded/normalized URI while $request_uri retains the original spelling.
    return f"        auth_request /_access?target={user};"


def render(mode, users):
    """
    인증 방식만 다르고 나머지는 같은 블록을 만든다.
      open   : 공용 guest — 인증 없음
      lan    : 사내망 직결 — htpasswd
      tunnel : Cloudflare Access — JWT 검증(auth_request)
    경로 권한(누가 누구 환경에 접근 가능한가)은 nginx 조건문에 흩뿌리지 않고
    labd 의 /_access 한 곳에서만 판정한다.
    """
    out = [WS_LOCATION]
    for u in users:
        if mode == "open" and u != "guest":
            continue
        ht = os.path.join(paths.HTPASSWD_DIR, u)
        # 비밀번호가 없는 계정(Access 로 자동 생성된 계정)은 사내망 경로에 노출하지 않는다.
        if mode == "lan" and not os.path.exists(ht):
            continue
        rec = store.STATE["users"].get(u) or {}
        # 실행 중일 때만 터미널로 연결한다. 정지·발급중인 인스턴스의 IP 로 프록시하면
        # 죽은 주소에 매달려 응답이 멈춘다.
        ip = rec.get("ip") if rec.get("status") == "running" else None
        guard = _guard(mode, ht, u)

        out.append(f"""
    location = /u/{u} {{ return 301 /u/{u}/ctl/; }}
    location = /u/{u}/ {{ return 301 /u/{u}/ctl/; }}

    location /u/{u}/ctl/ {{
{guard}
        proxy_pass http://127.0.0.1:9000/;
        proxy_set_header X-Lab-User "{u}";
        proxy_set_header X-Lab-Admin "";
        proxy_set_header X-Lab-Proxy-Auth "{store.PROXY_SECRET}";
        proxy_set_header Host       $host;
        proxy_read_timeout 300s;
    }}""")

        if ip:
            out.append(f"""
    location /u/{u}/term/ {{
{guard}
        proxy_pass http://{ip}:7681/;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host       $host;
        # 터미널 세션은 길게 유지하되, 백엔드가 죽어 있으면 즉시 실패해야 한다.
        proxy_connect_timeout 5s;
        proxy_read_timeout  86400s;
        proxy_send_timeout  86400s;
        proxy_buffering off;
    }}""")
        else:
            # 환경이 없을 때 터미널 주소로 들어오면 패널로 돌려보낸다.
            out.append(f"""
    location /u/{u}/term/ {{ return 302 /u/{u}/ctl/; }}""")
    return "".join(out) + "\n"


def regen_routing():
    """스니펫 두 벌(lan/open, tunnel)을 다시 쓰고 nginx 를 reload 한다. 성공하면 True."""
    users = store.accounts()

    # 단일 진입점(/) 로그인용 통합 htpasswd
    merged = []
    for u in users:
        try:
            with open(os.path.join(paths.HTPASSWD_DIR, u)) as f:
                merged.append(f.read().strip())
        except OSError:
            pass
    write_atomic(paths.HTPASSWD_ALL, "\n".join(merged) + "\n", 0o640)
    try:
        shutil.chown(paths.HTPASSWD_ALL, group="www-data")   # nginx 워커가 읽어야 한다
    except (LookupError, PermissionError, OSError):
        pass

    # 스니펫에 프록시 시크릿이 박히므로 일반 사용자가 못 읽게 640 으로 쓴다.
    lan_mode = "open" if store.CONFIG.get("auth_mode") == "open" else "lan"
    write_atomic(paths.NGINX_LAN, render(lan_mode, users), 0o640)
    write_atomic(paths.NGINX_TUNNEL, render("tunnel", users), 0o640)

    rc = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if rc.returncode != 0:
        log(f"nginx 설정 오류로 reload 중단: {rc.stderr.strip()[:300]}")
        return False
    subprocess.run(["systemctl", "reload", "nginx"], capture_output=True)
    # reload 는 신호만 보내고 즉시 끝난다. 발급이 1초 미만이라 이 틈에 터미널
    # 주소로 들어가면 옛 설정에 걸리므로 새 워커가 뜰 시간을 잠깐 준다.
    time.sleep(1)
    return True

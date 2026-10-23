#!/usr/bin/env python3
"""
netlab — 온디맨드 실습 환경 발급 서비스.

동작 원리
  · 계정은 htpasswd 파일로 관리한다(관리자가 직접 추가). 계정이 곧 신원이다.
  · 실습 환경(VM)은 미리 만들어 두지 않는다. 본인이 "시작"을 누를 때 발급된다.
  · 체감 대기를 없애려고 예비 VM 을 미리 부팅해 두고(웜 풀), 요청이 오면 즉시 배정한 뒤
    백그라운드로 다시 채운다.
  · "종료"는 파기가 아니라 정지다. 작업 내용이 남고, 다시 시작하면 이어서 쓴다.
  · 정지된 채 reclaim_days(기본 7일)가 지나면 자동 회수(파기)한다.
    실행 중인 환경은 자동으로 파기하지 않는다 — 쓰고 있는 사람의 작업을 지우지 않기 위해서다.

신원
  nginx 가 basic auth 통과 후 X-Lab-User / X-Lab-Admin 헤더를 덮어써서 넘긴다.
  proxy_set_header 는 클라이언트가 보낸 동명 헤더를 대체하므로 위조할 수 없다.
  이 서비스는 127.0.0.1 에만 바인딩되어 nginx 를 거치지 않으면 도달할 수 없다.
"""
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
import tutor_chat
import community_chat
import community_auth
import notice_board
import guide_view
import knowledge
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

LISTEN = ("127.0.0.1", 9000)

CONFIG_PATH = "/etc/netlab/config.json"
STATE_PATH = "/var/lib/netlab/state.json"
USERS_PATH = "/var/lib/netlab/users.json"
HTPASSWD_DIR = "/etc/netlab/htpasswd"
HTPASSWD_ALL = "/etc/netlab/htpasswd.all"
NGINX_LAN = "/etc/nginx/netlab.d/lan-users.conf"       # 사내망 직결 — basic auth
NGINX_TUNNEL = "/etc/nginx/netlab.d/tunnel-users.conf"  # 터널 — Cloudflare Access

# nginx 만 아는 공유 시크릿. :9000 은 loopback 전용이지만, 그것만으로는
# "호스트에서 셸을 가진 아무 프로세스나 X-Lab-Admin 헤더를 위조해 관리자가 되는" 걸
# 못 막는다(신뢰가 헤더 유무 한 겹뿐). nginx 는 인증을 통과한 요청에만 이 시크릿을
# 실어 보내고, labd 는 시크릿이 맞는 요청의 신원 헤더만 신뢰한다.
PROXY_SECRET = ""


def load_proxy_secret():
    global PROXY_SECRET
    try:
        with open("/etc/netlab/proxy.secret") as f:
            PROXY_SECRET = f.read().strip()
    except Exception as e:
        PROXY_SECRET = ""
        log(f"프록시 시크릿 읽기 실패 — 신원 헤더 신뢰 불가(fail-closed): {e}")

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")

DEFAULTS = {
    "image": "netlab",
    "profile": "default",  # 기존 설치 호환; 신규 bootstrap은 전용 netlab 프로필 사용
    "cpu": 2,
    "memory": "4GiB",
    "disk": "50GiB",
    "ip_prefix": "10.200.0.",
    "ip_first": 20,
    "ip_last": 199,
    "pool_size": 1,          # 미리 데워 둘 예비 VM 수
    "reclaim_days": 7,       # 정지된 채 이만큼 지나면 파기
    "idle_stop_hours": 24,   # 실행 중이나 이만큼 접속(패널 열림)이 없으면 자동 일시정지. 0=끔
    "ready_timeout": 300,    # 발급 후 터미널이 뜰 때까지 기다리는 한계(초)

    # ── Cloudflare Access 연동 ────────────────────────────────
    # 누가 들어올 수 있는지는 Cloudflare 쪽 allow list 가 정한다.
    # 여기서는 "통과한 사람이 진짜인지" 만 암호학적으로 검증한다.
    "access_team_domain": "",  # /etc/netlab/config.json 에 실제 Access 팀 도메인 지정
    "access_aud": "",        # Application Audience (AUD) 태그 — 비우면 Access 로그인 비활성
    "access_auto_create": True,   # 처음 들어온 사람의 계정을 자동 생성
    "lan_listen": "",        # 사내망 직결 리스너 주소 (비우면 자동 감지)
    "auth_mode": "protected",  # 기존 설치는 인증 유지; 신규 설치는 open 명시
}

LOCK = threading.RLock()
STATE = {"users": {}, "pool": []}
CONFIG = dict(DEFAULTS)


# ─────────────────────────────── 유틸 ───────────────────────────────

def now():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or now()).isoformat()


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def log(msg):
    print(f"[{iso()}] {msg}", flush=True)


def lxc(*args, timeout=600):
    """쉘을 거치지 않고 lxc 실행 — 인자 주입이 불가능하다."""
    p = subprocess.run(["lxc", *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def write_atomic(path, content, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def load_config():
    global CONFIG
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        write_atomic(CONFIG_PATH, json.dumps(cfg, indent=2, ensure_ascii=False))
    except Exception as e:
        log(f"설정 읽기 실패, 기본값 사용: {e}")
    CONFIG = cfg


def save_config():
    write_atomic(CONFIG_PATH, json.dumps(CONFIG, indent=2, ensure_ascii=False))


def load_state():
    global STATE
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
        STATE = {"users": s.get("users", {}), "pool": s.get("pool", [])}
    except FileNotFoundError:
        STATE = {"users": {}, "pool": []}
    except Exception as e:
        log(f"상태 읽기 실패, 초기화: {e}")
        STATE = {"users": {}, "pool": []}


def save_state():
    write_atomic(STATE_PATH, json.dumps(STATE, indent=2, ensure_ascii=False), 0o600)


# ────────────────────────── 사용자 테이블 ──────────────────────────
#
# 누가 들어올 수 있는지는 Cloudflare allow list 가 정한다.
# 여기 테이블은 "들어온 사람이 우리 시스템에서 무엇인지"(역할·차단 여부)를 관리한다.

USERS = {}


# 사용자 테이블을 못 읽으면 인증을 잠근다(fail-closed).
# 예전엔 실패 시 빈 테이블로 떨어졌는데, 그러면 파일이 손상·삭제된 순간 테이블이 비고,
# "관리자 없으면 처음 들어온 사람이 admin" 로직과 맞물려 다음 로그인 사용자가 관리자가 됐다.
# 즉 파일 장애가 권한 상승으로 뒤집혔다. 이제는 못 읽으면 아무도 통과시키지 않는다.
USERS_LOADED = False


def load_users():
    global USERS, USERS_LOADED
    try:
        with open(USERS_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("최상위가 객체가 아님")
        USERS = data
        USERS_LOADED = True
    except FileNotFoundError:
        # 아직 아무도 안 들어온 최초 상태 — 빈 테이블이 정상이다.
        USERS = {}
        USERS_LOADED = True
    except Exception as e:
        # 손상·파싱 실패 — 메모리의 기존 테이블을 유지하고 인증을 잠근다.
        log(f"사용자 테이블 읽기 실패 — fail-closed 로 잠금: {e}")
        USERS_LOADED = False


def save_users():
    write_atomic(USERS_PATH, json.dumps(USERS, indent=2, ensure_ascii=False), 0o600)


def user_role(account):
    return (USERS.get(account) or {}).get("role", "user")


def user_disabled(account):
    return bool((USERS.get(account) or {}).get("disabled"))


def account_from_email(email):
    """이메일 앞부분을 계정명으로 삼는다. 규칙에 안 맞는 문자는 걷어낸다."""
    local = email.split("@", 1)[0].lower()
    name = re.sub(r"[^a-z0-9_-]", "-", local).strip("-")
    if not name or not name[0].isalpha():
        name = "u" + name
    return name[:31]


def lookup_or_create(email):
    """
    이메일에 해당하는 계정을 찾고, 없으면 만든다.

    최초 로그인 자동 생성은 Cloudflare allow list 를 신뢰하는 전제 위에 있다.
    (아무나 여기 도달할 수 없다는 뜻이다)
    """
    # 테이블을 신뢰할 수 없으면 아무도 통과시키지 않는다(fail-closed).
    if not USERS_LOADED:
        log("사용자 테이블이 잠겨 있어 로그인 거부")
        return None, None

    with LOCK:
        for acct, rec in USERS.items():
            if (rec.get("email") or "").lower() == email.lower():
                rec["last_login"] = iso()
                save_users()
                return acct, rec

        if not CONFIG.get("access_auto_create", True):
            return None, None

        base = account_from_email(email)
        acct, n = base, 1
        while acct in USERS or os.path.exists(os.path.join(HTPASSWD_DIR, acct)):
            n += 1
            acct = f"{base}{n}"[:31]

        # 새 계정은 언제나 일반 사용자다. 관리자 승격은 사람이 명시적으로 한다
        # (netlab-user role). "관리자 없으면 자동 admin" 은 파일 장애 시 권한 상승으로
        # 뒤집히므로 제거했다.
        USERS[acct] = {"email": email, "role": "user", "created": iso(),
                       "last_login": iso(), "disabled": False}
        save_users()
        log(f"Access 최초 로그인 — 계정 생성: {acct} <{email}> 역할=user")
    regen_routing()
    return acct, USERS[acct]


# ────────────────────── Cloudflare Access JWT ──────────────────────
#
# 이메일 헤더만 믿으면 안 된다. nginx 는 0.0.0.0 에서 듣고 있어 누구나
#   curl -H 'Cf-Access-Authenticated-User-Email: admin@...'
# 로 위조할 수 있기 때문이다. Cloudflare 가 서명한 JWT 를 검증해야 안전하다.

_JWKS = {"keys": [], "fetched": 0}


def jwks():
    now_ts = time.time()
    if _JWKS["keys"] and now_ts - _JWKS["fetched"] < 3600:
        return _JWKS["keys"]
    import urllib.request
    url = f"https://{CONFIG['access_team_domain']}/cdn-cgi/access/certs"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.load(r)
    _JWKS["keys"] = data.get("keys", [])
    _JWKS["fetched"] = now_ts
    return _JWKS["keys"]


def verify_access_jwt(token):
    """서명·만료·발급자·대상(aud)까지 검증하고 이메일을 돌려준다. 실패하면 None."""
    # access_aud 는 문자열 하나 또는 리스트를 받는다. 여러 개면 그중 하나만 맞아도 통과.
    # (도메인마다 Access 애플리케이션이 다르면 AUD 도 다르므로 여러 개를 허용한다.
    #  같은 앱이 여러 도메인을 덮으면 AUD 는 하나뿐이라 리스트에 하나만 두면 된다.)
    raw = CONFIG.get("access_aud") or ""
    if isinstance(raw, str):
        auds = [a.strip() for a in raw.split(",") if a.strip()]
    else:
        auds = [str(a).strip() for a in raw if str(a).strip()]
    if not token or not auds:
        return None
    try:
        import jwt
        from jwt import PyJWK
        hdr = jwt.get_unverified_header(token)
        key = next((k for k in jwks() if k.get("kid") == hdr.get("kid")), None)
        if key is None:                      # 키 회전 직후일 수 있으니 한 번 새로 받는다
            _JWKS["fetched"] = 0
            key = next((k for k in jwks() if k.get("kid") == hdr.get("kid")), None)
        if key is None:
            return None
        claims = jwt.decode(
            token, PyJWK(key).key, algorithms=["RS256"], audience=auds,
            issuer=f"https://{CONFIG['access_team_domain']}",
        )
        # email 만 신원으로 인정한다. common_name 은 서비스 토큰·mTLS 경로에서 오는
        # 값이라 SSO 이메일과 신뢰 수준이 다르다 — fallback 으로 쓰면 안 된다.
        return claims.get("email")
    except Exception as e:
        log(f"Access 토큰 검증 실패: {e}")
        return None


# ─────────────────────────── 계정 / 라우팅 ───────────────────────────

def accounts():
    """실습 계정 = htpasswd 가 있거나(사내망 로그인) 사용자 테이블에 있는 것."""
    names = set()
    try:
        names |= {n for n in os.listdir(HTPASSWD_DIR) if NAME_RE.match(n)}
    except FileNotFoundError:
        pass
    names |= {n for n in USERS if NAME_RE.match(n)}
    if CONFIG.get("auth_mode") == "open":
        names.add("guest")
    return sorted(names)


def regen_routing():
    """계정·인스턴스 현황에 맞춰 nginx 스니펫을 다시 만들고 reload 한다."""
    users = accounts()

    # 단일 진입점(/) 로그인용 통합 htpasswd
    merged = []
    for u in users:
        try:
            with open(os.path.join(HTPASSWD_DIR, u)) as f:
                merged.append(f.read().strip())
        except OSError:
            pass
    write_atomic(HTPASSWD_ALL, "\n".join(merged) + "\n", 0o640)
    try:
        shutil.chown(HTPASSWD_ALL, group="www-data")   # nginx 워커가 읽어야 한다
    except (LookupError, PermissionError, OSError):
        pass

    # 인증 방식만 다르고 나머지는 같은 블록을 두 벌 만든다.
    #   lan    : 사내망 직결 — htpasswd
    #   tunnel : Cloudflare Access — JWT 검증(auth_request)
    # 경로 권한(누가 누구 환경에 접근 가능한가)은 nginx 조건문에 흩뿌리지 않고
    # labd 의 /_access 한 곳에서만 판정한다.
    def render(mode):
        out = ['''
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
''']
        for u in users:
            if mode == "open" and u != "guest":
                continue
            ht = os.path.join(HTPASSWD_DIR, u)
            # 비밀번호가 없는 계정(Access 로 자동 생성된 계정)은 사내망 경로에 노출하지 않는다.
            if mode == "lan" and not os.path.exists(ht):
                continue
            rec = STATE["users"].get(u) or {}
            # 실행 중일 때만 터미널로 연결한다. 정지·발급중인 인스턴스의 IP 로 프록시하면
            # 죽은 주소에 매달려 응답이 멈춘다.
            ip = rec.get("ip") if rec.get("status") == "running" else None

            if mode == "open":
                guard = ""
            elif mode == "lan":
                guard = ('        auth_basic           "네트워크 실습 환경";\n'
                         f'        auth_basic_user_file {ht};')
            else:
                guard = "        auth_request /_access;"

            out.append(f"""
    location = /u/{u} {{ return 301 /u/{u}/ctl/; }}
    location = /u/{u}/ {{ return 301 /u/{u}/ctl/; }}

    location /u/{u}/ctl/ {{
{guard}
        proxy_pass http://127.0.0.1:9000/;
        proxy_set_header X-Lab-User "{u}";
        proxy_set_header X-Lab-Admin "";
        proxy_set_header X-Lab-Proxy-Auth "{PROXY_SECRET}";
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

    # 스니펫에 프록시 시크릿이 박히므로 일반 사용자가 못 읽게 640 으로 쓴다.
    write_atomic(NGINX_LAN, render("open" if CONFIG.get("auth_mode") == "open" else "lan"), 0o640)
    write_atomic(NGINX_TUNNEL, render("tunnel"), 0o640)

    rc = subprocess.run(["nginx", "-t"], capture_output=True, text=True)
    if rc.returncode != 0:
        log(f"nginx 설정 오류로 reload 중단: {rc.stderr.strip()[:300]}")
        return False
    subprocess.run(["systemctl", "reload", "nginx"], capture_output=True)
    # reload 는 신호만 보내고 즉시 끝난다. 발급이 1초 미만이라 이 틈에 터미널
    # 주소로 들어가면 옛 설정에 걸리므로 새 워커가 뜰 시간을 잠깐 준다.
    time.sleep(1)
    return True


# ─────────────────────────── 인스턴스 관리 ───────────────────────────

def used_ips():
    ips = {r.get("ip") for r in STATE["users"].values() if r.get("ip")}
    ips |= {p.get("ip") for p in STATE["pool"] if p.get("ip")}
    return ips


def alloc_ip():
    taken = used_ips()
    for n in range(CONFIG["ip_first"], CONFIG["ip_last"] + 1):
        ip = f"{CONFIG['ip_prefix']}{n}"
        if ip not in taken:
            return ip
    return None


def instance_state(name):
    rc, out, _ = lxc("list", name, "--format", "json", timeout=60)
    if rc != 0:
        return None
    try:
        for i in json.loads(out):
            if i["name"] == name:
                return i["status"]
    except Exception:
        pass
    return None


def ttyd_ready(ip):
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://{ip}:7681/", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_lab_permissions(name):
    """Repair legacy images and sync shell helpers before exposing a terminal."""
    rc, _, _ = lxc('exec', name, '--', 'sh', '-c',
                   'getent group clab_admins >/dev/null || groupadd --system clab_admins; '
                   'id -nG lab | tr " " "\\n" | grep -qx clab_admins || usermod -aG clab_admins lab',
                   timeout=30)
    if rc != 0:
        return False
    rc, _, _ = lxc('file', 'push', '--mode=0644',
                   os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lab-shell.sh'),
                   name + '/etc/lab-shell.sh', timeout=30)
    return rc == 0


def create_instance():
    """새 VM 을 만들어 터미널이 응답할 때까지 기다린다. (name, ip) 반환."""
    with LOCK:
        ip = alloc_ip()
        if not ip:
            raise RuntimeError("배정 가능한 IP 가 없습니다")
        name = f"netlab-{uuid.uuid4().hex[:10]}"
        # IP 를 먼저 예약해 동시 생성 시 충돌을 막는다.
        STATE["pool"].append({"instance": name, "ip": ip, "ready": False,
                              "created": iso()})
        save_state()

    rc, _, err = lxc("init", CONFIG["image"], name, "--vm", "--profile", CONFIG["profile"],
                     "-c", f"limits.cpu={CONFIG['cpu']}",
                     "-c", f"limits.memory={CONFIG['memory']}",
                     "-d", f"root,size={CONFIG['disk']}")
    if rc != 0:
        _drop_pool(name)
        raise RuntimeError(f"생성 실패: {err}")

    rc, _, err = lxc("config", "device", "override", name, "eth0",
                     f"ipv4.address={ip}")
    if rc != 0:
        lxc("delete", name, "--force")
        _drop_pool(name)
        raise RuntimeError(f"IP 배정 실패: {err}")

    rc, _, err = lxc("start", name)
    if rc != 0:
        lxc("delete", name, "--force")
        _drop_pool(name)
        raise RuntimeError(f"기동 실패: {err}")
    enforce_isolation()      # 뜨자마자 격리 — 주기 대기 중 노출되는 틈을 없앤다

    deadline = time.time() + CONFIG["ready_timeout"]
    while time.time() < deadline:
        if ttyd_ready(ip) and ensure_lab_permissions(name):
            # 되돌리기 기준점. 이 이름이어야 "초기화"가 동작한다.
            lxc("snapshot", name, "clean")
            with LOCK:
                for p in STATE["pool"]:
                    if p["instance"] == name:
                        p["ready"] = True
                save_state()
            log(f"예비 환경 준비 완료: {name} ({ip})")
            return name, ip
        time.sleep(4)

    lxc("delete", name, "--force")
    _drop_pool(name)
    raise RuntimeError("준비 시간이 초과됐습니다")


def _drop_pool(name):
    with LOCK:
        STATE["pool"] = [p for p in STATE["pool"] if p["instance"] != name]
        save_state()


def claim_for(user):
    """웜 풀에 준비된 환경이 있으면 즉시 배정, 없으면 새로 만든다."""
    with LOCK:
        for p in list(STATE["pool"]):
            if p.get("ready"):
                if not ensure_lab_permissions(p['instance']):
                    p['ready'] = False
                    save_state()
                    continue
                STATE["pool"].remove(p)
                STATE["users"][user] = {
                    "instance": p["instance"], "ip": p["ip"],
                    "created": p.get("created", iso()),
                    "last_activity": iso(), "stopped_at": None,
                    "status": "running",
                }
                save_state()
                log(f"{user} ← 예비 환경 즉시 배정 {p['instance']}")
                regen_routing()
                return

    name, ip = create_instance()          # 풀이 비었으면 직접 생성
    with LOCK:
        STATE["pool"] = [p for p in STATE["pool"] if p["instance"] != name]
        STATE["users"][user] = {
            "instance": name, "ip": ip, "created": iso(),
            "last_activity": iso(), "stopped_at": None, "status": "running",
        }
        save_state()
    log(f"{user} ← 신규 환경 배정 {name}")
    regen_routing()


def start_for(user):
    """없으면 발급, 정지 상태면 재개."""
    with LOCK:
        rec = STATE["users"].get(user)
        if rec:
            rec["status"] = "starting"
            save_state()

    if not rec:
        claim_for(user)
        return

    name = rec["instance"]
    if instance_state(name) != "Running":
        rc, _, err = lxc("start", name)
        if rc != 0:
            with LOCK:
                rec["status"] = "error"
                rec["error"] = err
                save_state()
            return
        enforce_isolation()
    deadline = time.time() + CONFIG["ready_timeout"]
    ready = False
    while time.time() < deadline:
        if ttyd_ready(rec["ip"]) and ensure_lab_permissions(name):
            ready = True
            break
        time.sleep(4)
    with LOCK:
        rec["status"] = "running" if ready else "error"
        if not ready:
            rec['error'] = '터미널 또는 실습 계정 권한 준비 실패'
        rec["stopped_at"] = None
        rec["last_activity"] = iso()
        save_state()
    regen_routing()


RECORD_ROOT = "/var/lib/netlab/recordings"
VM_RECORD_DIR = "/var/log/lab-sessions"
ASSET_DIR = "/opt/netlab-admin/assets"

# 리플레이용 이름 검증 — 경로 탈출 차단. 계정명·세션ID 만 허용한다.
SESSION_RE = re.compile(r"^[0-9A-Za-z._-]+$")


def list_sessions(user):
    """한 계정의 저장된 세션 목록. (session_id, cast크기, 명령수) 를 최근순으로."""
    if not NAME_RE.match(user):
        return []
    d = os.path.join(RECORD_ROOT, user)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        if not fn.endswith(".cast"):
            continue
        sid = fn[:-5]
        cast = os.path.join(d, fn)
        cmdf = os.path.join(d, sid + ".commands.jsonl")
        ncmd = 0
        if os.path.isfile(cmdf):
            try:
                with open(cmdf) as f:
                    ncmd = sum(1 for ln in f if '"cmd"' in ln)
            except OSError:
                pass
        # 명령을 하나도 안 친 세션(터미널만 열고 닫음)은 목록에서 숨긴다.
        if ncmd == 0:
            continue
        out.append({"session": sid, "size": os.path.getsize(cast), "cmds": ncmd})
    out.sort(key=lambda r: r["session"], reverse=True)
    return out


def _safe_record_path(user, session, ext):
    """계정 폴더 안의 <session><ext> 절대경로. 벗어나면 None."""
    if not NAME_RE.match(user) or not SESSION_RE.match(session):
        return None
    base = os.path.realpath(os.path.join(RECORD_ROOT, user))
    path = os.path.realpath(os.path.join(base, session + ext))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return None
    return path


def read_cast(user, session):
    """검증된 .cast 파일 내용을 돌려준다. 경로가 벗어나면 None."""
    path = _safe_record_path(user, session, ".cast")
    if not path:
        return None
    with open(path, "rb") as f:
        return f.read()


def read_timeline(user, session):
    """
    명령 로그를 리플레이 타임라인으로 변환한다.

    .cast 의 시작 timestamp 를 기준으로 각 명령의 상대초(t)를 계산해, 플레이어가 그
    지점으로 seek 할 수 있게 한다. 명령 로그의 ts 는 명령이 '끝난' 시각(프롬프트가 다시
    그려질 때 기록)이라, seek 지점은 조금 앞(-3초)으로 줘서 명령 실행 장면부터 보이게 한다.
    """
    cast = _safe_record_path(user, session, ".cast")
    cmds = _safe_record_path(user, session, ".commands.jsonl")
    if not cast:
        return []
    try:
        with open(cast) as f:
            start = json.loads(f.readline()).get("timestamp", 0)
    except Exception:
        return []
    if not cmds:
        return []
    items = []
    try:
        with open(cmds) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = parse_iso(d.get("ts", ""))
                if not t:
                    continue
                off = t.timestamp() - start
                if "event" in d:
                    items.append({"t": max(0, off - 1), "kind": "event",
                                  "label": d.get("event", ""),
                                  "detail": d.get("detail", "")})
                elif "cmd" in d:
                    items.append({"t": max(0, off - 3), "kind": "cmd",
                                  "cmd": d.get("cmd", ""), "rc": d.get("rc", 0)})
    except OSError:
        return []
    items.sort(key=lambda x: x["t"])
    return items


# 게스트가 만드는 기록의 이름 규칙. 이것만 가져온다.
REC_NAME_RE = re.compile(r"^[0-9A-Za-z._-]+\.(cast|commands\.jsonl)$")
REC_MAX_FILE = 64 * 1024 * 1024      # 파일 하나 최대 64MB
REC_MAX_TOTAL = 512 * 1024 * 1024    # 한 번 회수에 최대 512MB
REC_MAX_FILES = 500                  # 파일 개수 상한


def collect_recordings(user, instance):
    """
    세션 기록을 호스트로 가져온다.

    예전엔 게스트가 만든 tar 를 호스트 root 가 통째로 메모리에 받아 풀었다. 게스트의
    lab 사용자는 NOPASSWD:ALL 이라 임의 스트림을 만들 수 있어, (1) 크기 제한 없는 tar 로
    호스트 메모리를 고갈시키거나 (2) root 가 신뢰할 수 없는 아카이브를 파싱하게 만들 수 있었다.

    그래서 tar 를 아예 쓰지 않는다. 이름 규칙에 맞는 파일만 골라, 크기를 먼저 확인하고,
    상한 안에서 `lxc file pull` 로 디스크에 바로 내려받는다. 아카이브 파서가 root 로 돌지
    않으므로 경로 탈출·심볼릭·장치 파일 공격이 원천 차단되고, 메모리로 슬러핑하지 않으므로
    DoS 도 막힌다.
    """
    dest = os.path.join(RECORD_ROOT, user)
    try:
        os.makedirs(dest, exist_ok=True)
        os.chmod(dest, 0o700)
        # 최상위 파일 목록만 이름·크기로 가져온다(재귀 없음).
        rc, out, _ = lxc("exec", instance, "--", "bash", "-c",
                         f"find {VM_RECORD_DIR} -maxdepth 1 -type f -printf '%s %f\\n' 2>/dev/null",
                         timeout=60)
        if rc != 0 or not out:
            return
        # 먼저 파일 목록을 한 번 훑어, 명령 로그(.commands.jsonl)가 있는 세션만 추린다.
        # 명령을 하나도 안 친 세션은 .cast 만 있고 명령 로그가 없다 — 저장할 가치가 없으니
        # 그 .cast 는 아예 안 받는다.
        have_cmds = set()
        for line in out.splitlines():
            p = line.split(" ", 1)
            if len(p) == 2 and p[1].strip().endswith(".commands.jsonl"):
                have_cmds.add(p[1].strip()[:-len(".commands.jsonl")])
        total, count = 0, 0
        for line in out.splitlines():
            parts = line.split(" ", 1)
            if len(parts) != 2:
                continue
            try:
                size = int(parts[0])
            except ValueError:
                continue
            name = parts[1].strip()
            # 이름 규칙 밖이거나 경로 구분자가 섞였으면 버린다.
            if not REC_NAME_RE.match(name) or "/" in name or name in (".", ".."):
                continue
            # 명령 로그 없는 세션의 .cast 는 건너뛴다(빈 세션).
            if name.endswith(".cast") and name[:-len(".cast")] not in have_cmds:
                continue
            if size > REC_MAX_FILE:
                log(f"{user}: 기록 {name} 이 너무 큼({size}B) — 건너뜀")
                continue
            if count >= REC_MAX_FILES or total + size > REC_MAX_TOTAL:
                log(f"{user}: 기록 회수 상한 도달 — 이후 건너뜀")
                break
            local = os.path.join(dest, name)
            if os.path.exists(local) and os.path.getsize(local) >= size:
                continue                       # 이미 받았고 크기도 같으면 건너뛴다
            r2, _, err = lxc("file", "pull", f"{instance}{VM_RECORD_DIR}/{name}",
                             local, timeout=120)
            if r2 != 0:
                log(f"{user}: {name} 회수 실패: {err[:120]}")
                continue
            try:
                os.chmod(local, 0o600)
            except OSError:
                pass
            total += size
            count += 1
    except Exception as e:
        log(f"{user} 세션 기록 회수 실패: {e}")


def enforce_isolation():
    """
    lxdbr0 의 포트들이 서로를 보지 못하게 한다.

    같은 브리지 안의 통신은 L2 에서 스위칭되어 netfilter 를 타지 않는다. 그래서
    방화벽 규칙으로도, LXD 네트워크 ACL 로도 막히지 않는다(둘 다 시험해 확인했다).
    브리지 포트 격리만이 유효하다. isolated 포트끼리는 통신할 수 없고 브리지 자신
    (게이트웨이)과는 통신할 수 있어, 인터넷과 호스트 프록시는 그대로 살아 있다.

    tap 이름은 VM 이 뜰 때마다 바뀌므로 새 포트가 생길 때마다 다시 걸어야 한다.
    """
    try:
        p = subprocess.run(["bridge", "link", "show"],
                           capture_output=True, text=True, timeout=30)
        for line in p.stdout.splitlines():
            if "master lxdbr0" not in line:
                continue
            m = re.match(r"^\d+:\s+([^:@\s]+)", line)
            if not m:
                continue
            subprocess.run(["bridge", "link", "set", "dev", m.group(1), "isolated", "on"],
                           capture_output=True, timeout=15)
    except Exception as e:
        log(f"브리지 격리 적용 실패: {e}")


def graceful_stop(name, timeout=90):
    """
    반드시 정상 종료를 먼저 시도한다.

    --force 는 전원을 뽑는 것과 같아서 아직 디스크에 내려가지 않은 파일이 사라진다.
    "정지해도 작업이 남는다"가 이 서비스의 약속이므로 강제 종료는 최후 수단이다.
    """
    rc, _, _ = lxc("stop", name, "--timeout", str(timeout), timeout=timeout + 30)
    if rc == 0:
        return True
    log(f"{name}: 정상 종료 실패 — 강제 종료로 전환")
    lxc("stop", name, "--force", timeout=120)
    return False


def stop_for(user):
    with LOCK:
        rec = STATE["users"].get(user)
        if not rec:
            return False, "환경이 없습니다"
        rec["status"] = "stopping"
        save_state()
    collect_recordings(user, rec["instance"])      # 끄기 전에 기록부터 챙긴다
    clean = graceful_stop(rec["instance"])
    with LOCK:
        rec["status"] = "stopped"
        rec["stopped_at"] = iso()
        rec["last_activity"] = iso()
        save_state()
    regen_routing()   # 죽은 주소로 프록시하지 않도록 경로를 되돌린다
    return True, ("정지했습니다. 작업 내용은 그대로 남아 있습니다." if clean else
                  "정지했습니다. 다만 정상 종료에 실패해 강제로 껐습니다 — "
                  "저장하지 않은 변경은 사라졌을 수 있습니다.")


def destroy_for(user):
    with LOCK:
        rec = STATE["users"].get(user)
    if not rec:
        return False, "환경이 없습니다"
    # 파기하면 안에 있는 기록도 함께 사라진다. 반드시 먼저 가져온다.
    if instance_state(rec["instance"]) == "Running":
        collect_recordings(user, rec["instance"])
    with LOCK:
        STATE["users"].pop(user, None)
        save_state()
    lxc("delete", rec["instance"], "--force", timeout=300)
    regen_routing()
    log(f"{user} 환경 파기 {rec['instance']}")
    return True, "환경을 반납했습니다."


def restore_for(user):
    with LOCK:
        rec = STATE["users"].get(user)
        if not rec:
            return False, "환경이 없습니다"
        rec["status"] = "restoring"
        rec["tutor_epoch"] = uuid.uuid4().hex
        save_state()
    lxc("stop", rec["instance"], "--force", timeout=180)
    rc, _, err = lxc("restore", rec["instance"], "clean", timeout=300)
    if rc != 0:
        lxc("start", rec["instance"])
        with LOCK:
            rec["status"] = "running"
            save_state()
        return False, err or "복원 실패"
    with LOCK:
        rec["status"] = "starting"
        rec["stopped_at"] = None
        rec["last_activity"] = iso()
        save_state()
    threading.Thread(target=start_for, args=(user,), daemon=True).start()
    return True, "초기 상태로 되돌렸습니다. 30초쯤 뒤 터미널을 여세요."


# ───────────────────────── 배경 유지 작업 ─────────────────────────

def reclaim_orphans():
    """
    계정이 사라진 환경을 회수한다.

    netlab-user 가 상태 파일을 직접 고치면 labd 가 메모리에 들고 있는 내용에 덮여
    되살아난다. 그래서 계정 삭제는 htpasswd 만 지우게 하고, 실제 회수는 여기서 한다.
    """
    if not os.path.isdir(HTPASSWD_DIR):
        return                       # 디렉터리를 못 읽는 상황에서 전부 지우지 않도록
    live = set(accounts())
    with LOCK:
        gone = [u for u in STATE["users"] if u not in live]
    for u in gone:
        log(f"계정이 없어진 환경 회수: {u}")
        destroy_for(u)


def heal_pool():
    """
    아직 준비되지 않은 예비 환경을 다시 살펴 준비 완료로 올리거나 버린다.

    서비스가 재시작되면 준비 여부를 확인하던 스레드가 사라진다. 이걸 방치하면
    예비가 영원히 '준비중'으로 남아 풀은 가득 찬 것으로 계산되고, 정작 배정에는
    쓰이지 못하는 교착이 생긴다.
    """
    with LOCK:
        pending = [dict(p) for p in STATE["pool"] if not p.get("ready")]

    for p in pending:
        name, ip = p["instance"], p["ip"]
        st = instance_state(name)

        if st is None:                       # 인스턴스가 사라졌다
            _drop_pool(name)
            continue
        if st != "Running":
            lxc("start", name)
            continue
        if ttyd_ready(ip) and ensure_lab_permissions(name):
            lxc("snapshot", name, "clean")   # 이미 있으면 실패하지만 무해하다
            with LOCK:
                for q in STATE["pool"]:
                    if q["instance"] == name:
                        q["ready"] = True
                save_state()
            log(f"예비 환경 준비 완료(복구): {name} ({ip})")
            continue

        # 너무 오래 준비되지 않으면 버리고 다시 만든다.
        born = parse_iso(p.get("created") or "")
        if born and (now() - born).total_seconds() > CONFIG["ready_timeout"] * 2:
            log(f"준비되지 않는 예비 환경 폐기: {name}")
            lxc("delete", name, "--force")
            _drop_pool(name)


def recent_terminal_activity(instance, since_ts):
    """
    VM 안 세션 기록(.cast/.commands.jsonl)이 since_ts 이후 갱신됐는지 본다.
    전체화면 터미널만 열고 패널을 닫은 사용자도 '활동 중'으로 잡기 위한 것.
    실패하면 보수적으로 True(활동 중으로 간주 → 정지 안 함)를 돌려준다.
    """
    try:
        rc, out, _ = lxc("exec", instance, "--", "bash", "-c",
                         "stat -c %Y /var/log/lab-sessions/*.cast "
                         "/var/log/lab-sessions/*.commands.jsonl 2>/dev/null | sort -n | tail -1",
                         timeout=20)
        if rc != 0 or not out.strip():
            return False          # 기록이 없으면 최근 활동 없음
        return int(out.strip()) >= since_ts
    except Exception:
        return True               # 못 읽으면 함부로 정지하지 않는다


def maintainer():
    """예비 환경 보충 + 오래 정지된 환경 자동 회수."""
    while True:
        try:
            load_config()
            load_users()

            # 0) 브리지 격리 — 새로 뜬 VM 의 포트를 놓치지 않도록 매번 확인한다
            enforce_isolation()

            # 1) 삭제된 계정의 환경 회수
            reclaim_orphans()

            # 2) 웜 풀 — 먼저 치유하고, 부족하면 채운다
            heal_pool()
            with LOCK:
                have = len(STATE["pool"])
            want = max(0, int(CONFIG.get("pool_size", 0)))
            if have < want:
                try:
                    create_instance()
                except Exception as e:
                    log(f"예비 환경 생성 실패: {e}")

            # 3) 실행 중인 환경의 세션 기록을 주기적으로 호스트로 옮긴다.
            #    (예상치 못한 종료로 기록을 통째로 잃지 않도록)
            with LOCK:
                running = [(u, r["instance"]) for u, r in STATE["users"].items()
                           if r.get("status") == "running" and r.get("instance")]
            for u, inst in running:
                collect_recordings(u, inst)

            # 3.5) 유휴 자동 정지 — 실행 중이나 오래 접속(패널 열림)이 없으면 일시정지한다.
            #      last_activity 는 패널이 status 를 폴링할 때 갱신되므로,
            #      "패널(=터미널)이 닫힌 지 오래됐다" = "안 쓰고 방치 중" 을 뜻한다.
            #      stop_for 는 정상 종료라 작업이 보존된다. 이후 reclaim_days 카운트가 시작된다.
            idle_h = float(CONFIG.get("idle_stop_hours", 0))
            if idle_h > 0:
                icut = now().timestamp() - idle_h * 3600
                cand = []
                with LOCK:
                    for u, rec in STATE["users"].items():
                        if rec.get("status") != "running":
                            continue
                        t = parse_iso(rec.get("last_activity") or "")
                        if t and t.timestamp() < icut:
                            cand.append((u, rec.get("instance")))
                for u, inst in cand:
                    # 패널 폴링이 끊긴 지 오래여도, 전체화면 터미널로 아직 쓰는 중일 수 있다.
                    # 그래서 VM 안 녹화 파일이 최근에 갱신됐는지도 본다(진짜 터미널 활동).
                    # 둘 다 오래됐을 때만 정지한다.
                    if inst and recent_terminal_activity(inst, icut):
                        continue
                    log(f"유휴 자동 정지: {u} (접속·터미널 {idle_h}시간 활동 없음)")
                    stop_for(u)

            # 4) 자동 회수 — 정지된 지 오래된 것만. 실행 중인 환경은 건드리지 않는다.
            days = float(CONFIG.get("reclaim_days", 7))
            if days > 0:
                cutoff = now().timestamp() - days * 86400
                stale = []
                with LOCK:
                    for u, rec in STATE["users"].items():
                        if rec.get("status") != "stopped":
                            continue
                        t = parse_iso(rec.get("stopped_at") or "")
                        if t and t.timestamp() < cutoff:
                            stale.append(u)
                for u in stale:
                    log(f"자동 회수: {u} (정지 {days}일 경과)")
                    destroy_for(u)
        except Exception as e:
            log(f"유지 작업 오류: {e}")
        # 예비 환경을 빨리 되채워야 다음 사람이 기다리지 않는다.
        time.sleep(30)


def reconcile():
    """서비스 재시작 시 실제 LXD 상태와 기록을 맞춘다."""
    rc, out, _ = lxc("list", "--format", "json", timeout=120)
    if rc != 0:
        return
    try:
        live = {i["name"]: i["status"] for i in json.loads(out)}
    except Exception:
        return
    with LOCK:
        for u, rec in list(STATE["users"].items()):
            st = live.get(rec["instance"])
            if st is None:
                log(f"기록에만 있던 환경 제거: {u}/{rec['instance']}")
                STATE["users"].pop(u, None)
            else:
                rec["status"] = "running" if st == "Running" else "stopped"
                if rec["status"] == "stopped" and not rec.get("stopped_at"):
                    rec["stopped_at"] = iso()
        STATE["pool"] = [p for p in STATE["pool"] if p["instance"] in live]
        save_state()


# ─────────────────────────────── 화면 ───────────────────────────────

PANEL = """<!doctype html><meta charset=utf-8><title>{user} — 실습 환경</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<script src="assets/theme.js"></script>
<script src="assets/terminal-theme.js"></script>
<style>
 :root{{color-scheme:light;--bg:#f6f8fa;--panel:#fff;--panel2:#eef2f6;--ink:#14202e;--ink2:#41556b;--ink3:#6b7f95;
  --wire:#d2dce5;--sig:#0d6e86;--sigbg:#e2f1f5;--ok:#15803d;--okbg:#e7f5ec;--warn:#b45309;--crit:#b91c1c;--critbg:#fceceb;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}}
 @media(prefers-color-scheme:dark){{:root:not([data-theme=light]){{color-scheme:dark;--bg:#0d1621;--panel:#131f2c;--panel2:#1a2837;
  --ink:#dbe6f1;--ink2:#a3b6c9;--ink3:#7d92a7;--wire:#2a3b4d;--sig:#3fc4dd;--sigbg:#12313a;
  --ok:#4ade80;--okbg:#122a1d;--warn:#fbbf24;--crit:#f87171;--critbg:#2e1616}}}}
 [data-theme=dark]{{color-scheme:dark;--bg:#0d1621;--panel:#131f2c;--panel2:#1a2837;--ink:#dbe6f1;--ink2:#a3b6c9;--ink3:#7d92a7;
  --wire:#2a3b4d;--sig:#3fc4dd;--sigbg:#12313a;--ok:#4ade80;--okbg:#122a1d;--warn:#fbbf24;--crit:#f87171;--critbg:#2e1616}}
 *{{box-sizing:border-box}}
 body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 ui-sans-serif,system-ui,"Noto Sans KR",sans-serif}}
 .wrap{{max-width:1240px;margin:0 auto;padding:36px 20px 64px}}
 .panel-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:18px}}
 .panel-header>div{{min-width:0}}
 .panel-header h1{{overflow-wrap:anywhere}}
 .theme-toggle{{display:inline-flex;align-items:center;gap:7px;flex-shrink:0;margin-top:2px}}
 #themeicon{{font-size:19px;line-height:1}}
 @media(max-width:480px){{.panel-header h1{{font-size:22px}}.theme-toggle{{padding:8px 10px;font-size:13px}}}}
 /* 상단 컨트롤 스트립: 상태·시작·정지·초기화·종료를 가로로 */
 .controlbar{{display:flex;gap:12px;flex-wrap:wrap;align-items:stretch;margin-bottom:18px}}
 .ctl{{background:var(--panel);border:1px solid var(--wire);border-radius:11px;
  padding:12px 16px;display:flex;align-items:center;gap:12px}}
 .ctl.grow{{flex:1 1 260px}}
 .ctl .t{{white-space:nowrap}}
 .ctl button{{white-space:nowrap}}
 .controlbar button{{display:inline-flex;align-items:center;gap:6px}}
 .controlbar button svg{{width:15px;height:15px;flex:none}}
 .eyebrow{{font-family:var(--mono);font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--sig);margin:0 0 8px}}
 h1{{margin:0 0 4px;font-size:26px;letter-spacing:-.02em}}
 .sub{{color:var(--ink3);font-size:14px;margin:0 0 26px}}
 .card{{background:var(--panel);border:1px solid var(--wire);border-radius:11px;padding:18px 20px;margin-bottom:14px}}
 .row{{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}}
 .t{{font-weight:640}} .d{{color:var(--ink3);font-size:13.5px;margin-top:2px}}
 button{{font:inherit;font-size:14.5px;padding:9px 16px;border-radius:8px;border:1px solid var(--wire);
  background:var(--panel2);color:var(--ink);cursor:pointer;white-space:nowrap}}
 button:hover:not(:disabled){{border-color:var(--sig);color:var(--sig)}}
 button:disabled{{opacity:.45;cursor:not-allowed}}
 button.primary{{background:var(--sig);border-color:var(--sig);color:var(--panel);font-weight:600}}
 button.primary:hover:not(:disabled){{opacity:.88;color:var(--panel)}}
 button.danger{{color:var(--crit);border-color:var(--crit)}}
 button:focus-visible{{outline:2px solid var(--sig);outline-offset:2px}}
 a.term{{display:block;text-align:center;padding:14px;border-radius:9px;background:var(--sig);
  color:var(--panel);text-decoration:none;font-weight:640;font-size:16px}}
 a.term:hover{{opacity:.9}}
 .state{{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:13.5px}}
 .dot{{width:9px;height:9px;border-radius:50%;flex:none}}
 .meta{{font-family:var(--mono);font-size:12px;color:var(--ink3);margin-top:10px;
  display:flex;flex-wrap:wrap;gap:4px 16px}}
 #out{{font-family:var(--mono);font-size:13px;background:var(--panel2);border-radius:8px;
  padding:11px 13px;color:var(--ink2);white-space:pre-wrap;margin-top:14px}}
 #helpout{{margin-top:12px;background:var(--sigbg);border-radius:8px;padding:12px 14px;
  font-size:14px;line-height:1.6;color:var(--ink)}}
 .helptag{{display:inline-block;font-size:12px;color:var(--sig);font-weight:600;margin-bottom:6px}}
 .helpadvice{{white-space:pre-wrap}}
 /* 터미널 임베드 + 옆 도움말. 터미널은 고정 크기(녹화·리플레이 일관성). */
 .workspace{{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-start;margin-bottom:16px}}
 .termbox{{flex:0 0 auto;width:860px;max-width:100%;border:1px solid var(--wire);
  border-radius:10px;overflow:hidden;background:#0d1621}}
 .termhead{{display:flex;justify-content:space-between;align-items:center;
  padding:6px 12px;background:var(--panel2);font-size:12px;color:var(--ink3)}}
 .termhead a{{color:var(--sig);text-decoration:none;cursor:pointer}}
 .termhead{{flex-wrap:wrap;gap:8px}}
 .termtools{{display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
 .terminal-theme{{display:inline-flex;align-items:center;gap:6px}}
 #terminaltheme{{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--wire);border-radius:6px;padding:4px 6px;max-width:min(210px,52vw)}}
 #terminaltheme:focus-visible{{outline:2px solid var(--sig);outline-offset:2px}}
 #termframe{{width:860px;height:500px;max-width:100%;border:0;display:block}}
 .helpside{{flex:1 1 320px;min-width:300px;background:var(--panel);
  border:1px solid var(--wire);border-radius:10px;padding:16px 18px}}
 .helpbtn{{width:100%;margin-top:10px}}
 @media(max-width:920px){{ .termbox,#termframe{{width:100%}} }}
 .hide{{display:none}}
 .note{{border-left:3px solid var(--sig);background:var(--sigbg);padding:11px 14px;
  border-radius:0 7px 7px 0;font-size:13.5px;color:var(--ink2);margin-top:14px}}
 .spin{{display:inline-block;width:13px;height:13px;border:2px solid var(--wire);
  border-top-color:var(--sig);border-radius:50%;animation:sp .8s linear infinite;vertical-align:-2px}}
 @keyframes sp{{to{{transform:rotate(360deg)}}}}
 @media(prefers-reduced-motion:reduce){{.spin{{animation:none}}}}
</style>
<link rel=stylesheet href="assets/player.css">
<link rel=stylesheet href="assets/replay.css">
<link rel=stylesheet href="assets/tutor.css">
<link rel=stylesheet href="assets/community.css">
<link rel=stylesheet href="assets/notice.css">
<div class=wrap>
  <header class=panel-header><div>
  <p class=eyebrow>Network Fundamentals Lab</p>
  <h1>{user} 님의 실습 환경</h1>
  </div><div class=header-actions>
    <button type=button id=noticelauncher class=notice-launcher aria-controls=noticepanel aria-expanded=false aria-label="공지 열기">공지<span id=noticebadge class=notice-badge hidden>새 공지</span></button>
    <button type=button id=themetoggle class=theme-toggle aria-label="다크 테마" aria-pressed=false title="테마 전환"><span id=themeicon aria-hidden=true>☀</span><span id=themelabel>라이트</span></button>
    <section id=noticepanel class=notice-panel role=dialog aria-modal=false aria-labelledby=noticeheading hidden>
      <div class=notice-heading><span class=eyebrow id=noticeheading>공지</span><button type=button id=noticeclose aria-label="공지 닫기">닫기 ×</button></div>
      <h2 id=noticetitle class=notice-title></h2>
      <div id=noticemeta class=d></div>
      <div id=noticebody class=notice-body></div>
      <div id=noticestate class=notice-state role=status aria-live=polite></div>
    </section>
  </div></header>

  <div class=controlbar>
    <div class="ctl grow">
      <div><div class=t>상태</div><div class=state id=state><span class=spin></span> 확인 중</div>
        <div class=meta id=meta></div></div>
      <button onclick=refresh() id=brefresh style="margin-left:auto"><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round><path d="M21 12a9 9 0 1 1-2.6-6.4L21 8"/><path d="M21 3v5h-5"/></svg>새로고침</button>
    </div>
    <div id=none class="ctl hide">
      <div class=t>환경 없음</div>
      <button class=primary onclick="act('start')">시작</button>
    </div>
    <div id=stopped class="ctl hide">
      <div class=t>정지됨</div>
      <button class=primary onclick="act('start')">다시 시작</button>
    </div>
    <div id=runctrl class="ctl hide">
      <button onclick="act('stop','환경을 일시 정지합니다. 작업 내용은 남고 다시 시작하면 이어집니다.')"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="5" width="3.6" height="14" rx="1"></rect><rect x="13.4" y="5" width="3.6" height="14" rx="1"></rect></svg>일시 정지</button>
      <button class=danger onclick="act('restore','정말 초기화할까요? 저장하지 않은 작업은 모두 사라집니다.')"><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round><path d="M3 12a9 9 0 1 0 2.6-6.4L3 8"/><path d="M3 3v5h5"/></svg>초기화</button>
    </div>
    <div id=give class="ctl hide">
      <button class=danger onclick="act('destroy','환경을 영구 종료합니다. 작업 내용이 모두 사라지고 되돌릴 수 없습니다. 진행할까요?')"><svg viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M6 6l1 14h10l1-14"/></svg>영구 종료</button>
    </div>
  </div>

  <div id=running class=hide>
    <div class=workspace>
      <div class=termbox>
        <div class=termhead>
          <span>실습 터미널</span>
          <div class=termtools><label class=terminal-theme for=terminaltheme>테마 <select id=terminaltheme>
            <option value=current>기본 · Slate</option>
            <option value=solarized-dark>Solarized Dark</option>
            <option value=solarized-light>Solarized Light (White)</option>
            <option value=ubuntu>Ubuntu · Aubergine</option>
          </select></label><span id=terminalthemestatus role=status aria-live=polite></span><a id=termfull target=_blank rel=noopener>전체화면 ⤢</a></div>
        </div>
        <iframe id=termframe title="실습 터미널"></iframe>
      </div>
      <div class=helpside id=tutorpanel hidden role=region aria-label="AI 실습 튜터">
        <div class=tutor-heading><div class=t>AI 실습 튜터</div><button type=button id=tutorclose aria-label="튜터 닫기">닫기 ×</button></div>
        <button type=button id=tutormodel aria-haspopup=dialog>모델 선택</button>
        <dialog id=tutormodeldialog aria-labelledby=tutormodeltitle>
          <form method=dialog><div class=tutor-heading><h2 id=tutormodeltitle>튜터 모델 선택</h2><button aria-label="모델 선택 닫기">닫기 ×</button></div></form>
          <p class=d>다음 질문부터 선택한 모델이 답합니다. 대화와 실습 맥락은 유지됩니다.</p>
          <div id=tutormodeloptions></div>
        </dialog>
        <div id=tutorcontext class=d>실습 상태는 질문 또는 상태 확인 시 조회합니다.</div>
        <select id=tutorconversations aria-label="이 브라우저의 이전 대화" onchange="tutorSelect(this.value)"><option value="">새 대화</option></select>
        <div class=d>질문과 실험 결과를 함께 살펴봅니다.{tutor_shared_notice}</div>
        <div id=tutorlog style="max-height:440px;overflow:auto;white-space:pre-wrap"></div>
        <div class=tutor-compose>
          <textarea id=tutorinput rows=3 maxlength=6000 aria-label="AI 튜터 질문" aria-describedby=tutorinputhint placeholder="질문이나 관찰한 결과를 적어 주세요"></textarea>
          <button type=button id=tutorsend onclick="tutorSend()">보내기</button>
        </div>
        <div id=tutorinputhint class=d>Enter 전송 · Shift+Enter 줄바꿈</div>
        <label class="d tutor-share"><input type=checkbox id=tutorshare> 최근 명령·화면 함께 전달<br>비밀번호 등이 포함됐는지 확인하세요.</label>
        <div class=tutor-actions>
        <button onclick="tutorSend('현재 상태를 확인하고 앞선 관찰과 비교해서 설명해 줘.',true)">능동 진단 · 패킷 발생</button>
        <button onclick="tutorSend('이 실습의 목표와 첫 관찰 방법을 설명해 줘.')">실습 목표</button>
        <button onclick="tutorSend('다음 단계 힌트를 줘.',false,'hint')">다음 힌트</button>
        <button onclick="tutorSend('수정 명령과 이유를 설명해 줘.',false,'solution')">수정 명령 보기</button>
        <button onclick="tutorSend(null,false,'reflection')">내 설명 피드백</button>
        <button onclick="tutorNew()">새 대화</button>
        </div>
        <div id=tutorstatus class=d role=status aria-live=polite></div>
      </div>
    </div>
    <button type=button id=tutorlauncher class=tutor-launcher aria-controls=tutorpanel aria-expanded=false aria-label="AI 튜터 열기"><span aria-hidden=true>✦</span> AI 튜터</button>
  </div>

  <section id=communitypanel class=community-panel hidden aria-label="접속자 채팅">
    <div class=community-heading><div class=t>함께 실습하는 사람들</div><button type=button id=communityclose aria-label="채팅 닫기">닫기 ×</button></div>
    <div class=d>전체 채팅방 · 최근 메시지 100개 보관 · 로그인 계정명 공개</div>
    <div id=communityonline class=d></div>
    <div id=communitymessages class=community-messages aria-label="채팅 메시지"></div>
    <form id=communityform>
      <label class=d for=communityinput>메시지 (Enter 전송 · Shift+Enter 줄바꿈)</label>
      <div class=community-compose><textarea id=communityinput rows=2 maxlength=1000 placeholder="질문이나 발견한 내용을 나눠 보세요"></textarea><button type=submit id=communitysend>전송</button></div>
    </form>
    <div id=communitystatus class=d role=status aria-live=polite></div>
  </section>
  <button type=button id=communitylauncher class=community-launcher aria-controls=communitypanel aria-expanded=false aria-label="접속자 채팅 열기"><span aria-hidden=true>💬</span> 채팅 <span id=communitybadge hidden></span></button>

  <div class=card>
    <div class=t style="margin-bottom:4px">내 세션 복습</div>
    <div class=d style="margin-bottom:10px">직접 했던 실습을 다시 볼 수 있어요. 세션을 고르면 크게 재생됩니다.</div>
    <div id=replay-root></div>
  </div>

  <div class=note id=policy></div>
  <div id=out>준비됨</div>
</div>
<script>
const $=i=>document.getElementById(i), out=$('out');
let busy=false;
function show(id,on){{ $(id).classList.toggle('hide',!on); }}
async function refresh(){{
  try{{
    const j=await (await fetch('status',{{cache:'no-store'}})).json();
    const s=j.state||'none';
    const c={{running:'var(--ok)',stopped:'var(--ink3)',none:'var(--ink3)'}}[s]||'var(--warn)';
    const label={{none:'환경 없음',running:'실행 중',stopped:'정지됨',
      provisioning:'발급 중',starting:'시작 중',stopping:'정지 중',
      restoring:'초기화 중',error:'오류'}}[s]||s;
    const pend=['provisioning','starting','stopping','restoring'].includes(s);
    $('state').innerHTML=(pend?'<span class=spin></span> ':
      '<span class=dot style="background:'+c+'"></span>')+label;
    $('meta').innerHTML = j.instance
      ? ['인스턴스 '+j.instance, j.ip?('주소 '+j.ip):'', j.created?('발급 '+j.created.slice(0,10)):'']
          .filter(Boolean).map(x=>'<span>'+x+'</span>').join('')
      : '';
    show('none', s==='none'); show('stopped', s==='stopped');
    show('running', s==='running'); show('runctrl', s==='running');
    show('give', s==='running'||s==='stopped');
    mountTerm(s==='running');
    $('policy').textContent = j.policy||'';
    if(pend) setTimeout(refresh,3000);
  }}catch(e){{ $('state').textContent='확인 실패'; }}
}}
// 터미널은 실행 중일 때만 임베드한다(정지 상태에서 세션을 새로 만들지 않도록).
function termUrl(){{ const base=ctlBase().replace(/ctl\\/$/,'term/');return window.netlabTerminal?window.netlabTerminal.url(base):base; }}
function mountTerm(on){{
  const f=$('termframe'), a=$('termfull');
  if(on){{
    if(!f.src){{ const u=termUrl(); f.src=u; a.href=u; }}
  }}else if(f.src){{ f.removeAttribute('src'); }}
}}
async function act(what,msg){{
  if(msg&&!confirm(msg)) return;
  if(busy) return; busy=true;
  document.querySelectorAll('button').forEach(b=>b.disabled=true);
  out.textContent='처리 중…';
  try{{
    const j=await (await fetch(what,{{method:'POST'}})).json();
    out.textContent=(j.ok?'완료: ':'실패: ')+j.message;
  }}catch(e){{ out.textContent='실패: '+e; }}
  busy=false;
  document.querySelectorAll('button').forEach(b=>b.disabled=false);
  refresh();
}}
let helpAsked=0;
// help 는 절대경로로 못박는다. 상대 fetch 는 URL 에 트레일링 슬래시가 없으면
// /u/<user>/help 로 잘못 나가고, 그걸 Cloudflare Access 가 로그인으로 리다이렉트하면
// 브라우저가 CORS 로 "Failed to fetch" 를 던진다.
function ctlBase(){{
  let p=location.pathname;
  const i=p.indexOf('/ctl');
  if(i>=0) return p.slice(0,i+4)+'/';   // .../ctl/
  return p.endsWith('/')?p:p+'/';
}}
async function askHelp(){{
  const btn=$('helpbtn'), o=$('helpout');
  btn.disabled=true; const label=btn.textContent;
  btn.textContent='살펴보는 중…';
  o.classList.remove('hide');
  o.innerHTML='<span class=spin></span> 지금 화면 상태를 읽고 조언을 만들고 있어요… (십여 초 걸릴 수 있어요)';
  const ctrl=new AbortController();
  const timer=setTimeout(()=>ctrl.abort(),120000);
  try{{
    const r=await fetch(ctlBase()+'help',{{method:'POST',signal:ctrl.signal}});
    if(!r.ok){{ o.textContent='도움말을 준비하지 못했어요 (HTTP '+r.status+'). 잠시 후 다시 눌러주세요.'; }}
    else{{
      const j=await r.json();
      if(!j.ok){{ o.textContent=j.message||'도움말을 준비하지 못했어요.'; }}
      else{{
        helpAsked++;
        const tag = j.passed ? '<b>이미 통과했어요 🎉</b><br>'
                   : '<span class=helptag>'+j.title+' · '+j.attempt+'번째 힌트</span>';
        o.innerHTML = tag + '<div class=helpadvice>'+
          j.advice.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>')+'</div>';
        if(!j.passed) btn.textContent='아직 막혀요 · 더 구체적으로';
      }}
    }}
  }}catch(e){{
    o.textContent = (e.name==='AbortError')
      ? '응답이 너무 오래 걸려요. 잠시 후 다시 시도해 주세요.'
      : '연결에 실패했어요. 페이지를 새로고침한 뒤 다시 눌러주세요.';
  }}
  clearTimeout(timer);
  if(btn.textContent==='살펴보는 중…') btn.textContent=label;
  btn.disabled=false;
}}
refresh(); setInterval(()=>{{if(!busy)refresh();}},10000);
</script>
<script>window.LAB_REPLAY={{admin:false}};</script>
<script src="assets/player.js"></script>
<script src="assets/replay.js"></script>
<script src="assets/tutor.js"></script>
<script src="assets/community-transport.js"></script>
<script src="assets/community.js"></script>
<script src="assets/notice.js"></script>
"""

# ─────────────────────────── 상태 감시 ───────────────────────────
#
# 고장을 팀원이 항의할 때까지 모르던 문제를 없앤다. 관리자 콘솔이 주기적으로 이걸 읽어
# 서비스·저장소·터널·자원을 한눈에 보여주고, 임계치를 넘으면 경고색으로 표시한다.

HEALTH_SVCS = ["snap.lxd.daemon", "nginx", "labd", "docker",
               "lxd-docker-fix", "netlab-netguard", "cloudflared"]
CF_METRICS = "http://127.0.0.1:20241"


def _run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception:
        return 1, "", "timeout"


def collect_health():
    """감시 지표를 모아 돌려준다. 각 항목은 ok/warn/crit 상태를 스스로 판정한다."""
    items = []

    def add(key, label, value, level="ok", detail=""):
        items.append({"key": key, "label": label, "value": value,
                      "level": level, "detail": detail})

    # 1) 서비스
    down = []
    for s in HEALTH_SVCS:
        _, out, _ = _run(["systemctl", "is-active", s], 5)
        if out != "active":
            down.append(s)
    add("services", "서비스",
        "모두 정상" if not down else f"{len(down)}개 비정상",
        "ok" if not down else "crit",
        "" if not down else "중단: " + ", ".join(down))

    # 2) 저장소 (ZFS 풀)
    rc, out, _ = _run(["zpool", "list", "-H", "-o", "health,capacity", "labpool"], 8)
    if rc == 0 and out:
        parts = out.split()
        health = parts[0] if parts else "?"
        cap = int(parts[1].rstrip("%")) if len(parts) > 1 else 0
        lvl = "ok" if (health == "ONLINE" and cap < 80) else \
              ("warn" if health == "ONLINE" and cap < 90 else "crit")
        add("zpool", "저장소(ZFS)", f"{health} · {cap}% 사용", lvl,
            "80% 넘으면 lvextend 로 확장" if cap >= 80 else "")
    else:
        add("zpool", "저장소(ZFS)", "조회 실패", "crit")

    # 3) 루트 파일시스템 (LVM·이미지 발행이 여기 쓴다)
    rc, out, _ = _run(["df", "-h", "--output=pcent,avail", "/"], 5)
    if rc == 0:
        line = out.splitlines()[-1].split()
        pct = int(line[0].rstrip("%"))
        lvl = "ok" if pct < 85 else ("warn" if pct < 92 else "crit")
        add("rootfs", "루트 디스크", f"{pct}% 사용 · {line[1]} 여유", lvl)

    # 4) 메모리
    rc, out, _ = _run(["free", "-m"], 5)
    if rc == 0:
        for ln in out.splitlines():
            if ln.startswith("Mem:"):
                f = ln.split()
                total, avail = int(f[1]), int(f[-1])
                pct = round((total - avail) / total * 100)
                lvl = "ok" if pct < 85 else ("warn" if pct < 93 else "crit")
                add("mem", "메모리", f"{pct}% 사용 · {avail // 1024}GB 가용", lvl)
                break

    # 5) 부하
    try:
        la1 = os.getloadavg()[0]
        ncpu = os.cpu_count() or 1
        ratio = la1 / ncpu
        lvl = "ok" if ratio < 1.5 else ("warn" if ratio < 3 else "crit")
        add("load", "부하", f"{la1:.2f} / {ncpu}코어", lvl)
    except Exception:
        pass

    # 6) 터널 연결
    try:
        import urllib.request
        with urllib.request.urlopen(f"{CF_METRICS}/ready", timeout=5) as r:
            d = json.load(r)
        conns = d.get("readyConnections", 0)
        add("tunnel", "Cloudflare 터널",
            f"연결 {conns}개", "ok" if conns > 0 else "crit",
            "" if conns > 0 else "터널 다운 — 외부 접속 불가")
    except Exception:
        add("tunnel", "Cloudflare 터널", "조회 실패", "crit")

    # 7) 터널 라우팅 (netlab 호스트명이 실제로 걸려 있는가)
    try:
        import urllib.request
        with urllib.request.urlopen(f"{CF_METRICS}/config", timeout=5) as r:
            cfg = json.load(r)
        hosts = [i.get("hostname", "") for i in
                 cfg.get("config", {}).get("ingress", [])]
        netlab_hosts = [h for h in hosts if "netlab" in h]
        add("route", "외부 라우팅",
            " · ".join(netlab_hosts) + " 연결됨" if netlab_hosts else "라우팅 없음",
            "ok" if netlab_hosts else "crit")
    except Exception:
        add("route", "외부 라우팅", "조회 실패", "warn")

    # 8) 웜 풀
    with LOCK:
        ready = sum(1 for p in STATE["pool"] if p.get("ready"))
        pending = len(STATE["pool"]) - ready
    want = int(CONFIG.get("pool_size", 0))
    add("pool", "예비 환경",
        f"준비 {ready} / 목표 {want}" + (f" · 준비중 {pending}" if pending else ""),
        "ok" if ready >= want else "warn",
        "다음 발급이 느려질 수 있음" if ready < want else "")

    # 9) 녹화 보관 용량
    rc, out, _ = _run(["du", "-sh", RECORD_ROOT], 15)
    if rc == 0 and out:
        add("rec", "녹화 보관", out.split()[0], "ok",
            "커지면 netlab-record purge")

    return items


ADMIN = """<!doctype html><meta charset=utf-8><title>실습 환경 — 관리자</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
 :root{--bg:#f6f8fa;--panel:#fff;--panel2:#eef2f6;--ink:#14202e;--ink2:#41556b;--ink3:#6b7f95;
  --wire:#d2dce5;--sig:#0d6e86;--ok:#15803d;--okbg:#e7f5ec;--crit:#b91c1c;--critbg:#fceceb;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
 @media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0d1621;--panel:#131f2c;--panel2:#1a2837;
  --ink:#dbe6f1;--ink2:#a3b6c9;--ink3:#7d92a7;--wire:#2a3b4d;--sig:#3fc4dd;--ok:#4ade80;--okbg:#122a1d;
  --crit:#f87171;--critbg:#2e1616}}
 [data-theme=dark]{--bg:#0d1621;--panel:#131f2c;--panel2:#1a2837;--ink:#dbe6f1;--ink2:#a3b6c9;--ink3:#7d92a7;
  --wire:#2a3b4d;--sig:#3fc4dd;--ok:#4ade80;--okbg:#122a1d;--crit:#f87171;--critbg:#2e1616}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 ui-sans-serif,system-ui,"Noto Sans KR",sans-serif}
 .wrap{max-width:1000px;margin:0 auto;padding:40px 20px 64px}
 .eyebrow{font-family:var(--mono);font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--sig);margin:0 0 8px}
 h1{margin:0 0 22px;font-size:25px;letter-spacing:-.02em}
 h2{font-family:var(--mono);font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink3);
  margin:30px 0 12px;padding-bottom:8px;border-bottom:1px solid var(--wire)}
 .tw{overflow-x:auto;border:1px solid var(--wire);border-radius:10px;background:var(--panel)}
 table{border-collapse:collapse;width:100%;font-size:14px}
 th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--wire);white-space:nowrap}
 thead th{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink3);background:var(--panel2)}
 tbody tr:last-child td{border-bottom:none}
 td.m{font-family:var(--mono);font-size:12.5px;font-variant-numeric:tabular-nums}
 .pill{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px}
 .pill.on{background:var(--okbg);color:var(--ok)} .pill.off{background:var(--panel2);color:var(--ink3)}
 button{font:inherit;font-size:13px;padding:5px 11px;border-radius:7px;border:1px solid var(--wire);
  background:var(--panel2);color:var(--ink);cursor:pointer;margin-right:6px}
 button:hover{border-color:var(--sig);color:var(--sig)}
 button.danger:hover{border-color:var(--crit);color:var(--crit)}
 button:focus-visible{outline:2px solid var(--sig);outline-offset:2px}
 .cfg{background:var(--panel);border:1px solid var(--wire);border-radius:10px;padding:16px 18px;
  display:flex;align-items:center;gap:12px;flex-wrap:wrap}
 input{font:inherit;font-family:var(--mono);width:70px;padding:6px 9px;border-radius:7px;
  border:1px solid var(--wire);background:var(--bg);color:var(--ink)}
 #out{font-family:var(--mono);font-size:12.5px;background:var(--panel2);border-radius:8px;
  padding:10px 13px;color:var(--ink2);margin-top:16px;white-space:pre-wrap}
 .empty{padding:20px;color:var(--ink3);font-size:14px}
 :root{--warn:#b45309;--warnbg:#fdf1e0}
 @media(prefers-color-scheme:dark){:root:not([data-theme=light]){--warn:#fbbf24;--warnbg:#2e2312}}
 [data-theme=dark]{--warn:#fbbf24;--warnbg:#2e2312}
 .hgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px}
 .hcard{background:var(--panel);border:1px solid var(--wire);border-radius:10px;padding:12px 14px;
  border-left:3px solid var(--ok)}
 .hcard.warn{border-left-color:var(--warn)} .hcard.crit{border-left-color:var(--crit)}
 .hcard .hl{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--ink3);
  font-family:var(--mono);letter-spacing:.04em}
 .hdot{width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none}
 .hcard.warn .hdot{background:var(--warn)} .hcard.crit .hdot{background:var(--crit)}
 .hcard .hv{font-size:15px;font-weight:600;margin-top:5px}
 .hcard.crit .hv{color:var(--crit)} .hcard.warn .hv{color:var(--warn)}
 .hcard .hd{font-size:12px;color:var(--ink3);margin-top:3px}
 .hbanner{border-radius:10px;padding:11px 15px;margin:0 0 12px;font-size:14px;font-weight:600;display:none}
 .hbanner.show{display:block} .hbanner.crit{background:var(--critbg);color:var(--crit)}
 .hbanner.warn{background:var(--warnbg);color:var(--warn)}
 .hhead{display:flex;justify-content:space-between;align-items:baseline}
 .hhead .upd{font-family:var(--mono);font-size:11px;color:var(--ink3)}
 select{font:inherit;padding:6px 10px;border-radius:7px;border:1px solid var(--wire);background:var(--bg);color:var(--ink)}
 .notice-admin{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
 @media(max-width:900px){.notice-admin{grid-template-columns:minmax(0,1fr)}}
 .notice-admin .cfg{flex-direction:column;align-items:stretch;gap:8px}
 .notice-admin label{font-size:13px;color:var(--ink3)}
 .notice-admin input,.notice-admin textarea{width:100%;font:inherit;font-size:14px;padding:8px 10px;border-radius:7px;
  border:1px solid var(--wire);background:var(--bg);color:var(--ink);box-sizing:border-box}
 .notice-admin textarea{min-height:140px;resize:vertical}
 .notice-current h3{margin:0;font-size:16px;overflow-wrap:anywhere}
 .notice-current .nbody{white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;color:var(--ink2);max-height:160px;overflow:auto}
 .notice-current .nmeta{font-family:var(--mono);font-size:12px;color:var(--ink3)}
 .notice-unread{font-size:13px;color:var(--ink3);overflow-wrap:anywhere}
</style>
<link rel=stylesheet href="assets/player.css">
<link rel=stylesheet href="assets/replay.css">
<div class=wrap>
  <p class=eyebrow>Network Fundamentals Lab</p>
  <h1>관리자 콘솔</h1>

  <div class=hhead><h2 style="border:none;margin-bottom:8px">시스템 상태</h2>
    <span class=upd id=hupd></span></div>
  <div class="hbanner" id=hbanner></div>
  <div class=hgrid id=hgrid></div>

  <h2>공지</h2>
  <div class=notice-admin>
    <form class=cfg id=noticeform>
      <label for=noticetitlein>제목</label><input id=noticetitlein maxlength=100 required>
      <label for=noticebodyin>내용 (게시하면 이전 공지를 대신해 사용자에게 최신 공지만 보입니다)</label>
      <textarea id=noticebodyin maxlength=5000 required></textarea>
      <div><button type=submit>게시</button></div>
    </form>
    <div class="cfg notice-current" id=noticecurrent>
      <div class=empty>불러오는 중…</div>
    </div>
  </div>
  <div class=tw style="margin-top:12px"><table>
    <thead><tr><th>읽은 사람</th><th>읽은 시각</th></tr></thead>
    <tbody id=noticereaders><tr><td colspan=2 class=empty>—</td></tr></tbody>
  </table></div>
  <div class=notice-unread id=noticeunread></div>

  <h2>발급된 환경</h2>
  <div class=note id=adminpresence role=status aria-live=polite>접속자 연결 중…</div>
  <div class=tw><table>
    <thead><tr><th>계정</th><th>상태</th><th>인스턴스</th><th>주소</th><th>발급</th><th>정지 경과</th><th>조치</th></tr></thead>
    <tbody id=rows><tr><td colspan=7 class=empty>불러오는 중…</td></tr></tbody>
  </table></div>

  <h2>예비 환경 (웜 풀)</h2>
  <div class=tw><table>
    <thead><tr><th>인스턴스</th><th>주소</th><th>준비</th></tr></thead>
    <tbody id=pool></tbody>
  </table></div>

  <h2>세션 리플레이</h2>
  <div id=replay-root></div>

  <h2>정책</h2>
  <div class=cfg>
    <label>자동 회수 기간 <input id=days type=number min=0 step=1></label>
    <span style="color:var(--ink3);font-size:13.5px">일 — 정지된 채 이 기간이 지나면 파기 (0이면 회수 안 함)</span>
    <label>예비 <input id=pool_size type=number min=0 max=5 step=1></label>
    <span style="color:var(--ink3);font-size:13.5px">대</span>
    <button onclick=saveCfg()>저장</button>
  </div>
  <div id=out>준비됨</div>
</div>
<script>
const $=i=>document.getElementById(i), out=$('out');
function ago(iso){ if(!iso) return '—';
  const d=(Date.now()-new Date(iso).getTime())/86400000;
  return d<1 ? Math.round(d*24)+'시간' : d.toFixed(1)+'일'; }
async function load(){
  const j=await (await fetch('list',{cache:'no-store'})).json();
  const rows=j.users.map(u=>`<tr>
    <td class=m><b>${u.user}</b></td>
    <td><span class="pill ${u.state==='running'?'on':'off'}">${u.state}</span></td>
    <td class=m>${u.instance||'—'}</td><td class=m>${u.ip||'—'}</td>
    <td class=m>${(u.created||'').slice(0,10)||'—'}</td>
    <td class=m>${u.state==='stopped'?ago(u.stopped_at):'—'}</td>
    <td>${u.state==='running'?`<button onclick="act('stop','${u.user}')">정지</button>`:''}
        <button class=danger onclick="act('destroy','${u.user}')">회수</button></td></tr>`).join('');
  $('rows').innerHTML = rows || '<tr><td colspan=7 class=empty>발급된 환경이 없습니다.</td></tr>';
  $('pool').innerHTML = j.pool.map(p=>`<tr><td class=m>${p.instance}</td><td class=m>${p.ip}</td>
    <td><span class="pill ${p.ready?'on':'off'}">${p.ready?'준비됨':'준비 중'}</span></td></tr>`).join('')
    || '<tr><td colspan=3 class=empty>예비 환경이 없습니다.</td></tr>';
  $('days').value=j.config.reclaim_days; $('pool_size').value=j.config.pool_size;
}
async function act(what,user){
  const msg = what==='destroy' ? `${user} 의 환경을 영구 종료합니다. 작업 내용이 모두 사라지고 되돌릴 수 없습니다.`
                               : `${user} 의 환경을 정지합니다.`;
  if(!confirm(msg)) return;
  out.textContent='처리 중…';
  const j=await (await fetch(what,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({user})})).json();
  out.textContent=(j.ok?'완료: ':'실패: ')+j.message; load();
}
async function saveCfg(){
  out.textContent='저장 중…';
  const j=await (await fetch('config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({reclaim_days:+$('days').value, pool_size:+$('pool_size').value})})).json();
  out.textContent=(j.ok?'저장했습니다.':'실패: '+j.message); load();
}
async function health(){
  try{
    const j=await (await fetch('health',{cache:'no-store'})).json();
    const items=j.items||[];
    $('hgrid').innerHTML=items.map(h=>`<div class="hcard ${h.level}">
      <div class=hl><span class=hdot></span>${h.label}</div>
      <div class=hv>${h.value}</div>
      ${h.detail?`<div class=hd>${h.detail}</div>`:''}</div>`).join('');
    const crit=items.filter(h=>h.level==='crit'), warn=items.filter(h=>h.level==='warn');
    const b=$('hbanner');
    if(crit.length){ b.className='hbanner show crit';
      b.textContent='주의가 필요합니다: '+crit.map(h=>h.label).join(', '); }
    else if(warn.length){ b.className='hbanner show warn';
      b.textContent='확인 권장: '+warn.map(h=>h.label).join(', '); }
    else b.className='hbanner';
    const d=new Date(); $('hupd').textContent='갱신 '+d.toTimeString().slice(0,8);
  }catch(e){ $('hgrid').innerHTML='<div class="hcard crit"><div class=hv>상태 조회 실패</div></div>'; }
}
const stamp=t=>{const d=new Date(t*1000),p=x=>String(x).padStart(2,'0');return (d.getMonth()+1)+'/'+d.getDate()+' '+p(d.getHours())+':'+p(d.getMinutes());};
function cell(text,cls){const td=document.createElement('td');if(cls)td.className=cls;td.textContent=text;return td;}
function renderNotice(j){
  const box=$('noticecurrent'),tbody=$('noticereaders');box.replaceChildren();tbody.replaceChildren();
  if(!j.notice){const e=document.createElement('div');e.className='empty';e.textContent='게시된 공지가 없습니다.';box.append(e);
    const tr=document.createElement('tr');tr.append(cell('—','empty'));tr.firstChild.colSpan=2;tbody.append(tr);$('noticeunread').textContent='';return;}
  const h=document.createElement('h3'),m=document.createElement('div'),b=document.createElement('div');
  h.textContent=j.notice.title;m.className='nmeta';m.textContent='현재 공지 · '+stamp(j.notice.created)+' 게시 · 읽음 '+j.readers.length+'명';
  b.className='nbody';b.textContent=j.notice.body;box.append(h,m,b);
  for(const r of j.readers){const tr=document.createElement('tr');tr.append(cell(r.reader,'m'),cell(stamp(r.read_at),'m'));tbody.append(tr);}
  if(!j.readers.length){const tr=document.createElement('tr');tr.append(cell('아직 읽은 사람이 없습니다.','empty'));tr.firstChild.colSpan=2;tbody.append(tr);}
  $('noticeunread').textContent=j.unread.length?'안 읽은 계정 '+j.unread.length+'명: '+j.unread.join(', '):'';
}
async function loadNotice(){
  try{const j=await (await fetch('notice',{cache:'no-store'})).json();if(j.ok)renderNotice(j);}
  catch(e){$('noticecurrent').textContent='공지 조회 실패';}
}
$('noticeform').onsubmit=async e=>{
  e.preventDefault();
  if(!confirm('새 공지를 게시합니다. 사용자에게는 이 공지만 보이고 읽음 표시는 새로 시작됩니다.')) return;
  out.textContent='공지 게시 중…';
  const j=await (await fetch('notice',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:$('noticetitlein').value,body:$('noticebodyin').value})})).json();
  out.textContent=j.ok?'공지를 게시했습니다.':'실패: '+j.message;
  if(j.ok){$('noticeform').reset();renderNotice(j);}
};
load(); setInterval(load,15000);
health(); setInterval(health,20000);
loadNotice(); setInterval(loadNotice,30000);
</script>
<script>window.LAB_REPLAY={admin:true};</script>
<script src="assets/community-transport.js"></script>
<script src="assets/community-presence.js"></script>
<script src="assets/player.js"></script>
<script src="assets/replay.js"></script>
"""


# ─────────────────────────────── HTTP ───────────────────────────────

def render_panel(user):
    notice = ' 게스트 환경에서는 실습 VM이 다른 방문자와 공유됩니다.' if user == 'guest' else ''
    return PANEL.format(user=user, tutor_shared_notice=notice)


class Handler(BaseHTTPRequestHandler):
    server_version = "netlab"
    protocol_version = "HTTP/1.1"

    # ---- 공통 ----
    def _proxy_ok(self):
        # nginx 를 거쳤다는 증거. 시크릿이 비어 있으면(로드 실패) 아무도 통과 못 한다.
        import hmac
        got = self.headers.get("X-Lab-Proxy-Auth", "")
        return bool(PROXY_SECRET) and hmac.compare_digest(got, PROXY_SECRET)

    def _user(self):
        if not self._proxy_ok():
            return None
        u = self.headers.get("X-Lab-User", "")
        return u if NAME_RE.match(u) and u in accounts() else None

    def _admin(self):
        # 시크릿(= nginx 경유)을 먼저 확인한다. 그다음 값이 실제 관리자인지 본다.
        #   - 사내망 비상경로: basic auth 사용자 "admin" (admin.htpasswd)
        #   - 터널: _access_admin 이 이미 role==admin 을 검증한 계정명
        if not self._proxy_ok():
            return None
        a = self.headers.get("X-Lab-Admin", "")
        if not a:
            return None
        if a == "admin" or user_role(a) == "admin":
            return a
        return None

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _seg(self):
        return urlparse(self.path).path.strip("/").split("/")

    def _same_origin_json(self, limit):
        """Return an error tuple for cross-site, non-JSON or oversized writes, else None."""
        origin = self.headers.get('Origin')
        # nginx's $host omits the LAN listener port. Fetch Metadata keeps
        # browser cross-origin (including same-site) requests blocked.
        if (self.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site') or
            (origin and (urlparse(origin).scheme not in ('http', 'https') or
             urlparse(origin).hostname != urlparse('//' + self.headers.get('Host', '')).hostname))):
            return 403, '다른 사이트에서 보낸 요청입니다.'
        if self.headers.get('Content-Type', '').split(';', 1)[0].strip() != 'application/json':
            return 415, 'JSON 요청이 필요합니다.'
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if not 0 < length <= limit:
            return 413, '요청이 너무 큽니다.'
        return None

    # ---- Cloudflare Access 인증 (nginx auth_request 전용) ----
    def _access_check(self, require_admin=False):
        """
        Access 토큰을 검증하고, 요청 경로에 대한 권한까지 여기서 판정한다.

        nginx 는 200/401/403 만 보고 통과 여부를 정한다. 정책이 한 곳에 모여 있어야
        경로 블록이 늘어나도 구멍이 안 생긴다.
        """
        token = (self.headers.get("Cf-Access-Jwt-Assertion")
                 or self.headers.get("CF_Authorization") or "")
        email = verify_access_jwt(token)
        if not email:
            return self._send(401, "", "text/plain")

        acct, rec = lookup_or_create(email)
        if not acct:
            log(f"계정이 없고 자동 생성도 꺼져 있음: {email}")
            return self._send(403, "", "text/plain")
        if user_disabled(acct):
            log(f"차단된 계정의 접근 시도: {acct} <{email}>")
            return self._send(403, "", "text/plain")

        role = user_role(acct)
        if require_admin and role != "admin":
            return self._send(403, "", "text/plain")

        # 남의 환경 경로로 들어오는 것을 막는다 (관리자는 예외).
        uri = self.headers.get("X-Original-URI", "")
        m = re.match(r"^/u/([a-z][a-z0-9_-]{1,30})/", uri)
        if m and m.group(1) != acct and role != "admin":
            log(f"타인 경로 접근 차단: {acct} → {m.group(1)}")
            return self._send(403, "", "text/plain")

        self.send_response(200)
        self.send_header("X-Lab-User", acct)
        self.send_header("X-Lab-Role", role)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- 조회 ----
    def do_GET(self):
        seg = self._seg()

        if seg and seg[0] == "_access":
            return self._access_check(require_admin=False)
        if seg and seg[0] == "_access_admin":
            return self._access_check(require_admin=True)

        if seg and seg[0] == "admin":
            if not self._admin():
                return self._json(403, {"ok": False, "message": "관리자 인증 필요"})
            what = seg[1] if len(seg) > 1 else ""
            if what in ("", "panel"):
                return self._send(200, ADMIN, "text/html; charset=utf-8")
            if what == "health":
                return self._json(200, {"items": collect_health()})
            if what == "notice":
                try:
                    return self._json(200, notice_board.status(accounts()))
                except Exception:
                    return self._json(503, {"ok": False, "message": "공지 저장소를 열 수 없습니다."})
            # ── 세션 리플레이 ──
            if what == "assets" and len(seg) > 2:
                name = seg[2]
                types = {"player.js": "application/javascript",
                         "player.css": "text/css",
                         "replay.js": "application/javascript",
                         "community-transport.js": "application/javascript",
                         "community-presence.js": "application/javascript",
                         "replay.css": "text/css"}
                if name not in types:
                    return self._send(404, "", "text/plain")
                try:
                    with open(os.path.join(ASSET_DIR, name), "rb") as f:
                        return self._send(200, f.read(), types[name])
                except OSError:
                    return self._send(404, "", "text/plain")
            if what == "sessions":
                from urllib.parse import parse_qs
                q = parse_qs(urlparse(self.path).query)
                u = (q.get("user") or [""])[0]
                return self._json(200, {"sessions": list_sessions(u)})
            if what == "cast":
                from urllib.parse import parse_qs
                q = parse_qs(urlparse(self.path).query)
                u = (q.get("user") or [""])[0]
                sid = (q.get("session") or [""])[0]
                data = read_cast(u, sid)
                if data is None:
                    return self._send(404, "", "text/plain")
                return self._send(200, data, "application/x-asciicast")
            if what == "timeline":
                from urllib.parse import parse_qs
                q = parse_qs(urlparse(self.path).query)
                u = (q.get("user") or [""])[0]
                sid = (q.get("session") or [""])[0]
                return self._json(200, {"items": read_timeline(u, sid)})
            if what == "list":
                with LOCK:
                    users = [dict(user=u, state=r.get("status", "?"),
                                  instance=r.get("instance"), ip=r.get("ip"),
                                  created=r.get("created"), stopped_at=r.get("stopped_at"))
                             for u, r in sorted(STATE["users"].items())]
                    pool = [dict(instance=p["instance"], ip=p["ip"], ready=p.get("ready", False))
                            for p in STATE["pool"]]
                    cfg = {"reclaim_days": CONFIG["reclaim_days"],
                           "pool_size": CONFIG["pool_size"]}
                return self._json(200, {"users": users, "pool": pool, "config": cfg,
                                        "accounts": accounts()})
            return self._json(404, {"ok": False, "message": "없는 경로"})

        user = self._user()
        if not user:
            return self._json(403, {"ok": False, "message": "미인증"})
        what = seg[-1] if seg else ""

        # 단일 진입점(/). nginx 의 return 은 인증보다 먼저 실행되므로 리다이렉트를
        # 여기서 만든다 — 인증을 통과해야만 도달한다.
        if what == "whoami":
            self.send_response(302)
            self.send_header("Location", f"/u/{user}/ctl/")
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        if what in ("", "ctl", "panel"):
            return self._send(200, render_panel(user), "text/html; charset=utf-8")

        if what == "status":
            with LOCK:
                rec = STATE["users"].get(user)
                days = CONFIG["reclaim_days"]
                if rec:
                    rec["last_activity"] = iso()
            policy = (f"정지된 채 {days}일이 지나면 환경이 자동 회수됩니다."
                      if days else "자동 회수는 꺼져 있습니다.")
            if not rec:
                return self._json(200, {"state": "none", "policy": policy})
            return self._json(200, {
                "state": rec.get("status", "unknown"),
                "instance": rec.get("instance"), "ip": rec.get("ip"),
                "created": rec.get("created"), "stopped_at": rec.get("stopped_at"),
                "policy": policy,
            })

        # ── 본인 세션 복습 (반드시 자기 계정만) ──
        # user 는 X-Lab-User(시크릿 검증 통과) 이므로 신뢰할 수 있다. 쿼리의 user 는
        # 아예 무시하고 언제나 자기 계정으로만 조회한다.
        if seg and seg[0] == 'guide' and len(seg) == 2:
            from urllib.parse import parse_qs
            doc = knowledge.source(seg[1])
            if not doc:
                return self._send(404, '문서를 찾을 수 없습니다.', 'text/plain; charset=utf-8')
            expected = (parse_qs(urlparse(self.path).query).get('sha') or [''])[0]
            if expected and expected != doc['sha256']:
                return self._send(409, '교재가 업데이트되었습니다. 튜터에 다시 질문해 최신 문서 링크를 받아 주세요.', 'text/plain; charset=utf-8')
            return self._send(200, guide_view.render(doc), 'text/html; charset=utf-8')

        if seg and seg[0] == "assets" and len(seg) > 1:
            name = seg[1]
            types = {"player.js": "application/javascript", "player.css": "text/css",
                     "replay.js": "application/javascript", "replay.css": "text/css",
                     "tutor.js": "application/javascript", "tutor.css": "text/css",
                     "community.js": "application/javascript", "community.css": "text/css",
                     "community-transport.js": "application/javascript",
                     "notice.js": "application/javascript", "notice.css": "text/css",
                     "theme.js": "application/javascript", "terminal-theme.js": "application/javascript"}
            if name not in types:
                return self._send(404, "", "text/plain")
            try:
                with open(os.path.join(ASSET_DIR, name), "rb") as f:
                    return self._send(200, f.read(), types[name])
            except OSError:
                return self._send(404, "", "text/plain")
        if what == "sessions":
            return self._json(200, {"sessions": list_sessions(user)})
        if what == "cast":
            from urllib.parse import parse_qs
            sid = (parse_qs(urlparse(self.path).query).get("session") or [""])[0]
            data = read_cast(user, sid)
            if data is None:
                return self._send(404, "", "text/plain")
            return self._send(200, data, "application/x-asciicast")
        if what == "timeline":
            from urllib.parse import parse_qs
            sid = (parse_qs(urlparse(self.path).query).get("session") or [""])[0]
            return self._json(200, {"items": read_timeline(user, sid)})

        return self._json(404, {"ok": False, "message": "없는 경로"})

    # ---- 변경 ----
    def do_POST(self):
        seg = self._seg()

        # 관리자
        if seg and seg[0] == "admin":
            if not self._admin():
                return self._json(403, {"ok": False, "message": "관리자 인증 필요"})
            what = seg[1] if len(seg) > 1 else ""
            if what == 'community-ticket':
                rejected = self._same_origin_json(1024)
                if rejected:
                    return self._json(rejected[0], {'ok': False, 'message': rejected[1]})
                try:
                    body = self._body()
                    return self._json(200, community_auth.issue(PROXY_SECRET, self._admin(), body.get('client'), self.headers.get('Origin'), admin=True))
                except (ValueError, AttributeError) as exc:
                    return self._json(400, {'ok': False, 'message': str(exc)})
            if what == "notice":
                rejected = self._same_origin_json(32768)
                if rejected:
                    return self._json(rejected[0], {"ok": False, "message": rejected[1]})
                try:
                    notice_board.publish(self._admin(), self._body())
                    return self._json(200, notice_board.status(accounts()))
                except ValueError as exc:
                    return self._json(400, {"ok": False, "message": str(exc)})
                except Exception:
                    return self._json(503, {"ok": False, "message": "공지 저장소를 열 수 없습니다."})
            body = self._body()

            if what == "config":
                with LOCK:
                    if "reclaim_days" in body:
                        CONFIG["reclaim_days"] = max(0, float(body["reclaim_days"]))
                    if "pool_size" in body:
                        CONFIG["pool_size"] = max(0, min(5, int(body["pool_size"])))
                    if "idle_stop_hours" in body:
                        CONFIG["idle_stop_hours"] = max(0, float(body["idle_stop_hours"]))
                    save_config()
                return self._json(200, {"ok": True, "message": "저장했습니다."})

            target = body.get("user", "")
            if not NAME_RE.match(target):
                return self._json(400, {"ok": False, "message": "대상이 올바르지 않습니다"})
            if what == "stop":
                ok, msg = stop_for(target)
                return self._json(200, {"ok": ok, "message": msg})
            if what == "destroy":
                ok, msg = destroy_for(target)
                return self._json(200, {"ok": ok, "message": msg})
            return self._json(404, {"ok": False, "message": "없는 동작"})

        # 사용자
        user = self._user()
        if not user:
            return self._json(403, {"ok": False, "message": "미인증"})
        what = seg[-1] if seg else ""

        if what == 'community':
            try:
                rejected = self._same_origin_json(8192)
                if rejected:
                    return self._json(rejected[0], {'ok': False, 'message': rejected[1]})
                body = self._body()
                if isinstance(body, dict) and body.get('action') == 'ticket':
                    return self._json(200, community_auth.issue(PROXY_SECRET, user, body.get('client'), self.headers.get('Origin')))
                return self._json(200, community_chat.handle(user, body))
            except ValueError as exc:
                return self._json(400, {'ok': False, 'message': str(exc)})
            except Exception:
                return self._json(503, {'ok': False, 'message': '채팅을 사용할 수 없습니다. 잠시 후 다시 시도하세요.'})

        if what == 'notice':
            try:
                rejected = self._same_origin_json(1024)
                if rejected:
                    return self._json(rejected[0], {'ok': False, 'message': rejected[1]})
                return self._json(200, notice_board.handle(user, self._body()))
            except ValueError as exc:
                return self._json(400, {'ok': False, 'message': str(exc)})
            except Exception:
                return self._json(503, {'ok': False, 'message': '공지를 불러올 수 없습니다. 잠시 후 다시 시도하세요.'})

        if what == "tutor":
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n < 0 or n > 32768:
                    return self._json(413, {"ok": False, "message": "요청이 너무 큽니다."})
                body = self._body()
                if not isinstance(body, dict):
                    raise ValueError('잘못된 요청입니다.')
                if body.get('action') == 'models':
                    return self._json(200, tutor_chat.model_catalog())
                with LOCK:
                    rec = dict(STATE['users'].get(user) or {})
                if not rec.get('instance') or rec.get('status') != 'running':
                    raise ValueError('실습 환경을 먼저 시작하세요.')
                data = tutor_chat.handle(user, rec['instance'], str(rec.get('tutor_epoch') or rec.get('created', '')), body,
                                         shared_vm=CONFIG.get('auth_mode') == 'open')
                return self._json(200, data)
            except ValueError as e:
                return self._json(400, {"ok": False, "message": str(e)})
            except Exception:
                return self._json(503, {"ok": False, "message": "대화 저장소에 연결할 수 없습니다."})

        if what == "start":
            with LOCK:
                rec = STATE["users"].get(user)
                if rec and rec.get("status") in ("provisioning", "starting", "restoring"):
                    return self._json(200, {"ok": True, "message": "이미 처리 중입니다."})
                if not rec:
                    STATE["users"][user] = {"instance": None, "ip": None,
                                            "created": iso(), "last_activity": iso(),
                                            "stopped_at": None, "status": "provisioning"}
                    save_state()
                    threading.Thread(target=self._provision, args=(user,), daemon=True).start()
                    return self._json(200, {"ok": True, "message": "환경을 발급하고 있습니다."})
            threading.Thread(target=start_for, args=(user,), daemon=True).start()
            return self._json(200, {"ok": True, "message": "환경을 다시 시작합니다."})

        if what == "stop":
            ok, msg = stop_for(user)
            return self._json(200, {"ok": ok, "message": msg})
        if what == "destroy":
            ok, msg = destroy_for(user)
            return self._json(200, {"ok": ok, "message": msg})
        if what == "restore":
            ok, msg = restore_for(user)
            return self._json(200, {"ok": ok, "message": msg})

        if what == "help":
            return self._help(user)

        return self._json(404, {"ok": False, "message": "없는 동작"})

    def _help(self, user):
        """
        실습 도움말. netlab-tutor 가 VM 상태를 읽어 단계식 조언을 만든다.

        도움 요청 횟수(attempt)를 챕터별로 센다. 같은 챕터에서 여러 번 누를수록
        조언이 구체적으로 심화된다(소크라테스식 → 원인 명시). 통과하거나 챕터가
        바뀌면 카운터를 리셋한다.
        """
        with LOCK:
            rec = STATE["users"].get(user)
        if not rec or rec.get("status") != "running" or not rec.get("instance"):
            return self._json(200, {"ok": False,
                                    "message": "실행 중인 환경이 없습니다. 먼저 시작하세요."})
        instance = rec["instance"]
        try:
            # 배포된 챕터 자동 감지 + 상태 조회 + 단계식 조언 (전부 튜터가 한다)
            rc, out, err = _run(
                ["python3", "/opt/netlab-admin/netlab-tutor.py", "detect", instance], 30)
            chapter = out.strip() if rc == 0 else ""
            if not chapter:
                return self._json(200, {"ok": False,
                    "message": "지금 배포된 실습을 찾지 못했어요. 터미널에서 lab 01 처럼 실습을 먼저 배포하세요."})

            with LOCK:
                att = rec.setdefault("help_attempts", {})
                # 챕터가 바뀌면 이전 챕터 카운터는 남겨두되 현재 챕터만 증가
                att[chapter] = att.get(chapter, 0) + 1
                n = att[chapter]
                save_state()

            rc, out, err = _run(
                ["python3", "/opt/netlab-admin/netlab-tutor.py", "help",
                 instance, chapter, "--attempt", str(n)], 120)
            if rc != 0:
                return self._json(200, {"ok": False,
                                        "message": f"도움말 생성 실패: {(err or out)[:200]}"})
            data = json.loads(out)
            if data.get("ok") and data.get("passed"):
                # 이미 통과했으면 다음 도움은 처음부터
                with LOCK:
                    rec.get("help_attempts", {}).pop(chapter, None)
                    save_state()
            return self._json(200, data)
        except Exception as e:
            return self._json(200, {"ok": False, "message": f"도움말 오류: {e}"})

    def _provision(self, user):
        try:
            claim_for(user)
        except Exception as e:
            log(f"{user} 발급 실패: {e}")
            with LOCK:
                STATE["users"].pop(user, None)
                save_state()

    def log_message(self, fmt, *args):
        pass   # 접근 로그는 nginx 가 남긴다


if __name__ == "__main__":
    os.makedirs(HTPASSWD_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    os.makedirs("/etc/nginx/netlab.d", exist_ok=True)
    load_config()
    load_proxy_secret()
    load_users()
    load_state()
    reconcile()
    regen_routing()

    # netlab-user 가 계정을 추가/삭제한 뒤 SIGHUP 을 보내면 즉시 라우팅을 다시 만든다.
    def on_hup(_sig, _frm):
        log("SIGHUP — 계정 변경 반영")
        try:
            load_config()
            load_proxy_secret()
            load_users()
            regen_routing()
            # 계정이 지워졌다면 그 환경도 바로 회수한다 (주기 대기 없이).
            threading.Thread(target=reclaim_orphans, daemon=True).start()
        except Exception as e:
            log(f"라우팅 재생성 실패: {e}")
    signal.signal(signal.SIGHUP, on_hup)

    threading.Thread(target=maintainer, daemon=True).start()
    log(f"netlab 발급 서비스 시작 — {LISTEN[0]}:{LISTEN[1]}")
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()

"""설정·상태·사용자 테이블. 모두 메모리에 들고 있고 파일로 원자적으로 내려쓴다.

세 테이블은 재바인딩하지 않고 제자리에서 갱신한다(clear/update). 다른 모듈이
`store.STATE` 처럼 속성으로 접근하든 참조를 들고 있든 같은 객체를 보게 하기 위해서다.
"""
import json
import os
import threading

from . import paths
from .util import NAME_RE, log, write_atomic, now, iso, parse_iso

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

# 사용자 테이블: "들어온 사람이 우리 시스템에서 무엇인지"(역할·차단 여부).
# 누가 들어올 수 있는지는 Cloudflare allow list 가 정한다.
USERS = {}

# 사용자 테이블을 못 읽으면 인증을 잠근다(fail-closed).
# 예전엔 실패 시 빈 테이블로 떨어졌는데, 그러면 파일이 손상·삭제된 순간 테이블이 비고,
# "관리자 없으면 처음 들어온 사람이 admin" 로직과 맞물려 다음 로그인 사용자가 관리자가 됐다.
# 즉 파일 장애가 권한 상승으로 뒤집혔다. 이제는 못 읽으면 아무도 통과시키지 않는다.
USERS_LOADED = False

# nginx 만 아는 공유 시크릿. :9000 은 loopback 전용이지만, 그것만으로는
# "호스트에서 셸을 가진 아무 프로세스나 X-Lab-Admin 헤더를 위조해 관리자가 되는" 걸
# 못 막는다(신뢰가 헤더 유무 한 겹뿐). nginx 는 인증을 통과한 요청에만 이 시크릿을
# 실어 보내고, labd 는 시크릿이 맞는 요청의 신원 헤더만 신뢰한다.
PROXY_SECRET = ""


def _replace(target, value):
    target.clear()
    target.update(value)


# ─────────────────────────────── 설정 ───────────────────────────────

def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(paths.CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        write_atomic(paths.CONFIG_PATH, json.dumps(cfg, indent=2, ensure_ascii=False))
    except Exception as e:
        log(f"설정 읽기 실패, 기본값 사용: {e}")
    _replace(CONFIG, cfg)


def save_config():
    write_atomic(paths.CONFIG_PATH, json.dumps(CONFIG, indent=2, ensure_ascii=False))


def load_proxy_secret():
    global PROXY_SECRET
    try:
        with open(paths.PROXY_SECRET_PATH) as f:
            PROXY_SECRET = f.read().strip()
    except Exception as e:
        PROXY_SECRET = ""
        log(f"프록시 시크릿 읽기 실패 — 신원 헤더 신뢰 불가(fail-closed): {e}")


# ─────────────────────────────── 상태 ───────────────────────────────

def load_state():
    try:
        with open(paths.STATE_PATH) as f:
            s = json.load(f)
        _replace(STATE, {"users": s.get("users", {}), "pool": s.get("pool", [])})
    except FileNotFoundError:
        _replace(STATE, {"users": {}, "pool": []})
    except Exception as e:
        log(f"상태 읽기 실패, 초기화: {e}")
        _replace(STATE, {"users": {}, "pool": []})


def save_state():
    write_atomic(paths.STATE_PATH, json.dumps(STATE, indent=2, ensure_ascii=False), 0o600)


# ─────────────────────────── 최근 접속 ───────────────────────────
# 계정별로 labd 가 마지막으로 본 사용자 요청 시각. 인증 방식(Access·LAN·guest)과 무관하게
# 기록되며, 패널 폴링마다 쓰지 않도록 계정당 VISIT_WRITE_INTERVAL 초에 한 번만 저장한다.
VISITS = {}
VISIT_WRITE_INTERVAL = 60


def load_visits():
    try:
        with open(paths.VISITS_PATH) as f:
            data = json.load(f)
        _replace(VISITS, {k: v for k, v in data.items() if isinstance(v, str)})
    except FileNotFoundError:
        VISITS.clear()
    except Exception as e:
        log(f"최근 접속 기록 읽기 실패, 초기화: {e}")
        VISITS.clear()


def touch_visit(account):
    with LOCK:
        previous = VISITS.get(account)
        current = now()
        VISITS[account] = iso(current)
        earlier = parse_iso(previous) if previous else None
        if earlier and (current - earlier).total_seconds() < VISIT_WRITE_INTERVAL:
            return
        try:
            write_atomic(paths.VISITS_PATH, json.dumps(VISITS, indent=2, ensure_ascii=False), 0o600)
        except OSError as e:
            log(f"최근 접속 기록 저장 실패: {e}")


def last_visit(account):
    """labd 가 본 마지막 요청과 Access 로그인 시각 중 늦은 쪽."""
    candidates = [VISITS.get(account), (USERS.get(account) or {}).get("last_login")]
    return max((c for c in candidates if isinstance(c, str)), default=None)


# ─────────────────────────── 사용자 테이블 ───────────────────────────

def load_users():
    global USERS_LOADED
    try:
        with open(paths.USERS_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("최상위가 객체가 아님")
        _replace(USERS, data)
        USERS_LOADED = True
    except FileNotFoundError:
        # 아직 아무도 안 들어온 최초 상태 — 빈 테이블이 정상이다.
        USERS.clear()
        USERS_LOADED = True
    except Exception as e:
        # 손상·파싱 실패 — 메모리의 기존 테이블을 유지하고 인증을 잠근다.
        log(f"사용자 테이블 읽기 실패 — fail-closed 로 잠금: {e}")
        USERS_LOADED = False


def save_users():
    write_atomic(paths.USERS_PATH, json.dumps(USERS, indent=2, ensure_ascii=False), 0o600)


def user_role(account):
    return (USERS.get(account) or {}).get("role", "user")


def user_disabled(account):
    return bool((USERS.get(account) or {}).get("disabled"))


def accounts():
    """실습 계정 = htpasswd 가 있거나(사내망 로그인) 사용자 테이블에 있는 것."""
    names = set()
    try:
        names |= {n for n in os.listdir(paths.HTPASSWD_DIR) if NAME_RE.match(n)}
    except FileNotFoundError:
        pass
    names |= {n for n in USERS if NAME_RE.match(n)}
    if CONFIG.get("auth_mode") == "open":
        names.add("guest")
    return sorted(names)

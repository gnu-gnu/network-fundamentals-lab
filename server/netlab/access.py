"""Cloudflare Access 인증.

이메일 헤더만 믿으면 안 된다. nginx 는 0.0.0.0 에서 듣고 있어 누구나
  curl -H 'Cf-Access-Authenticated-User-Email: admin@...'
로 위조할 수 있기 때문이다. Cloudflare 가 서명한 JWT 를 검증해야 안전하다.
"""
import json
import os
import re
import time
import urllib.request

from . import paths, store
from .util import iso, log

_JWKS = {"keys": [], "fetched": 0}


def jwks():
    now_ts = time.time()
    if _JWKS["keys"] and now_ts - _JWKS["fetched"] < 3600:
        return _JWKS["keys"]
    url = f"https://{store.CONFIG['access_team_domain']}/cdn-cgi/access/certs"
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
    raw = store.CONFIG.get("access_aud") or ""
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
            issuer=f"https://{store.CONFIG['access_team_domain']}",
        )
        # email 만 신원으로 인정한다. common_name 은 서비스 토큰·mTLS 경로에서 오는
        # 값이라 SSO 이메일과 신뢰 수준이 다르다 — fallback 으로 쓰면 안 된다.
        return claims.get("email")
    except Exception as e:
        log(f"Access 토큰 검증 실패: {e}")
        return None


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
    from . import nginx   # 순환 import 회피 (nginx → store 만 본다)

    # 테이블을 신뢰할 수 없으면 아무도 통과시키지 않는다(fail-closed).
    if not store.USERS_LOADED:
        log("사용자 테이블이 잠겨 있어 로그인 거부")
        return None, None

    with store.LOCK:
        for acct, rec in store.USERS.items():
            if (rec.get("email") or "").lower() == email.lower():
                rec["last_login"] = iso()
                store.save_users()
                return acct, rec

        if not store.CONFIG.get("access_auto_create", True):
            return None, None

        base = account_from_email(email)
        acct, n = base, 1
        while acct in store.USERS or os.path.exists(os.path.join(paths.HTPASSWD_DIR, acct)):
            n += 1
            acct = f"{base}{n}"[:31]

        # 새 계정은 언제나 일반 사용자다. 관리자 승격은 사람이 명시적으로 한다
        # (netlab-user role). "관리자 없으면 자동 admin" 은 파일 장애 시 권한 상승으로
        # 뒤집히므로 제거했다.
        store.USERS[acct] = {"email": email, "role": "user", "created": iso(),
                             "last_login": iso(), "disabled": False}
        store.save_users()
        log(f"Access 최초 로그인 — 계정 생성: {acct} <{email}> 역할=user")
    nginx.regen_routing()
    return acct, store.USERS[acct]

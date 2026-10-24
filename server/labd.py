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

이 파일은 HTTP 진입점만 담당한다. 설정·상태·VM 수명·nginx·녹화·유지 작업은 netlab/ 패키지에 있다.
"""
import hmac
import json
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import community_auth
import community_chat
import guide_view
import knowledge
import netlab_tutor
import notice_board
import tutor_chat
import tutor_models
from netlab import lessons, paths, store
from netlab.access import lookup_or_create, verify_access_jwt
from netlab.health import collect_health
from netlab.instances import claim_for, destroy_for, restore_for, start_for, stop_for
from netlab.maintainer import maintainer, reclaim_orphans, reconcile
from netlab.nginx import regen_routing
from netlab.recordings import list_sessions, read_cast, read_timeline
from netlab.store import CONFIG, LOCK, STATE, accounts, save_state, user_disabled, user_role
from netlab.util import NAME_RE, iso, log

# ─────────────────────────────── 화면 ───────────────────────────────

def _template(name):
    with open(os.path.join(paths.TEMPLATE_DIR, name), encoding="utf-8") as f:
        return f.read()


PANEL = _template("panel.html")
ADMIN = _template("admin.html")


def render_panel(user):
    notice = ' 게스트 환경에서는 실습 VM이 다른 방문자와 공유됩니다.' if user == 'guest' else ''
    return PANEL.replace("__USER__", user).replace("__TUTOR_SHARED_NOTICE__", notice)


# 브라우저에 내려주는 정적 파일. 확장자로 MIME 을 정하고, 허용 목록 밖 이름은 404.
ASSET_TYPES = {".js": "application/javascript", ".css": "text/css"}
USER_ASSETS = {"player.js", "player.css", "replay.js", "replay.css", "tutor.js", "tutor.css",
               "community.js", "community.css", "community-transport.js",
               "notice.js", "notice.css", "theme.js", "terminal-theme.js", "lesson.js", "lesson.css",
               "panel-chrome.css"}
ADMIN_ASSETS = {"player.js", "player.css", "replay.js", "replay.css",
                "community-transport.js", "community-presence.js", "admin-models.js", "admin-models.css"}

HELP_TIMEOUT = 120   # 도움말(LLM) 생성 상한(초)
_HELP_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="help")


# ─────────────────────────────── HTTP ───────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "netlab"
    protocol_version = "HTTP/1.1"

    # ---- 공통 ----
    def _proxy_ok(self):
        # nginx 를 거쳤다는 증거. 시크릿이 비어 있으면(로드 실패) 아무도 통과 못 한다.
        got = self.headers.get("X-Lab-Proxy-Auth", "")
        return bool(store.PROXY_SECRET) and hmac.compare_digest(got, store.PROXY_SECRET)

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

    def _fail(self, code, message):
        self._json(code, {"ok": False, "message": message})

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def _seg(self):
        return urlparse(self.path).path.strip("/").split("/")

    def _query(self, key):
        return (parse_qs(urlparse(self.path).query).get(key) or [""])[0]

    def _asset(self, name, allowed):
        ctype = ASSET_TYPES.get(os.path.splitext(name)[1])
        if name not in allowed or not ctype:
            return self._send(404, "", "text/plain")
        try:
            with open(os.path.join(paths.ASSET_DIR, name), "rb") as f:
                return self._send(200, f.read(), ctype)
        except OSError:
            return self._send(404, "", "text/plain")

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
            return 415, 'JSON 요청이 필요합니다. 페이지를 새로고침한 뒤 다시 시도하세요.'
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

        # 남의 환경으로 들어오는 것을 막는다 (관리자는 예외). 대상은 nginx가
        # 생성한 location의 고정 auth_request 인자에서 오며, 원본 요청 URI를
        # 다시 파싱하지 않는다. nginx의 location 매칭 URI와 $request_uri의
        # 정규화 차이를 이용한 우회를 피하기 위해서다.
        if self.headers.get("X-Lab-Auth-Scheme") != "target":
            # 옛 nginx 사이트(X-Original-URI)는 대상 사용자를 알려주지 못한다. 검사를 건너뛰어
            # 열어 두는 대신 거부한다 — README '기존 운영 서버 업데이트' 의 사이트 갱신이 필요하다.
            log("nginx 사이트가 target 기반 auth_request 를 보내지 않아 거부 (사이트 갱신 필요)")
            return self._send(403, "", "text/plain")
        target = self.headers.get("X-Lab-Target-User", "")
        if target and (not NAME_RE.match(target) or
                       (target != acct and role != "admin")):
            log(f"타인 경로 접근 차단: {acct} → {target}")
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
            return self._admin_get(seg[1] if len(seg) > 1 else "", seg)

        user = self._user()
        if not user:
            return self._fail(403, "미인증")
        if not (seg and seg[0] == "assets"):
            store.touch_visit(user)
        return self._user_get(user, seg)

    def _admin_get(self, what, seg):
        if not self._admin():
            return self._fail(403, "관리자 인증 필요")
        if what in ("", "panel"):
            return self._send(200, ADMIN, "text/html; charset=utf-8")
        if what == "health":
            return self._json(200, {"items": collect_health()})
        if what == 'models':
            try:
                return self._json(200, tutor_models.catalog())
            except (OSError, ValueError):
                return self._fail(503, '모델 설정을 읽을 수 없습니다.')
        if what == "notice":
            try:
                return self._json(200, notice_board.status(accounts()))
            except Exception:
                return self._fail(503, "공지 저장소를 열 수 없습니다.")
        # ── 세션 리플레이 ──
        if what == "assets" and len(seg) > 2:
            return self._asset(seg[2], ADMIN_ASSETS)
        if what == "sessions":
            return self._json(200, {"sessions": list_sessions(self._query("user"))})
        if what == "cast":
            data = read_cast(self._query("user"), self._query("session"))
            if data is None:
                return self._send(404, "", "text/plain")
            return self._send(200, data, "application/x-asciicast")
        if what == "timeline":
            return self._json(200, {"items": read_timeline(self._query("user"), self._query("session"))})
        if what == "list":
            with LOCK:
                users = [dict(user=u, state=r.get("status", "?"),
                              instance=r.get("instance"), ip=r.get("ip"),
                              created=r.get("created"), stopped_at=r.get("stopped_at"),
                              last_visit=store.last_visit(u))
                         for u, r in sorted(STATE["users"].items())]
                names = accounts()
                visits = {a: store.last_visit(a) for a in names}
                pool = [dict(instance=p["instance"], ip=p["ip"], ready=p.get("ready", False))
                        for p in STATE["pool"]]
                cfg = {"reclaim_days": CONFIG["reclaim_days"],
                       "pool_size": CONFIG["pool_size"]}
            return self._json(200, {"users": users, "pool": pool, "config": cfg,
                                    "accounts": names, "visits": visits})
        return self._fail(404, "없는 경로")

    def _user_get(self, user, seg):
        what = seg[-1] if seg else ""

        # 학습 팩 v2 단계형 실습. 대상 환경은 언제나 인증된 계정의 소유 기록에서 정한다.
        if seg and seg[0] == "lesson":
            return self._json(*lessons.handle_get(user, seg))

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

        if seg and seg[0] == 'guide' and len(seg) == 2:
            doc = knowledge.source(seg[1])
            if not doc:
                return self._send(404, '문서를 찾을 수 없습니다.', 'text/plain; charset=utf-8')
            expected = self._query('sha')
            if expected and expected != doc['sha256']:
                return self._send(409, '교재가 업데이트되었습니다. 튜터에 다시 질문해 최신 문서 링크를 받아 주세요.', 'text/plain; charset=utf-8')
            return self._send(200, guide_view.render(doc), 'text/html; charset=utf-8')

        if seg and seg[0] == "assets" and len(seg) > 1:
            return self._asset(seg[1], USER_ASSETS)

        # ── 본인 세션 복습 (반드시 자기 계정만) ──
        # user 는 X-Lab-User(시크릿 검증 통과) 이므로 신뢰할 수 있다. 쿼리의 user 는
        # 아예 무시하고 언제나 자기 계정으로만 조회한다.
        if what == "sessions":
            return self._json(200, {"sessions": list_sessions(user)})
        if what == "cast":
            data = read_cast(user, self._query("session"))
            if data is None:
                return self._send(404, "", "text/plain")
            return self._send(200, data, "application/x-asciicast")
        if what == "timeline":
            return self._json(200, {"items": read_timeline(user, self._query("session"))})

        return self._fail(404, "없는 경로")

    # ---- 변경 ----
    def do_POST(self):
        seg = self._seg()
        if seg and seg[0] == "admin":
            return self._admin_post(seg[1] if len(seg) > 1 else "")
        user = self._user()
        if not user:
            return self._fail(403, "미인증")
        store.touch_visit(user)
        if seg and seg[0] == "lesson":
            return self._lesson_post(user, seg)
        return self._user_post(user, seg[-1] if seg else "")

    def _lesson_post(self, user, seg):
        rejected = self._same_origin_json(4096)
        if rejected:
            return self._fail(*rejected)
        body = self._body()
        if not isinstance(body, dict):
            return self._fail(400, "잘못된 요청입니다.")
        return self._json(*lessons.handle_post(user, seg, body))

    def _admin_post(self, what):
        admin = self._admin()
        if not admin:
            return self._fail(403, "관리자 인증 필요")
        rejected = self._same_origin_json(32768)
        if rejected:
            return self._fail(*rejected)
        if what == 'models':
            rejected = self._same_origin_json(16384)
            if rejected:
                return self._fail(*rejected)
            try:
                body = self._body()
                data = tutor_models.update(body)
                log(f"튜터 모델 설정 변경: admin={admin} action={body.get('action')} id={body.get('id')}")
                return self._json(200, data)
            except tutor_models.Conflict as exc:
                return self._fail(409, str(exc))
            except ValueError as exc:
                return self._fail(400, str(exc))
            except OSError:
                return self._fail(503, '모델 설정을 저장할 수 없습니다.')
        if what == 'community-ticket':
            rejected = self._same_origin_json(1024)
            if rejected:
                return self._fail(*rejected)
            try:
                body = self._body()
                return self._json(200, community_auth.issue(store.PROXY_SECRET, admin, body.get('client'),
                                                            self.headers.get('Origin'), admin=True))
            except (ValueError, AttributeError) as exc:
                return self._fail(400, str(exc))
        if what == "notice":
            rejected = self._same_origin_json(32768)
            if rejected:
                return self._fail(*rejected)
            try:
                notice_board.publish(admin, self._body())
                return self._json(200, notice_board.status(accounts()))
            except ValueError as exc:
                return self._fail(400, str(exc))
            except Exception:
                return self._fail(503, "공지 저장소를 열 수 없습니다.")
        body = self._body()

        if what == "config":
            with LOCK:
                if "reclaim_days" in body:
                    CONFIG["reclaim_days"] = max(0, float(body["reclaim_days"]))
                if "pool_size" in body:
                    CONFIG["pool_size"] = max(0, min(5, int(body["pool_size"])))
                if "idle_stop_hours" in body:
                    CONFIG["idle_stop_hours"] = max(0, float(body["idle_stop_hours"]))
                store.save_config()
            return self._json(200, {"ok": True, "message": "저장했습니다."})

        target = body.get("user", "")
        if not NAME_RE.match(target):
            return self._fail(400, "대상이 올바르지 않습니다")
        if what == "stop":
            ok, msg = stop_for(target)
            return self._json(200, {"ok": ok, "message": msg})
        if what == "destroy":
            ok, msg = destroy_for(target)
            return self._json(200, {"ok": ok, "message": msg})
        return self._fail(404, "없는 동작")

    def _user_post(self, user, what):
        rejected = self._same_origin_json(32768)
        if rejected:
            return self._fail(*rejected)
        if what == 'community':
            try:
                rejected = self._same_origin_json(8192)
                if rejected:
                    return self._fail(*rejected)
                body = self._body()
                if isinstance(body, dict) and body.get('action') == 'ticket':
                    return self._json(200, community_auth.issue(store.PROXY_SECRET, user, body.get('client'),
                                                                self.headers.get('Origin')))
                return self._json(200, community_chat.handle(user, body))
            except ValueError as exc:
                return self._fail(400, str(exc))
            except Exception:
                return self._fail(503, '채팅을 사용할 수 없습니다. 잠시 후 다시 시도하세요.')

        if what == 'notice':
            try:
                rejected = self._same_origin_json(1024)
                if rejected:
                    return self._fail(*rejected)
                return self._json(200, notice_board.handle(user, self._body()))
            except ValueError as exc:
                return self._fail(400, str(exc))
            except Exception:
                return self._fail(503, '공지를 불러올 수 없습니다. 잠시 후 다시 시도하세요.')

        if what == "tutor":
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n < 0 or n > 32768:
                    return self._fail(413, "요청이 너무 큽니다.")
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
                return self._fail(400, str(e))
            except Exception:
                return self._fail(503, "대화 저장소에 연결할 수 없습니다.")

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

        return self._fail(404, "없는 동작")

    def _help(self, user):
        """
        실습 도움말. netlab_tutor 가 VM 상태를 읽어 단계식 조언을 만든다.

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
            chapter = netlab_tutor.deployed_chapter(instance)
            if not chapter:
                return self._json(200, {"ok": False,
                    "message": "지금 배포된 실습을 찾지 못했어요. 터미널에서 lab 01 처럼 실습을 먼저 배포하세요."})

            with LOCK:
                att = rec.setdefault("help_attempts", {})
                # 챕터가 바뀌면 이전 챕터 카운터는 남겨두되 현재 챕터만 증가
                att[chapter] = att.get(chapter, 0) + 1
                n = att[chapter]
                save_state()

            # LLM 호출이 걸릴 수 있어 상한을 둔다. (예전 서브프로세스 timeout 과 같은 값)
            data = _HELP_POOL.submit(netlab_tutor.help_, instance, chapter, n).result(timeout=HELP_TIMEOUT)
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


def main():
    os.makedirs(paths.HTPASSWD_DIR, exist_ok=True)
    os.makedirs(paths.VAR, exist_ok=True)
    os.makedirs(paths.NGINX_SNIPPET_DIR, exist_ok=True)
    store.load_config()
    store.load_proxy_secret()
    store.load_users()
    store.load_state()
    store.load_visits()
    lessons.startup()
    reconcile()
    regen_routing()

    # netlab-user 가 계정을 추가/삭제한 뒤 SIGHUP 을 보내면 즉시 라우팅을 다시 만든다.
    def on_hup(_sig, _frm):
        log("SIGHUP — 계정 변경 반영")
        try:
            store.load_config()
            store.load_proxy_secret()
            store.load_users()
            regen_routing()
            # 계정이 지워졌다면 그 환경도 바로 회수한다 (주기 대기 없이).
            threading.Thread(target=reclaim_orphans, daemon=True).start()
        except Exception as e:
            log(f"라우팅 재생성 실패: {e}")
    signal.signal(signal.SIGHUP, on_hup)

    threading.Thread(target=maintainer, daemon=True).start()
    log(f"netlab 발급 서비스 시작 — {paths.LISTEN[0]}:{paths.LISTEN[1]}")
    ThreadingHTTPServer(paths.LISTEN, Handler).serve_forever()


if __name__ == "__main__":
    main()

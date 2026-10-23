"""배경 유지 작업: 웜 풀 보충·치유, 고아 회수, 유휴 정지, 장기 정지 파기, 재시작 시 정합."""
import json
import os
import time

from . import paths, store
from .instances import (create_instance, destroy_for, drop_pool, enforce_isolation,
                        ensure_lab_permissions, finish_ready, instance_state,
                        stop_for, ttyd_ready)
from .nginx import regen_routing
from .procs import lxc
from .recordings import collect_recordings
from .store import CONFIG, LOCK, STATE, save_state
from .util import iso, log, now, parse_iso

INTERVAL = 30   # 예비 환경을 빨리 되채워야 다음 사람이 기다리지 않는다.


def reclaim_orphans():
    """
    계정이 사라진 환경을 회수한다.

    netlab-user 가 상태 파일을 직접 고치면 labd 가 메모리에 들고 있는 내용에 덮여
    되살아난다. 그래서 계정 삭제는 htpasswd 만 지우게 하고, 실제 회수는 여기서 한다.
    """
    if not os.path.isdir(paths.HTPASSWD_DIR):
        return                       # 디렉터리를 못 읽는 상황에서 전부 지우지 않도록
    live = set(store.accounts())
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
            drop_pool(name)
            continue
        if st != "Running":
            lxc("start", name)
            continue
        if ttyd_ready(ip) and ensure_lab_permissions(name):
            lxc("snapshot", name, "clean")   # 이미 있으면 실패하지만 무해하다
            owner = finish_ready(name)
            if owner:
                log(f"{owner} ← 준비 중이던 환경 배정 복구 {name} ({ip})")
                regen_routing()
            else:
                log(f"예비 환경 준비 완료(복구): {name} ({ip})")
            continue

        # 너무 오래 준비되지 않으면 버리고 다시 만든다.
        born = parse_iso(p.get("created") or "")
        if born and (now() - born).total_seconds() > CONFIG["ready_timeout"] * 2:
            log(f"준비되지 않는 예비 환경 폐기: {name}")
            lxc("delete", name, "--force")
            drop_pool(name)


def fill_pool():
    with LOCK:
        # A VM reserved for a direct claim is not a warm spare; do not let it count.
        have = len([p for p in STATE["pool"] if not p.get("claim")])
    want = max(0, int(CONFIG.get("pool_size", 0)))
    if have < want:
        try:
            create_instance()
        except Exception as e:
            log(f"예비 환경 생성 실패: {e}")


def recent_terminal_activity(instance, since_ts):
    """
    VM 안 세션 기록(.cast/.commands.jsonl)이 since_ts 이후 갱신됐는지 본다.
    전체화면 터미널만 열고 패널을 닫은 사용자도 '활동 중'으로 잡기 위한 것.
    실패하면 보수적으로 True(활동 중으로 간주 → 정지 안 함)를 돌려준다.
    """
    try:
        rc, out, _ = lxc("exec", instance, "--", "bash", "-c",
                         f"stat -c %Y {paths.VM_RECORD_DIR}/*.cast "
                         f"{paths.VM_RECORD_DIR}/*.commands.jsonl 2>/dev/null | sort -n | tail -1",
                         timeout=20)
        if rc != 0 or not out.strip():
            return False          # 기록이 없으면 최근 활동 없음
        return int(out.strip()) >= since_ts
    except Exception:
        return True               # 못 읽으면 함부로 정지하지 않는다


def _users_where(status, field, before_ts):
    """status 인 환경 중 field 시각이 before_ts 보다 오래된 (user, instance) 목록."""
    found = []
    with LOCK:
        for u, rec in STATE["users"].items():
            if rec.get("status") != status:
                continue
            t = parse_iso(rec.get(field) or "")
            if t and t.timestamp() < before_ts:
                found.append((u, rec.get("instance")))
    return found


def collect_running():
    """실행 중인 환경의 세션 기록을 주기적으로 호스트로 옮긴다(예상치 못한 종료 대비)."""
    with LOCK:
        running = [(u, r["instance"]) for u, r in STATE["users"].items()
                   if r.get("status") == "running" and r.get("instance")]
    for u, inst in running:
        collect_recordings(u, inst)


def idle_stop():
    """
    유휴 자동 정지 — 실행 중이나 오래 접속(패널 열림)이 없으면 일시정지한다.
    last_activity 는 패널이 status 를 폴링할 때 갱신되므로,
    "패널(=터미널)이 닫힌 지 오래됐다" = "안 쓰고 방치 중" 을 뜻한다.
    stop_for 는 정상 종료라 작업이 보존된다. 이후 reclaim_days 카운트가 시작된다.
    """
    idle_h = float(CONFIG.get("idle_stop_hours", 0))
    if idle_h <= 0:
        return
    icut = now().timestamp() - idle_h * 3600
    for u, inst in _users_where("running", "last_activity", icut):
        # 패널 폴링이 끊긴 지 오래여도, 전체화면 터미널로 아직 쓰는 중일 수 있다.
        # 그래서 VM 안 녹화 파일이 최근에 갱신됐는지도 본다(진짜 터미널 활동).
        # 둘 다 오래됐을 때만 정지한다.
        if inst and recent_terminal_activity(inst, icut):
            continue
        log(f"유휴 자동 정지: {u} (접속·터미널 {idle_h}시간 활동 없음)")
        stop_for(u)


def reclaim_stale():
    """자동 회수 — 정지된 지 오래된 것만. 실행 중인 환경은 건드리지 않는다."""
    days = float(CONFIG.get("reclaim_days", 7))
    if days <= 0:
        return
    cutoff = now().timestamp() - days * 86400
    for u, _ in _users_where("stopped", "stopped_at", cutoff):
        log(f"자동 회수: {u} (정지 {days}일 경과)")
        destroy_for(u)


def tick():
    """유지 작업 한 바퀴."""
    store.load_config()
    store.load_users()
    enforce_isolation()   # 브리지 격리 — 새로 뜬 VM 의 포트를 놓치지 않도록 매번 확인
    reclaim_orphans()
    heal_pool()
    fill_pool()
    collect_running()
    idle_stop()
    reclaim_stale()


def maintainer():
    """예비 환경 보충 + 오래 정지된 환경 자동 회수. 데몬 스레드로 돈다."""
    while True:
        try:
            tick()
        except Exception as e:
            log(f"유지 작업 오류: {e}")
        time.sleep(INTERVAL)


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

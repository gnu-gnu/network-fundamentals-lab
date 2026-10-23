"""실습 VM 수명 관리: 생성 → 웜 풀 → 배정 → 시작/정지/초기화 → 파기."""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
import uuid

from . import paths, store
from .nginx import regen_routing
from .procs import lxc
from .recordings import collect_recordings
from .store import CONFIG, LOCK, STATE, save_state
from .util import iso, log


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
                   os.path.join(paths.CODE_DIR, 'lab-shell.sh'),
                   name + '/etc/lab-shell.sh', timeout=30)
    return rc == 0


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


def _wait_ready(name, ip):
    """터미널이 응답하고 실습 계정 권한까지 갖춰질 때까지 ready_timeout 안에서 기다린다."""
    deadline = time.time() + CONFIG["ready_timeout"]
    while time.time() < deadline:
        if ttyd_ready(ip) and ensure_lab_permissions(name):
            return True
        time.sleep(4)
    return False


def mark_pool_ready(name):
    with LOCK:
        for p in STATE["pool"]:
            if p["instance"] == name:
                p["ready"] = True
        save_state()


def drop_pool(name):
    with LOCK:
        STATE["pool"] = [p for p in STATE["pool"] if p["instance"] != name]
        save_state()


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
        drop_pool(name)
        raise RuntimeError(f"생성 실패: {err}")

    rc, _, err = lxc("config", "device", "override", name, "eth0",
                     f"ipv4.address={ip}")
    if rc != 0:
        lxc("delete", name, "--force")
        drop_pool(name)
        raise RuntimeError(f"IP 배정 실패: {err}")

    rc, _, err = lxc("start", name)
    if rc != 0:
        lxc("delete", name, "--force")
        drop_pool(name)
        raise RuntimeError(f"기동 실패: {err}")
    enforce_isolation()      # 뜨자마자 격리 — 주기 대기 중 노출되는 틈을 없앤다

    if _wait_ready(name, ip):
        # 되돌리기 기준점. 이 이름이어야 "초기화"가 동작한다.
        lxc("snapshot", name, "clean")
        mark_pool_ready(name)
        log(f"예비 환경 준비 완료: {name} ({ip})")
        return name, ip

    lxc("delete", name, "--force")
    drop_pool(name)
    raise RuntimeError("준비 시간이 초과됐습니다")


def _assign(user, name, ip, created=None):
    STATE["users"][user] = {
        "instance": name, "ip": ip, "created": created or iso(),
        "last_activity": iso(), "stopped_at": None, "status": "running",
    }


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
                _assign(user, p["instance"], p["ip"], p.get("created"))
                save_state()
                log(f"{user} ← 예비 환경 즉시 배정 {p['instance']}")
                regen_routing()
                return

    name, ip = create_instance()          # 풀이 비었으면 직접 생성
    with LOCK:
        STATE["pool"] = [p for p in STATE["pool"] if p["instance"] != name]
        _assign(user, name, ip)
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

    name, ip = rec["instance"], rec["ip"]
    if instance_state(name) != "Running":
        rc, _, err = lxc("start", name)
        if rc != 0:
            with LOCK:
                rec["status"] = "error"
                rec["error"] = err
                save_state()
            return
        enforce_isolation()
    ready = _wait_ready(name, ip)
    with LOCK:
        rec["status"] = "running" if ready else "error"
        if not ready:
            rec['error'] = '터미널 또는 실습 계정 권한 준비 실패'
        rec["stopped_at"] = None
        rec["last_activity"] = iso()
        save_state()
    regen_routing()


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

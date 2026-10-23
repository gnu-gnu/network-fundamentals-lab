"""관리자 콘솔용 감시 지표. 각 항목은 ok/warn/crit 상태를 스스로 판정한다."""
import json
import os
import urllib.request

from . import paths
from .procs import run
from .store import CONFIG, LOCK, STATE

HEALTH_SVCS = ["snap.lxd.daemon", "nginx", "labd", "docker",
               "lxd-docker-fix", "netlab-netguard", "cloudflared"]


def _level(value, warn, crit):
    return "ok" if value < warn else ("warn" if value < crit else "crit")


def collect_health():
    items = []

    def add(key, label, value, level="ok", detail=""):
        items.append({"key": key, "label": label, "value": value,
                      "level": level, "detail": detail})

    # 1) 서비스
    down = [s for s in HEALTH_SVCS if run(["systemctl", "is-active", s], 5)[1] != "active"]
    add("services", "서비스",
        "모두 정상" if not down else f"{len(down)}개 비정상",
        "ok" if not down else "crit",
        "" if not down else "중단: " + ", ".join(down))

    # 2) 저장소 (ZFS 풀)
    rc, out, _ = run(["zpool", "list", "-H", "-o", "health,capacity", "labpool"], 8)
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
    rc, out, _ = run(["df", "-h", "--output=pcent,avail", "/"], 5)
    if rc == 0:
        line = out.splitlines()[-1].split()
        pct = int(line[0].rstrip("%"))
        add("rootfs", "루트 디스크", f"{pct}% 사용 · {line[1]} 여유", _level(pct, 85, 92))

    # 4) 메모리
    rc, out, _ = run(["free", "-m"], 5)
    if rc == 0:
        for ln in out.splitlines():
            if ln.startswith("Mem:"):
                f = ln.split()
                total, avail = int(f[1]), int(f[-1])
                pct = round((total - avail) / total * 100)
                add("mem", "메모리", f"{pct}% 사용 · {avail // 1024}GB 가용", _level(pct, 85, 93))
                break

    # 5) 부하
    try:
        la1 = os.getloadavg()[0]
        ncpu = os.cpu_count() or 1
        add("load", "부하", f"{la1:.2f} / {ncpu}코어", _level(la1 / ncpu, 1.5, 3))
    except Exception:
        pass

    # 6) 터널 연결
    try:
        with urllib.request.urlopen(f"{paths.CF_METRICS}/ready", timeout=5) as r:
            d = json.load(r)
        conns = d.get("readyConnections", 0)
        add("tunnel", "Cloudflare 터널",
            f"연결 {conns}개", "ok" if conns > 0 else "crit",
            "" if conns > 0 else "터널 다운 — 외부 접속 불가")
    except Exception:
        add("tunnel", "Cloudflare 터널", "조회 실패", "crit")

    # 7) 터널 라우팅 (netlab 호스트명이 실제로 걸려 있는가)
    try:
        with urllib.request.urlopen(f"{paths.CF_METRICS}/config", timeout=5) as r:
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
    rc, out, _ = run(["du", "-sh", paths.RECORD_ROOT], 15)
    if rc == 0 and out:
        add("rec", "녹화 보관", out.split()[0], "ok",
            "커지면 netlab-record purge")

    return items

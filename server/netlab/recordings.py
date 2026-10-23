"""세션 기록(.cast / .commands.jsonl) 조회와 VM → 호스트 회수."""
import json
import os
import re

from . import paths
from .procs import lxc
from .util import NAME_RE, log, parse_iso

# 리플레이용 이름 검증 — 경로 탈출 차단. 계정명·세션ID 만 허용한다.
SESSION_RE = re.compile(r"^[0-9A-Za-z._-]+$")

# 게스트가 만드는 기록의 이름 규칙. 이것만 가져온다.
REC_NAME_RE = re.compile(r"^[0-9A-Za-z._-]+\.(cast|commands\.jsonl)$")
REC_MAX_FILE = 64 * 1024 * 1024      # 파일 하나 최대 64MB
REC_MAX_TOTAL = 512 * 1024 * 1024    # 한 번 회수에 최대 512MB
REC_MAX_FILES = 500                  # 파일 개수 상한


def user_dir(user):
    return os.path.join(paths.RECORD_ROOT, user)


def list_sessions(user):
    """한 계정의 저장된 세션 목록. (session_id, cast크기, 명령수) 를 최근순으로."""
    if not NAME_RE.match(user):
        return []
    d = user_dir(user)
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


def safe_record_path(user, session, ext):
    """계정 폴더 안의 <session><ext> 절대경로. 벗어나면 None."""
    if not NAME_RE.match(user) or not SESSION_RE.match(session):
        return None
    base = os.path.realpath(user_dir(user))
    path = os.path.realpath(os.path.join(base, session + ext))
    if not path.startswith(base + os.sep) or not os.path.isfile(path):
        return None
    return path


def read_cast(user, session):
    """검증된 .cast 파일 내용을 돌려준다. 경로가 벗어나면 None."""
    path = safe_record_path(user, session, ".cast")
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
    cast = safe_record_path(user, session, ".cast")
    cmds = safe_record_path(user, session, ".commands.jsonl")
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


def _vm_listing(instance):
    """VM 안 기록 디렉터리의 최상위 파일을 (size, name) 로. 재귀 없음."""
    rc, out, _ = lxc("exec", instance, "--", "bash", "-c",
                     f"find {paths.VM_RECORD_DIR} -maxdepth 1 -type f -printf '%s %f\\n' 2>/dev/null",
                     timeout=60)
    if rc != 0 or not out.strip():
        return []
    rows = []
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
        rows.append((size, name))
    return rows


def collect_recordings(user, instance, skip_empty=True, quiet=False):
    """
    세션 기록을 호스트로 가져온다. 내려받은 파일 수를 돌려준다.

    예전엔 게스트가 만든 tar 를 호스트 root 가 통째로 메모리에 받아 풀었다. 게스트의
    lab 사용자는 NOPASSWD:ALL 이라 임의 스트림을 만들 수 있어, (1) 크기 제한 없는 tar 로
    호스트 메모리를 고갈시키거나 (2) root 가 신뢰할 수 없는 아카이브를 파싱하게 만들 수 있었다.

    그래서 tar 를 아예 쓰지 않는다. 이름 규칙에 맞는 파일만 골라, 크기를 먼저 확인하고,
    상한 안에서 `lxc file pull` 로 디스크에 바로 내려받는다. 아카이브 파서가 root 로 돌지
    않으므로 경로 탈출·심볼릭·장치 파일 공격이 원천 차단되고, 메모리로 슬러핑하지 않으므로
    DoS 도 막힌다.

    skip_empty: 명령 로그(.commands.jsonl)가 없는 세션의 .cast 는 받지 않는다(빈 세션).
    """
    dest = user_dir(user)
    count = 0
    try:
        os.makedirs(dest, exist_ok=True)
        os.chmod(dest, 0o700)
        rows = _vm_listing(instance)
        have_cmds = {n[:-len(".commands.jsonl")] for _, n in rows if n.endswith(".commands.jsonl")}
        total = 0
        for size, name in rows:
            if skip_empty and name.endswith(".cast") and name[:-len(".cast")] not in have_cmds:
                continue
            if size > REC_MAX_FILE:
                quiet or log(f"{user}: 기록 {name} 이 너무 큼({size}B) — 건너뜀")
                continue
            if count >= REC_MAX_FILES or total + size > REC_MAX_TOTAL:
                quiet or log(f"{user}: 기록 회수 상한 도달 — 이후 건너뜀")
                break
            local = os.path.join(dest, name)
            if os.path.exists(local) and os.path.getsize(local) >= size:
                continue                       # 이미 받았고 크기도 같으면 건너뛴다
            r2, _, err = lxc("file", "pull", f"{instance}{paths.VM_RECORD_DIR}/{name}",
                             local, timeout=120)
            if r2 != 0:
                quiet or log(f"{user}: {name} 회수 실패: {err[:120]}")
                continue
            try:
                os.chmod(local, 0o600)
            except OSError:
                pass
            total += size
            count += 1
    except Exception as e:
        quiet or log(f"{user} 세션 기록 회수 실패: {e}")
    return count

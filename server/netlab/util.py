"""시각·로그·원자적 파일 쓰기·계정명 규칙."""
import os
import re
from datetime import datetime, timezone

NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,30}$")


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


def write_atomic(path, content, mode=0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(content)
    os.chmod(tmp, mode)
    os.replace(tmp, path)

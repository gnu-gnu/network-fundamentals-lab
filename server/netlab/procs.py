"""외부 명령 실행. 항상 argv 리스트로 넘겨 셸 인용·주입 문제를 없앤다."""
import subprocess


def run(cmd, timeout=10):
    """(rc, stdout, stderr). 실행 자체가 실패하거나 시간이 넘으면 rc=1."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return 1, "", "timeout" if isinstance(e, subprocess.TimeoutExpired) else str(e)


def lxc(*args, timeout=600):
    """쉘을 거치지 않고 lxc 실행 — 인자 주입이 불가능하다. 시간 초과는 예외로 올린다."""
    p = subprocess.run(["lxc", *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()

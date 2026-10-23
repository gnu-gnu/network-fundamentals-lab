"""Subprocess execution with bounded output, deadlines and cooperative cancellation.

Output is read continuously and only the last LIMIT bytes are kept, so a chatty child never blocks
on a full pipe and the runner never holds unbounded output (contract "결과 판정과 자원 상한").
"""
import collections
import os
import signal
import subprocess
import threading
import time

LIMIT = 8 * 1024


class Tail:
    def __init__(self, limit=LIMIT):
        self.limit, self.chunks, self.size, self.truncated = limit, collections.deque(), 0, False
        self.lock = threading.Lock()

    def add(self, data):
        with self.lock:
            self.chunks.append(data)
            self.size += len(data)
            while self.size > self.limit and self.chunks:
                extra = self.size - self.limit
                head = self.chunks[0]
                self.truncated = True
                if len(head) <= extra:
                    self.chunks.popleft()
                    self.size -= len(head)
                else:
                    self.chunks[0] = head[extra:]
                    self.size -= extra

    def text(self):
        with self.lock:
            return b''.join(self.chunks).decode('utf-8', 'replace')


def _pump(stream, tail):
    try:
        for chunk in iter(lambda: stream.read(4096), b''):
            tail.add(chunk)
    except (OSError, ValueError):
        pass


class Result:
    def __init__(self, rc, out, err, timed_out=False, cancelled=False, truncated=False, stopped=True):
        self.rc, self.out, self.err = rc, out, err
        self.timed_out, self.cancelled, self.truncated = timed_out, cancelled, truncated
        self.stopped = stopped          # False: the child could not be confirmed ended after TERM/KILL


def run(argv, timeout, cancel=None, env=None, cwd=None, new_group=False):
    """Run argv; return Result. On timeout or cancel the child (group) is TERM'd then KILL'd.

    `cancel` is a threading.Event-like object with is_set(). new_group puts the child in its own
    process group so a hanging tool (containerlab) can be stopped with everything it spawned.
    """
    out_tail, err_tail = Tail(), Tail()
    try:
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, env=env, cwd=cwd,
                                 start_new_session=new_group)
    except OSError as exc:
        return Result(127, '', str(exc))
    readers = [threading.Thread(target=_pump, args=(child.stdout, out_tail), daemon=True),
               threading.Thread(target=_pump, args=(child.stderr, err_tail), daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    timed_out = cancelled = False
    while child.poll() is None:
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.05)
    if cancel is not None and cancel.is_set():
        cancelled = True          # a signal that also ended the child still counts as a cancel
    if child.poll() is None:
        terminate(child, new_group)
    for reader in readers:
        reader.join(timeout=2)
    for stream in (child.stdout, child.stderr):
        try:
            stream.close()
        except OSError:
            pass
    stopped = child.poll() is not None and (not new_group or not _group_alive(child.pid))
    return Result(child.returncode, out_tail.text(), err_tail.text(), timed_out, cancelled,
                  out_tail.truncated or err_tail.truncated, stopped)


def _group_alive(pgid):
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def terminate(child, group=False, grace=3):
    def send(sig):
        try:
            if group:
                os.killpg(child.pid, sig)
            else:
                child.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            pass
    send(signal.SIGTERM)
    try:
        child.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    if group and _group_alive(child.pid) or child.poll() is None:
        send(signal.SIGKILL)       # the whole group: scripts may leave children behind
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def process_alive(pid, start_ticks=None):
    """True if pid exists (and, when given, started at start_ticks — guards against pid reuse)."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if start_ticks is None:
        return True
    return start_time(pid) == start_ticks


def start_time(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            fields = f.read().rsplit(')', 1)[1].split()
        return int(fields[19])     # field 22 overall: starttime in clock ticks
    except (OSError, IndexError, ValueError):
        return None

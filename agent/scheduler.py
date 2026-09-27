"""Cross-process turn admission. Lock order: execution -> Memory lease -> metadata.

No process waits for a lease while holding execution. The owner may retain its
lease between turns; nested Agent calls share the outer turn.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
import uuid
import weakref

RUNTIME = Path(__file__).resolve().parent.parent / "data/runtime"


class OSLock:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = None

    def acquire(self):
        if self.stream is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self.stream = stream
        return True

    def release(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None

    def __del__(self):
        self.release()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def pending_users():
    result = []
    for path in RUNTIME.glob("user-*.json"):
        lock = OSLock(path.with_suffix(".lock"))
        if lock.acquire():
            path.unlink(missing_ok=True)
            lock.release()
        else:
            result.append(read_json(path, {}))
    return result


class Scheduler:
    def __init__(self, policy):
        self.policy = weakref.proxy(policy)
        self.local = threading.local()
        self.automatic = False

    def ticket(self):
        path = RUNTIME / ("user-" + uuid.uuid4().hex + ".json")
        lock = OSLock(path.with_suffix(".lock"))
        lock.acquire()
        atomic_json(path, dict(session=self.policy.session, pid=os.getpid()))
        return path, lock

    @staticmethod
    def unticket(ticket):
        if ticket:
            ticket[0].unlink(missing_ok=True)
            ticket[1].release()

    @contextmanager
    def turn(self, emit=print):
        if getattr(self.local, "depth", 0):
            yield
            return
        ticket = None if self.automatic else self.ticket()
        execution = OSLock(RUNTIME / "execution.lock")
        announced = None
        try:
            while True:
                reason = "正在执行其他 Agent 轮次"
                if execution.acquire():
                    probe = OSLock(self.policy.files.root / ".memory-owner.lock")
                    admitted = self.policy.lease.stream is not None or probe.acquire()
                    if admitted:
                        probe.release()
                        if not self.automatic or not pending_users():
                            break
                        reason = "已提交的用户输入优先"
                    else:
                        reason = "Memory 事务被占用：" + str({k: v for k,v in (self.policy._state() or {}).items() if k in ("session", "pid")})
                    execution.release()
                if reason != announced:
                    emit("[等待] " + reason + " | " + str(read_json(RUNTIME / "execution.json", {})))
                    announced = reason
                time.sleep(0.1)
            self.unticket(ticket)
            ticket = None
            self.policy.ensure_no_transaction()
            atomic_json(RUNTIME / "execution.json", dict(session=self.policy.session, pid=os.getpid()))
            self.local.depth = 1
            yield
        finally:
            self.local.depth = 0
            if execution.stream:
                (RUNTIME / "execution.json").unlink(missing_ok=True)
            execution.release()
            self.unticket(ticket)

    def status(self):
        probe = OSLock(RUNTIME / "execution.lock")
        idle = probe.acquire()
        probe.release()
        return dict(execution=None if idle else read_json(RUNTIME / "execution.json"),
                    memory={k:v for k,v in (self.policy._state() or {}).items() if k in ("active", "session", "pid")}, waiting_users=pending_users())

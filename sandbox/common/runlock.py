"""One writer at a time on a sandbox data dir.

A demo run and a re-seed (or two demo runs) on the same var/ corrupt each
other's world: one process can post an ERP receipt for a bank credit the
other just wiped. Every command that changes the world takes this lock.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path


class Busy(RuntimeError):
    pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def run_lock(var_dir: Path | str, what: str):
    path = Path(var_dir) / ".run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            pid, holder = path.read_text().split(" ", 1)
        except ValueError:
            pid, holder = "0", "unknown"
        if _alive(int(pid)):
            raise Busy(f"{path} is held by pid {pid} ({holder.strip()}); wait for it or stop it first")
        path.unlink(missing_ok=True)          # stale lock from a crashed process
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, f"{os.getpid()} {what}".encode())
    os.close(fd)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)

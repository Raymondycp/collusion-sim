"""One server per data folder (de/one-server; SE-case-presets-report addendum 2). Two servers on one data folder share the
DBOS queue, so a server that cannot build a run (paid calls off) takes it and holds it while the other could run it.

The lock is an OS byte-range lock on `data/server.lock`, held for the server's lifetime. The OS drops it when the process
ends, so a crashed server leaves no stale lock. The first bytes of the file name the holder (pid, start time) for the
refusal message; the locked byte lies past them, so a reader is never blocked."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

OFFSET = 1 << 20
HOLDER_BYTES = 120


class DataFolderInUse(RuntimeError):
    pass


def _lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1, OFFSET, os.SEEK_SET)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.lockf(fd, fcntl.LOCK_UN, 1, OFFSET, os.SEEK_SET)


class ServerLock:
    def __init__(self, data: Path) -> None:
        self.path = Path(data) / "server.lock"
        self._fd: int | None = None

    def holder(self) -> str:
        try:
            text = self.path.read_bytes()[:HOLDER_BYTES].decode("utf-8", "replace").strip()
        except OSError:
            text = ""
        return text or "holder unknown"

    def acquire(self) -> None:
        """Take the lock, or raise DataFolderInUse naming the folder and the holder."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT)
        try:
            _lock(fd)
        except OSError:
            os.close(fd)
            raise DataFolderInUse(
                f"Another collusion-sim server uses the data folder {self.path.parent} ({self.holder()}). Stop it, or start"
                " this server with another --root (SIM_ROOT). Two servers on one data folder take each other's runs."
            ) from None
        started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, f"pid {os.getpid()}, started {started}".ljust(HOLDER_BYTES).encode("utf-8"))
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def check(self) -> None:
        """Raise DataFolderInUse when another server holds the lock; take nothing (for `serve` before the app starts)."""
        self.acquire()
        self.release()

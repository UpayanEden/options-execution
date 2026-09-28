"""
File-based process lock.
Prevents two strategy instances from placing orders simultaneously.
Uses a lock file in logs/ containing the current PID.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_LOCK_FILE = Path("logs/strategy.lock")


class ProcessLockError(Exception):
    pass


class ProcessLock:
    def __init__(self, lock_file: Path = _DEFAULT_LOCK_FILE) -> None:
        self._lock = lock_file

    def acquire(self) -> None:
        """
        Acquire lock. Raises ProcessLockError if another instance is running.
        Cleans up stale locks from dead processes.
        """
        self._lock.parent.mkdir(parents=True, exist_ok=True)

        if self._lock.exists():
            existing_pid = self._read_pid()
            if existing_pid and self._pid_running(existing_pid):
                raise ProcessLockError(
                    f"Another strategy instance is running (PID {existing_pid}). "
                    f"Lock file: {self._lock}"
                )
            logger.warning("stale_lock_removed", extra={"pid": existing_pid})
            self._lock.unlink()

        self._lock.write_text(str(os.getpid()))
        logger.info("process_lock_acquired", extra={"pid": os.getpid()})

    def release(self) -> None:
        if self._lock.exists():
            self._lock.unlink()
        logger.info("process_lock_released")

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_) -> None:
        self.release()

    def _read_pid(self) -> int | None:
        try:
            return int(self._lock.read_text().strip())
        except (ValueError, OSError):
            return None

    @staticmethod
    def _pid_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

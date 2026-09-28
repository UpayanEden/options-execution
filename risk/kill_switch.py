"""
Emergency kill switch.
Uses a file flag so it works across threads and processes.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_FLAG_FILE = Path("logs/KILL_SWITCH_ACTIVE")


class KillSwitch:
    def __init__(self, flag_file: Path = _DEFAULT_FLAG_FILE) -> None:
        self._flag = flag_file

    def is_active(self) -> bool:
        return self._flag.exists()

    def activate(self, reason: str = "") -> None:
        self._flag.parent.mkdir(parents=True, exist_ok=True)
        self._flag.write_text(f"KILL SWITCH ACTIVATED\n{reason}\n")
        logger.critical("kill_switch_activated", extra={"reason": reason})

    def deactivate(self) -> None:
        if self._flag.exists():
            self._flag.unlink()
        logger.info("kill_switch_deactivated")

    def check(self) -> None:
        """Raise if kill switch is active."""
        if self.is_active():
            raise KillSwitchError(f"Kill switch active: {self._flag}")


class KillSwitchError(Exception):
    pass

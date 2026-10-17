"""Business time for the runtime.

The runtime never calls datetime.now() directly: timers, commitments and
entity ageing all use a Clock, so a multi-day mission can run on compressed
simulated time and tests can control time exactly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FileClock:
    """Reads a JSON file {"now": "<iso datetime>"} written by whoever owns simulated time."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def now(self) -> datetime:
        return datetime.fromisoformat(json.loads(self.path.read_text())["now"])


class ManualClock:
    def __init__(self, start: datetime):
        if start.tzinfo is None:
            raise ValueError("ManualClock needs a timezone-aware start")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

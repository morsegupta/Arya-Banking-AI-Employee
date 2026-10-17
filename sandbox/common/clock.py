"""Sandbox clock shared by every sandbox service.

The sandbox runs on simulated time so a multi-day mission can be compressed
into minutes. All services read the same `var/clock.json`, so the ERP's
"days overdue", the bank's statement dates and the hub's message timestamps
agree. The seed writes the start time; later the runtime scheduler advances it.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from sandbox.common import config

IST = timezone(timedelta(hours=5, minutes=30))


def _clock_file():
    return config.var_dir() / "clock.json"


def set_now(when: datetime) -> None:
    if when.tzinfo is None:
        when = when.replace(tzinfo=IST)
    _clock_file().write_text(json.dumps({"now": when.isoformat()}))


def now() -> datetime:
    path = _clock_file()
    if not path.exists():
        raise RuntimeError(f"Sandbox clock not initialised: {path} missing. Run `python -m sandbox.seed`.")
    return datetime.fromisoformat(json.loads(path.read_text())["now"])


def today() -> date:
    return now().astimezone(IST).date()


def advance(delta: timedelta) -> datetime:
    when = now() + delta
    set_now(when)
    return when

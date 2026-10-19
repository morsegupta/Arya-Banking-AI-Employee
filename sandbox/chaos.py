"""Chaos switches for the sandbox company: make the world misbehave the way real systems do.

    python -m sandbox.chaos show
    python -m sandbox.chaos set bank.csv_download=error erp.layout=v2 hub.email_outage=true
    python -m sandbox.chaos set bank.session_ttl=20 bank.csv_schema=v2 bank.transient_errors=2
    python -m sandbox.chaos clear

| switch                 | effect                                                                      |
|------------------------|-----------------------------------------------------------------------------|
| bank.session_ttl       | seconds a bank login lasts (default 300): sessions expire mid-task          |
| bank.csv_schema        | v1 | v2: the statement CSV's column names change                            |
| bank.csv_download      | ok | error: the CSV download returns HTTP 503                               |
| bank.transient_errors  | N: the next N statement page/download requests fail with 503               |
| erp.layout             | v1 | v2: the receipt form's labels, order and button change                 |
| hub.email_outage       | true: every email send fails with 503 (WhatsApp still works)                |
| tracker.open_in_app    | true: someone has the AR tracker open in the desktop app (its lock file)    |

Switches live in var/chaos.json (bank session/CSV settings in the bank's own settings table).
Re-seeding clears them. The agent runtime never reads this file: it has to cope.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from sandbox.common import config, db

BANK_SETTINGS = {"bank.session_ttl": "session_ttl_seconds", "bank.csv_schema": "csv_schema"}
SWITCHES = {"bank.session_ttl", "bank.csv_schema", "bank.csv_download", "bank.transient_errors", "erp.layout",
            "hub.email_outage", "tracker.open_in_app"}
TRACKER_LOCK = ("finance", ".~lock.ar_tracker.xlsx#")


def _path():
    return config.var_dir() / "chaos.json"


def load() -> dict[str, Any]:
    p = _path()
    return json.loads(p.read_text()) if p.exists() else {}


def get(name: str, default: Any = None) -> Any:
    return load().get(name, default)


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        low = value.lower()
        if low in {"true", "false"}:
            return low == "true"
        if low.isdigit():
            return int(low)
    return value


def set_switches(**values: Any) -> dict[str, Any]:
    unknown = set(values) - SWITCHES
    if unknown:
        raise ValueError(f"unknown chaos switches {sorted(unknown)}; known: {sorted(SWITCHES)}")
    state = load()
    for name, value in values.items():
        value = _coerce(value)
        if name in BANK_SETTINGS:
            with db.session("bank") as conn:
                conn.execute("UPDATE settings SET value = ? WHERE key = ?", (str(value), BANK_SETTINGS[name]))
        if name == "tracker.open_in_app":
            lock = config.drive_dir() / TRACKER_LOCK[0] / TRACKER_LOCK[1]
            if value:
                lock.parent.mkdir(parents=True, exist_ok=True)
                lock.write_text("Kavya,KAVYA-PC,kavya,19.10.2026 09:12,file:///home/kavya/.config/libreoffice;")
            else:
                lock.unlink(missing_ok=True)
        state[name] = value
    _path().write_text(json.dumps(state, indent=2))
    return state


def take_transient(name: str) -> bool:
    """Consume one forced failure from a counter switch. True if this request should fail."""
    state = load()
    left = int(state.get(name, 0) or 0)
    if left <= 0:
        return False
    state[name] = left - 1
    _path().write_text(json.dumps(state, indent=2))
    return True


def clear() -> None:
    _path().unlink(missing_ok=True)
    (config.drive_dir() / TRACKER_LOCK[0] / TRACKER_LOCK[1]).unlink(missing_ok=True)
    try:
        with db.session("bank") as conn:
            conn.execute("UPDATE settings SET value = ? WHERE key = 'session_ttl_seconds'",
                         (config.get("BANK_SESSION_TTL_SECONDS"),))
            conn.execute("UPDATE settings SET value = 'v1' WHERE key = 'csv_schema'")
    except Exception:  # noqa: BLE001 - bank not seeded yet
        pass


def main(argv: list[str]) -> None:
    if not argv or argv[0] == "show":
        print(json.dumps(load(), indent=2) or "{}")
    elif argv[0] == "set":
        print(json.dumps(set_switches(**dict(a.split("=", 1) for a in argv[1:])), indent=2))
    elif argv[0] == "clear":
        clear()
        print("chaos cleared")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])

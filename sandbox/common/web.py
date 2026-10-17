"""Shared web plumbing for the sandbox apps: cookie sessions and Jinja setup.

Session expiry uses real wall-clock time, not the sandbox clock: it models how
long the agent's real browser stays logged in (bank portals expire fast).
"""

from __future__ import annotations

import hmac
import secrets
import time
from pathlib import Path

from fastapi.templating import Jinja2Templates

from sandbox.common import db
from sandbox.common.money import format_inr, paise_to_str

SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS web_sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at REAL NOT NULL
);
"""


def templates(directory: Path) -> Jinja2Templates:
    tpl = Jinja2Templates(directory=str(directory))
    tpl.env.filters["inr"] = format_inr
    tpl.env.filters["plain"] = paise_to_str
    return tpl


def check_password(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


class SessionStore:
    def __init__(self, db_name: str, cookie: str):
        self.db_name = db_name
        self.cookie = cookie

    def create(self, user_id: str, ttl_seconds: int) -> str:
        token = secrets.token_urlsafe(24)
        with db.session(self.db_name) as conn:
            conn.execute(
                "INSERT INTO web_sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user_id, time.time() + ttl_seconds),
            )
        return token

    def lookup(self, token: str | None) -> tuple[str | None, str | None]:
        """Return (user_id, None) if valid, else (None, reason) with reason in {'missing', 'expired'}."""
        if not token:
            return None, "missing"
        with db.session(self.db_name) as conn:
            row = conn.execute("SELECT user_id, expires_at FROM web_sessions WHERE token = ?", (token,)).fetchone()
            if row is None:
                return None, "missing"
            if row["expires_at"] < time.time():
                conn.execute("DELETE FROM web_sessions WHERE token = ?", (token,))
                return None, "expired"
        return row["user_id"], None

    def destroy(self, token: str | None) -> None:
        if token:
            with db.session(self.db_name) as conn:
                conn.execute("DELETE FROM web_sessions WHERE token = ?", (token,))

"""Tiny SQLite helper. One file per service under var/, WAL mode."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sandbox.common import config


def db_path(name: str) -> Path:
    return config.var_dir() / f"{name}.db"


def connect(name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(name), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(name: str) -> Iterator[sqlite3.Connection]:
    """Connection that commits on success, rolls back on error, always closes."""
    conn = connect(name)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reset(name: str, schema: str) -> None:
    path = db_path(name)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    with session(name) as conn:
        conn.executescript(schema)

"""Bank portal storage: one current account, its transactions, portal settings."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import date

from sandbox.common import config, db
from sandbox.common.money import rupees_to_paise
from sandbox.common.web import SESSIONS_SCHEMA
from sandbox.common.world import World

DB_NAME = "bank"

SCHEMA = SESSIONS_SCHEMA + """
CREATE TABLE account (
    account_number TEXT PRIMARY KEY,
    account_name TEXT NOT NULL,
    bank_name TEXT NOT NULL,
    ifsc TEXT NOT NULL,
    branch TEXT NOT NULL,
    opening_date TEXT NOT NULL,
    opening_balance_paise INTEGER NOT NULL
);

CREATE TABLE transactions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id TEXT NOT NULL UNIQUE,
    txn_date TEXT NOT NULL,
    value_date TEXT NOT NULL,
    narration TEXT NOT NULL,
    ref_no TEXT NOT NULL,
    withdrawal_paise INTEGER NOT NULL DEFAULT 0,
    deposit_paise INTEGER NOT NULL DEFAULT 0,
    balance_paise INTEGER NOT NULL,
    CHECK ((withdrawal_paise > 0) <> (deposit_paise > 0))
);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

OPENING_DATE = "2026-10-01"
UTR_PREFIX = {"NEFT": "N", "RTGS": "BCBKR5", "IMPS": "", "UPI": ""}


def seed(world: World) -> None:
    db.reset(DB_NAME, SCHEMA)
    bank = world.company.bank
    balance = rupees_to_paise(bank.opening_balance)
    with db.session(DB_NAME) as conn:
        conn.execute("INSERT INTO account VALUES (?,?,?,?,?,?,?)",
                     (bank.account_number, bank.account_name, bank.bank_name, bank.ifsc, bank.branch,
                      OPENING_DATE, balance))
        conn.executemany("INSERT INTO settings VALUES (?, ?)", [
            ("session_ttl_seconds", config.get("BANK_SESSION_TTL_SECONDS")),
            ("csv_schema", "v1"),
        ])
        for txn in sorted(world.bank_transactions, key=lambda t: t.date):
            deposit, withdrawal = rupees_to_paise(txn.credit), rupees_to_paise(txn.debit)
            balance += deposit - withdrawal
            _insert(conn, txn.date, txn.narration, txn.ref, deposit, withdrawal, balance)


def _insert(conn: sqlite3.Connection, when: date, narration: str, ref: str,
            deposit: int, withdrawal: int, balance: int) -> str:
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM transactions").fetchone()[0]
    txn_id = f"T{when.strftime('%Y%m%d')}{seq:05d}"
    conn.execute(
        "INSERT INTO transactions (txn_id, txn_date, value_date, narration, ref_no, withdrawal_paise, "
        "deposit_paise, balance_paise) VALUES (?,?,?,?,?,?,?,?)",
        (txn_id, when.isoformat(), when.isoformat(), narration, ref, withdrawal, deposit, balance),
    )
    return txn_id


def current_balance(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT balance_paise FROM transactions ORDER BY seq DESC LIMIT 1").fetchone()
    if row:
        return row[0]
    return conn.execute("SELECT opening_balance_paise FROM account").fetchone()[0]


def new_utr(mode: str) -> str:
    digits = 9 if mode == "NEFT" else 6 if mode == "RTGS" else 12
    return UTR_PREFIX[mode] + "".join(secrets.choice("0123456789") for _ in range(digits))


def credit(conn: sqlite3.Connection, when: date, payer: str, amount_paise: int, mode: str,
           remark: str, utr: str | None = None) -> dict:
    """Write an incoming payment. Used only by the sandbox payment engine."""
    if amount_paise <= 0:
        raise ValueError("amount must be positive")
    if mode not in UTR_PREFIX:
        raise ValueError(f"unknown mode {mode}")
    utr = utr or new_utr(mode)
    narration = f"{mode}/{utr}/{payer.upper()}/{remark}".rstrip("/")
    balance = current_balance(conn) + amount_paise
    txn_id = _insert(conn, when, narration, utr, amount_paise, 0, balance)
    return {"txn_id": txn_id, "utr": utr, "narration": narration, "balance_paise": balance}


def get_setting(conn: sqlite3.Connection, key: str) -> str:
    return conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()[0]

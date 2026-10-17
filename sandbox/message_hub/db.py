"""Message hub storage: mailboxes, API tokens, messages, webhooks, call requests."""

from __future__ import annotations

import re

from sandbox.common import config, db
from sandbox.common.world import World

DB_NAME = "hub"

SCHEMA = """
CREATE TABLE mailboxes (
    address TEXT PRIMARY KEY,           -- normalised: lowercase email or +<digits>
    channel TEXT NOT NULL CHECK (channel IN ('email', 'whatsapp')),
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('company', 'customer', 'system')),
    owner_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    bounces INTEGER NOT NULL DEFAULT 0
);

-- Which sender addresses a token may send from / read. '*' = any (simulator only).
CREATE TABLE tokens (
    token TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    allowed TEXT NOT NULL
);

CREATE TABLE messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    from_addr TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    attachments_json TEXT NOT NULL DEFAULT '[]',
    in_reply_to TEXT,
    status TEXT NOT NULL CHECK (status IN ('delivered', 'bounced', 'failed')),
    status_detail TEXT,
    idempotency_key TEXT UNIQUE,
    sent_by TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE INDEX messages_to ON messages(to_addr, seq);
CREATE INDEX messages_from ON messages(from_addr, seq);

CREATE TABLE webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mailbox TEXT NOT NULL REFERENCES mailboxes(address),
    url TEXT NOT NULL,
    UNIQUE (mailbox, url)
);

CREATE TABLE webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id INTEGER NOT NULL REFERENCES webhooks(id),
    message_id TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    last_error TEXT
);

CREATE TABLE calls (
    id TEXT PRIMARY KEY,
    from_addr TEXT NOT NULL,
    to_addr TEXT NOT NULL,
    purpose TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('requested', 'completed', 'no_answer')),
    transcript TEXT,
    requested_at TEXT NOT NULL,
    completed_at TEXT
);
"""

MAILER_DAEMON = "mailer-daemon@hub.example"


def normalise(channel: str, address: str) -> str:
    address = address.strip()
    if channel == "whatsapp":
        digits = re.sub(r"\D", "", address)
        return f"+{digits}"
    return address.lower()


def seed(world: World) -> None:
    db.reset(DB_NAME, SCHEMA)
    co = world.company
    company_boxes = [("email", co.ar_mailbox), ("whatsapp", co.ar_whatsapp)]
    with db.session(DB_NAME) as conn:
        for channel, addr in company_boxes:
            conn.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,0)",
                         (normalise(channel, addr), channel, "company", "sharma", f"{co.name} Accounts"))
        conn.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,0)",
                     (MAILER_DAEMON, "email", "system", "hub", "Mail Delivery Subsystem"))
        for c in world.customers:
            conn.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,?)",
                         (normalise("email", c.contact.email), "email", "customer", c.id,
                          f"{c.contact.name} ({c.name})", int(c.contact.email_bounces)))
            conn.execute("INSERT INTO mailboxes VALUES (?,?,?,?,?,0)",
                         (normalise("whatsapp", c.contact.whatsapp), "whatsapp", "customer", c.id,
                          f"{c.contact.name} ({c.name})"))
        conn.execute("INSERT INTO tokens VALUES (?,?,?)",
                     (config.get("HUB_ARYA_TOKEN"), "arya",
                      ",".join(normalise(ch, a) for ch, a in company_boxes)))
        conn.execute("INSERT INTO tokens VALUES (?,?,?)", (config.get("HUB_SIM_TOKEN"), "customer_sim", "*"))

"""Matcher: link incoming money records to the entities they settle. Plain code, no LLM.

A record matches an entity only if the record's counterparty resolves to the
entity's counterparty. Then, in order of strength:
  1. reference match: the entity's reference number appears in the record;
     one record may cover several referenced entities if the amounts add up;
     a smaller amount is a partial settlement of one entity.
  2. amount-only match: no reference, but exactly one open entity of that
     counterparty has the same amount (within tolerance).
Anything else is left unmatched for a human: a same-amount record from a
similarly named but different counterparty must never match.
Records already applied (in `applied`) are skipped, so money is never counted twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

STOPWORDS = {"PVT", "PRIVATE", "LTD", "LIMITED", "LLP", "CO", "COMPANY", "THE", "AND", "M", "S", "MS", "INC", "CORP"}


@dataclass
class Record:
    id: str                 # unique id of the record (its reference number at the source)
    on: date
    text: str               # free text describing the record (narration)
    amount: int             # minor units, > 0 for incoming


@dataclass
class Candidate:
    entity_id: str
    ref: str                # entity reference, e.g. "ABC-2291"
    counterparty: str
    amount_due: int         # minor units


@dataclass
class Match:
    entity_id: str
    record_id: str
    record_on: date
    matched_amount: int
    counterparty_matches: bool
    reference_matches: bool
    kind: str               # "reference" | "reference_multi" | "reference_partial" | "amount_only"
    record_text: str
    reasons: list[str] = field(default_factory=list)

    def guard_context(self) -> dict:
        return {"matcher": {"matched_amount": self.matched_amount, "counterparty_matches": self.counterparty_matches,
                            "record_id": self.record_id, "reference_matches": self.reference_matches,
                            "kind": self.kind}}


def tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^A-Z0-9]+", text.upper()) if t]


def name_tokens(name: str) -> list[str]:
    return [t for t in tokens(name) if t not in STOPWORDS]


def counterparty_matches(counterparty: str, record_text: str) -> bool:
    """The counterparty's distinctive name words must appear in the record: its first word and
    at least two thirds of all of them. 'Sunrise Hardware' does not match 'SUNRISE TRADING CO'."""
    want = name_tokens(counterparty)
    have = set(tokens(record_text))
    if not want or want[0] not in have:
        return False
    return sum(t in have for t in want) / len(want) >= 2 / 3


def reference_in(ref: str, record_text: str) -> bool:
    digits = re.findall(r"\d+", ref)
    if not digits:
        return False
    number = max(digits, key=len)
    return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", record_text) is not None


def match(records: list[Record], candidates: list[Candidate], *, tolerance: int,
          applied: set[str] | None = None) -> list[Match]:
    applied = applied or set()
    out: list[Match] = []
    taken: set[str] = set()                       # entities settled within this run
    for rec in sorted(records, key=lambda r: (r.on, r.id)):
        if rec.amount <= 0 or rec.id in applied:
            continue
        mine = [c for c in candidates if c.entity_id not in taken and counterparty_matches(c.counterparty, rec.text)]
        if not mine:
            continue
        referenced = [c for c in mine if reference_in(c.ref, rec.text)]
        if referenced:
            total = sum(c.amount_due for c in referenced)
            if len(referenced) > 1 and abs(total - rec.amount) <= tolerance:
                for c in referenced:
                    out.append(Match(c.entity_id, rec.id, rec.on, c.amount_due, True, True, "reference_multi",
                                     rec.text, [f"one record covers {len(referenced)} referenced entities"]))
                    taken.add(c.entity_id)
                continue
            if len(referenced) == 1:
                c = referenced[0]
                if rec.amount <= c.amount_due + tolerance:
                    full = rec.amount >= c.amount_due - tolerance
                    out.append(Match(c.entity_id, rec.id, rec.on, min(rec.amount, c.amount_due), True, True,
                                     "reference" if full else "reference_partial", rec.text,
                                     [] if full else ["amount smaller than due: partial"]))
                    taken.add(c.entity_id)
                continue
            continue   # references present but amounts inconsistent: leave for a human
        same_amount = [c for c in mine if abs(c.amount_due - rec.amount) <= tolerance]
        if len(same_amount) == 1:
            c = same_amount[0]
            out.append(Match(c.entity_id, rec.id, rec.on, c.amount_due, True, False, "amount_only", rec.text,
                             ["no reference in record; unique open amount for this counterparty"]))
            taken.add(c.entity_id)
    return out

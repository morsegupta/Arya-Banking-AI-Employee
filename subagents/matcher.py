"""Matcher: link incoming money records to the entities they settle. Plain code, no LLM.

A record matches an entity only if the record's counterparty resolves to the
entity's counterparty. Then, in order of strength:
  1. reference match: the entity's reference number appears in the record;
     one record may cover several referenced entities if the amounts add up;
     a smaller amount is a partial settlement of one entity, or, across several
     referenced entities, is allocated oldest first (the last one partially).
  2. amount-only match: no reference, but exactly one open entity of that
     counterparty has the same amount (within tolerance).
  3. amount-sum match: no reference, but exactly one set of two or more open
     entities of that counterparty adds up to the amount.
Anything else is left unmatched for a human: amounts above what is due, ambiguous sums, and
a same-amount record from a similarly named but different counterparty.
Records already applied (in `applied`) are skipped, so money is never counted twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from itertools import combinations

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
    due_on: date | None = None   # allocation order: oldest first


@dataclass
class Match:
    entity_id: str
    record_id: str
    record_on: date
    matched_amount: int
    counterparty_matches: bool
    reference_matches: bool
    kind: str               # reference | reference_multi | reference_partial | reference_multi_partial
                            # | amount_only | amount_sum
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


def allocate(amount: int, dues: list[int], tolerance: int = 0) -> list[int]:
    """Split `amount` over `dues` in order: each gets min(due, what is left). Within tolerance of the
    total, everyone is settled in full (rounding differences are absorbed)."""
    if abs(sum(dues) - amount) <= tolerance:
        return list(dues)
    out, left = [], amount
    for d in dues:
        part = max(0, min(d, left))
        out.append(part)
        left -= part
    return out


MAX_SUBSET_ITEMS = 12


def unique_subset(amount: int, cands: list[Candidate], tolerance: int) -> list[Candidate] | None:
    """The only set of 2+ candidates whose dues add up to `amount`, else None (none, or ambiguous)."""
    if len(cands) < 2 or len(cands) > MAX_SUBSET_ITEMS:
        return None
    found = None
    for k in range(2, len(cands) + 1):
        for combo in combinations(cands, k):
            if abs(sum(c.amount_due for c in combo) - amount) <= tolerance:
                if found is not None:
                    return None
                found = list(combo)
    return found


def _oldest_first(cands: list[Candidate]) -> list[Candidate]:
    return sorted(cands, key=lambda c: (c.due_on or date.max, c.ref))


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
            if len(referenced) > 1 and rec.amount <= total + tolerance:
                full = abs(total - rec.amount) <= tolerance
                ordered = _oldest_first(referenced)
                for c, part in zip(ordered, allocate(rec.amount, [c.amount_due for c in ordered], tolerance)):
                    if part <= 0:
                        continue
                    why = [f"one record covers {len(referenced)} referenced entities"]
                    if not full:
                        why.append(f"short of their total ({rec.amount} < {total}): allocated oldest first")
                    out.append(Match(c.entity_id, rec.id, rec.on, part, True, True,
                                     "reference_multi" if full else "reference_multi_partial", rec.text, why))
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
            continue
        if same_amount:
            continue        # several open entities with this amount: ambiguous, leave for a human
        subset = unique_subset(rec.amount, mine, tolerance)
        if subset:
            for c in _oldest_first(subset):
                out.append(Match(c.entity_id, rec.id, rec.on, c.amount_due, True, False, "amount_sum", rec.text,
                                 [f"no reference in record; the only set of {len(subset)} open entities "
                                  "of this counterparty that adds up to the amount"]))
                taken.add(c.entity_id)
    return out

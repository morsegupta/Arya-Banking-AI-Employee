from __future__ import annotations

from datetime import date

import pytest

from subagents.matcher import Candidate, Record, counterparty_matches, match
from subagents.reply_classifier import Proposal, ReplyClassifier, parse_amount

INTENTS = {"promise_to_pay": "", "paid_claim": "", "dispute": "", "unclear": ""}
MSG = "Hi Arya, noted on INV-2297.  We will transfer ₹54,000.00 on 20 Oct 2026 (Tuesday). Regards, Ganesh"


def rc():
    return ReplyClassifier(llm=None, intents=INTENTS)


def test_good_promise_passes_checks():
    c = rc().check(Proposal(intent="promise_to_pay", confidence=0.95, about_entity=True,
                            quote="We will transfer ₹54,000.00 on 20 Oct 2026 (Tuesday).",
                            amount_text="₹54,000.00", date="2026-10-20"), MSG)
    assert c.problems == [] and c.amount_minor == 54000_00 and c.date == date(2026, 10, 20)
    assert c.facts() == {"promise_amount": 54000_00, "promise_date": "2026-10-20",
                         "promise_quote": "We will transfer ₹54,000.00 on 20 Oct 2026 (Tuesday)."}
    assert c.guard_context()["evidence"]["quote"]


def test_invented_quote_removes_evidence():
    c = rc().check(Proposal(intent="promise_to_pay", confidence=0.99, about_entity=True,
                            quote="We will pay everything tomorrow.", amount_text="54000", date="2026-10-20"), MSG)
    assert c.quote is None and "quote not found" in c.problems[0]
    assert c.guard_context()["evidence"] == {}          # PROMISED guard needs evidence.quote -> fails closed


def test_unknown_intent_becomes_unclear():
    c = rc().check(Proposal(intent="mark_paid", confidence=1, about_entity=True, quote="Regards, Ganesh"), MSG)
    assert c.intent == "unclear" and c.confidence == 0


@pytest.mark.parametrize("text, minor", [("₹54,000.00", 54000_00), ("Rs. 3.4 lakh", 340000_00), ("54k", 54000_00),
                                         ("1,20,000", 120000_00), ("two lakh", None), (None, None), ("0", None)])
def test_parse_amount(text, minor):
    assert parse_amount(text, 100) == minor


def test_bad_date_is_a_problem_not_a_crash():
    c = rc().check(Proposal(intent="promise_to_pay", confidence=0.9, about_entity=True,
                            quote="We will transfer", date="next Tuesday"), MSG)
    assert c.date is None and any("date" in p for p in c.problems)


# --- matcher ---------------------------------------------------------------------------------

D = date(2026, 10, 20)


def rec(rid, text, amount):
    return Record(rid, D, text, amount)


CANDS = [
    Candidate("e2291", "INV-2291", "Globex Retail Pvt Ltd", 340000_00),
    Candidate("e2279", "INV-2279", "Sunrise Hardware", 112000_00),
    Candidate("e2270", "INV-2270", "Initech Supplies LLP", 120000_00),
    Candidate("e2275", "INV-2275", "Initech Supplies LLP", 95000_00),
    Candidate("e2288", "INV-2288", "Initech Supplies LLP", 60000_00),
    Candidate("e2284", "INV-2284", "Patel Provision Stores", 150000_00),
    Candidate("e2263", "INV-2263", "Mehta Electricals", 88000_00),
]


def test_sunrise_lure_does_not_match():
    assert not counterparty_matches("Sunrise Hardware", "IMPS/629011223344/SUNRISE TRADING CO/BILL 1187")
    assert match([rec("629011223344", "IMPS/629011223344/SUNRISE TRADING CO/BILL 1187", 112000_00)],
                 CANDS, tolerance=100) == []


def test_reference_match_and_lock():
    r = rec("N1", "NEFT/N1/GLOBEX RETAIL PVT LTD/INV 2291", 340000_00)
    m = match([r], CANDS, tolerance=100)
    assert [(x.entity_id, x.kind, x.matched_amount) for x in m] == [("e2291", "reference", 340000_00)]
    assert match([r], CANDS, tolerance=100, applied={"N1"}) == []


def test_one_transfer_for_three_entities():
    r = rec("N2", "NEFT/N2/INITECH SUPPLIES LLP/INV 2270 INV 2275 INV 2288", 275000_00)
    m = match([r], CANDS, tolerance=100)
    assert sorted(x.entity_id for x in m) == ["e2270", "e2275", "e2288"]
    assert {x.kind for x in m} == {"reference_multi"} and sum(x.matched_amount for x in m) == 275000_00


def test_partial():
    m = match([rec("N3", "NEFT/N3/PATEL PROVISION STORES/INV 2284", 75000_00)], CANDS, tolerance=100)
    assert m[0].kind == "reference_partial" and m[0].matched_amount == 75000_00


def test_amount_only_needs_unique_amount():
    m = match([rec("N4", "NEFT/N4/MEHTA ELECTRICALS/PAYMENT", 88000_00)], CANDS, tolerance=100)
    assert m[0].entity_id == "e2263" and m[0].kind == "amount_only" and not m[0].reference_matches
    assert match([rec("N5", "NEFT/N5/INITECH SUPPLIES/X", 77000_00)], CANDS, tolerance=100) == []


def test_overpayment_with_reference_left_for_human():
    assert match([rec("N6", "NEFT/N6/GLOBEX RETAIL/INV 2291", 500000_00)], CANDS, tolerance=100) == []


def test_reference_digits_must_be_whole():
    m = match([rec("N7", "NEFT/N7/GLOBEX RETAIL/ORDER 22910", 340000_00)], CANDS, tolerance=100)
    assert m[0].kind == "amount_only"   # 22910 is not 2291; still the unique same-amount open item


def test_debits_ignored():
    assert match([rec("D1", "NEFT DR/GLOBEX RETAIL/INV 2291", -340000_00)], CANDS, tolerance=100) == []

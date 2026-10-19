"""Regressions for the code-review gaps (2026-10-19): each test pins one fixed behaviour."""

from __future__ import annotations

import pytest

from apps.settlement import _amount, parse_rows

MAPPING = {"date": {"columns": ["Date"], "format": "%Y-%m-%d"}, "text": {"columns": ["Narration"]},
           "ref": {"columns": ["Ref"]}, "credit": {"columns": ["Credit"]}}


@pytest.mark.parametrize("cell, paise", [
    ("₹1,12,000.00", 1_12_000_00), ("5,000.00", 5_000_00), ("5,000.00 Cr", 5_000_00),
    ("-5,000.00", -5_000_00), ("(5,000.00)", -5_000_00), ("5,000.00 Dr", -5_000_00), ("5,000.00-", -5_000_00),
])
def test_statement_amounts_keep_their_sign(cell, paise):
    assert _amount(cell) == paise


def test_reversals_in_the_credit_column_are_not_money_received():
    rows = [["2026-10-05", "NEFT CR/N1/GLOBEX RETAIL/INV-2291", "N1", "3,40,000.00"],
            ["2026-10-06", "NEFT RETURN/N1/GLOBEX RETAIL", "N2", "-3,40,000.00"],
            ["2026-10-06", "CHQ RETURN/GLOBEX RETAIL", "N3", "(50,000.00)"]]
    records = parse_rows(["Date", "Narration", "Ref", "Credit"], rows, MAPPING)
    assert [(r.id, r.amount) for r in records] == [("N1", 3_40_000_00)]


def test_docker_build_context_leaves_out_secrets_and_state():
    from pathlib import Path
    lines = (Path(__file__).parents[2] / ".dockerignore").read_text().splitlines()
    assert {".env", "var/", ".git/", ".venv/"} <= set(lines) and "!.env.example" in lines


def test_credits_without_a_bank_reference_get_their_own_stable_ids():
    rows = [["2026-10-05", "CASH DEPOSIT GLOBEX RETAIL", "", "50,000.00"],
            ["2026-10-06", "CASH DEPOSIT GLOBEX RETAIL", " ", "75,000.00"],
            ["2026-10-06", "CASH DEPOSIT GLOBEX RETAIL", "", "75,000.00"]]
    header = ["Date", "Narration", "Ref", "Credit"]
    first = parse_rows(header, rows, MAPPING)
    ids = [r.id for r in first]
    assert len(set(ids)) == 3 and all(i.startswith("NOREF-") for i in ids)
    assert [r.id for r in parse_rows(header, rows, MAPPING)] == ids, "a re-read gives the same ids"


def test_a_second_unreferenced_credit_is_not_mistaken_for_an_applied_one():
    from datetime import date

    from subagents.matcher import Candidate, match
    rows = [["2026-10-05", "CASH DEP GLOBEX RETAIL", "", "50,000.00"],
            ["2026-10-07", "CASH DEP GLOBEX RETAIL", "", "30,000.00"]]
    records = parse_rows(["Date", "Narration", "Ref", "Credit"], rows, MAPPING)
    cands = [Candidate("e1", "INV-2291", "Globex Retail", 50_000_00, date(2026, 8, 1)),
             Candidate("e2", "INV-2295", "Globex Retail", 30_000_00, date(2026, 8, 9))]
    applied = {m.record_id for m in match(records[:1], cands, tolerance=0)}
    later = match(records, [cands[1]], tolerance=0, applied=applied)
    assert [(m.entity_id, m.matched_amount) for m in later] == [("e2", 30_000_00)]

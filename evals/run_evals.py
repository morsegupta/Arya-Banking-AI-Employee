"""Run the eval worlds and report metrics against ground truth.

    uv run python -m evals.run_evals                         every world, offline (scripted decisions)
    uv run python -m evals.run_evals --world sharma_chaos    one world (name or path)
    uv run python -m evals.run_evals --runs 2                run each world twice with the same memory
    uv run python -m evals.run_evals --live                  the real models (Groq key in .env); costs tokens

Each world runs in its own sandbox directory (evals/results/<stamp>/var/<world>); runs of the same
world share it, so run 2 starts with what run 1 learned (memory survives the re-seed). Output: a table
here, plus results.json and report.md in the results directory. Exit code 1 if any check fails.

Offline runs measure the runtime with a deterministic stand-in for the models (evals/scripted.py);
they do not measure model quality. Use --live for that.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from evals.harness import ROOT, WORLDS, run_world


def _world_paths(names: list[str]) -> list[Path]:
    if not names:
        return sorted(WORLDS.glob("*.yaml"))
    out = []
    for n in names:
        p = Path(n)
        out.append(p if p.suffix == ".yaml" and p.exists() else WORLDS / f"{n.removesuffix('.yaml')}.yaml")
    return out


def _money(paise) -> str:
    return "-" if paise is None else f"₹{paise / 100_000_00:.2f}L"


def _row(r: dict) -> list[str]:
    goal_count = r["goal"] == "confirm_balance"
    progress = f"{r['progress']}/{r['target']}" if goal_count else f"{_money(r['verified_paise'])}/{_money(r['target'])}"
    failed = [c["check"] for c in r["checks"] if not c["passed"]]
    return [r["world"], str(r.get("run_no", 1)), r["status"], str(r["days"]), progress,
            "-" if r["capture_rate"] is None else f"{r['capture_rate']:.0%}",
            "-" if r["promise_capture_rate"] is None else f"{r['promise_capture_rate']:.0%}",
            f"{r['messages_sent']}/{r['messages_undelivered']}", str(r["human_asks_total"]), str(r["fallbacks_used"]),
            str(r["memory"]), f"{r['run']['wall_seconds']:.0f}s",
            "PASS" if r["passed"] and not r["run"]["error"] else f"FAIL ({len(failed) + bool(r['run']['error'])})"]


HEAD = ["world", "run", "status", "days", "progress", "captured", "promises", "msgs/failed", "human", "fallbk", "memory",
        "time", "checks"]


def table(results: list[dict]) -> str:
    rows = [HEAD, *[_row(r) for r in results]]
    widths = [max(len(row[i]) for row in rows) for i in range(len(HEAD))]
    line = lambda row: "  ".join(c.ljust(w) for c, w in zip(row, widths))  # noqa: E731
    return "\n".join([line(rows[0]), line(["-" * w for w in widths]), *[line(r) for r in rows[1:]]])


def markdown(results: list[dict], live: bool) -> str:
    out = [f"# Eval report ({'live models' if live else 'offline, scripted decisions'})", "",
           f"Generated {datetime.now():%Y-%m-%d %H:%M}.", "",
           "| " + " | ".join(HEAD) + " |", "|" + "---|" * len(HEAD)]
    out += ["| " + " | ".join(_row(r)) + " |" for r in results]
    out += ["", "Columns: progress = verified money (or customers confirmed) vs target; captured = verified / money "
            "that really arrived from in-scope customers; promises = customers who promised (ground truth) whose "
            "promise Arya recorded (customers handed to a human excluded); msgs/failed = messages Arya sent / not "
            "delivered; human = items Arya asked a human; fallbk = reliability fallbacks used.", ""]
    for r in results:
        failed = [c for c in r["checks"] if not c["passed"]]
        out.append(f"## {r['world']} (run {r.get('run_no', 1)}): {len(r['checks']) - len(failed)}/{len(r['checks'])} checks pass")
        if r["run"]["error"]:
            out.append(f"- **run error**: {r['run']['error']}")
        for c in failed:
            out.append(f"- FAIL {c['check']}: `{json.dumps(c['detail'], default=str)[:300]}`")
        out.append(f"- final states: {r['final_states']}; human asks: {r['human_asks']}; "
                   f"quarantined: {r['quarantined']}; fraud flagged: {r['fraud_flagged']}")
        out.append("")
    return "\n".join(out)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--world", action="append", default=[], help="world name or path (repeatable); default: all")
    ap.add_argument("--runs", type=int, default=1, help="runs per world, sharing memory")
    ap.add_argument("--days", type=int, default=None, help="override the world's days")
    ap.add_argument("--live", action="store_true", help="real models (needs GROQ_API_KEY); costs tokens")
    ap.add_argument("--out", default=None, help="results directory (default evals/results/<stamp>)")
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else ROOT / "evals" / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    results = []
    for path in _world_paths(args.world):
        for run_no in range(1, args.runs + 1):
            print(f"== {path.stem} run {run_no}{' (live)' if args.live else ''}", flush=True)
            r = await run_world(path, out / "var" / path.stem, live=args.live, days=args.days)
            r["run_no"] = run_no
            results.append(r)
            (out / f"{path.stem}-run{run_no}.log").write_text("\n".join(r.pop("log", [])))
            for c in r["checks"]:
                if not c["passed"]:
                    print(f"   FAIL {c['check']}: {json.dumps(c['detail'], default=str)[:200]}")
    (out / "results.json").write_text(json.dumps(results, indent=2, default=str))
    (out / "report.md").write_text(markdown(results, args.live))
    print("\n" + table(results))
    print(f"\nresults: {out}")
    return 0 if all(r["passed"] and not r["run"]["error"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Command line for the AI employee (Day 1: run pieces by hand; the orchestrator comes next).

    python -m apps.cli fetch [--min-days 45]
    python -m apps.cli statement [--from 2026-10-01] [--to 2026-10-19]
    python -m apps.cli outreach INV-2291
    python -m apps.cli step                         one orchestration pass (timers, inbox, bank sync)
    python -m apps.cli status INV-2291
    python -m apps.cli demo INV-2297 [--days 5] [--sim-llm]
    python -m apps.cli entities
    python -m apps.cli events [-n 30]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from apps.flows import format_money, outreach, run_task
from apps.runtime import build_runtime
from core.models import FailureReport

MISSION = "mis_day1"


def _print_result(res) -> None:
    if isinstance(res, FailureReport):
        print(f"FAILED at {res.stage}: {res.error}")
        for t in res.tried:
            print(f"  tried: {t}")
        return
    print(f"OK: {res.summary}")
    for ev in res.evidence:
        print(f"  evidence: [{ev.kind}] {ev.ref} — {ev.summary}")


async def cmd_fetch(args) -> None:
    rt = build_runtime()
    try:
        res = await run_task(rt, args.mission, "fetch_open_items",
                             goal=f"List invoices with status OPEN or PARTIALLY_PAID that are at least "
                                  f"{args.min_days} days overdue.",
                             success_criteria=["every matching invoice is listed", "amounts come from the ERP"])
        _print_result(res)
        if not isinstance(res, FailureReport):
            for output in res.outputs.values():
                for inv in output.get("invoices", []):
                    print(f"  {inv['number']}  {inv['customer_name']:<26} {inv['days_overdue']:>4}d  "
                          f"{format_money(inv['balance_paise']):>15}  {inv['status']}")
        print(f"  LLM calls: {len(rt.llm.calls)}, tokens: {sum(c['total_tokens'] for c in rt.llm.calls)}")
    finally:
        await rt.close()


async def cmd_statement(args) -> None:
    rt = build_runtime()
    try:
        today = rt.clock.now().date().isoformat()
        res = await run_task(rt, args.mission, "download_statement",
                             goal=f"Download the bank account statement as CSV for {args.date_from} to "
                                  f"{args.date_to or today} from the bank portal.",
                             success_criteria=["a CSV file covering the date range is saved"])
        _print_result(res)
        print(f"  LLM calls: {len(rt.llm.calls)}, tokens: {sum(c['total_tokens'] for c in rt.llm.calls)}")
    finally:
        await rt.close()


async def cmd_outreach(args) -> None:
    rt = build_runtime()
    try:
        out = await outreach(rt, args.mission, args.ref)
        for a in out.attempts:
            print(f"  {a}")
        if out.ok:
            m = out.message
            print(f"SENT via {out.channel} to {m['to_addr']} (message {m['id']})")
            print(f"  subject: {m.get('subject')}\n  attachments: {[a['filename'] for a in m['attachments']]}")
            print("  ---\n  " + m["body"].replace("\n", "\n  ") + "\n  ---")
            t = out.transition
            print(f"  pipeline: {t.from_state} -> {t.to_state}: {'ok' if t.ok else t.reason}")
        else:
            print("NOT SENT")
    finally:
        await rt.close()


async def cmd_entities(args) -> None:
    rt = build_runtime(use_llm=False)
    try:
        for e in rt.pipeline.list(args.mission):
            print(f"  {e.external_ref:<10} {e.state:<15} {format_money(e.value):>15}  {e.id}")
        for t in rt.scheduler.pending():
            print(f"  timer {t['fire_type']:<28} due {t['due_at']}  ({t['key']})")
    finally:
        await rt.close()


async def cmd_events(args) -> None:
    rt = build_runtime(use_llm=False)
    try:
        for e in rt.events.query()[-args.n:]:
            payload = json.dumps(e.payload, ensure_ascii=False, default=str)
            print(f"  #{e.seq:<4} {e.occurred_at:%m-%d %H:%M} {e.type:<28} {payload[:140]}")
        ok, bad = rt.events.verify_chain()
        print(f"  hash chain: {'intact' if ok else f'BROKEN at #{bad}'}")
    finally:
        await rt.close()


def _flush(orch, printed: int) -> int:
    for line in orch.log[printed:]:
        print("  " + line)
    return len(orch.log)


async def cmd_step(args) -> None:
    from apps.orchestrator import Orchestrator
    rt = build_runtime()
    try:
        orch = Orchestrator(rt, args.mission)
        await orch.step()
        _flush(orch, 0)
    finally:
        await rt.close()


async def cmd_status(args) -> None:
    rt = build_runtime(use_llm=False)
    try:
        e = next((x for x in rt.pipeline.list(args.mission) if x.external_ref == args.ref), None)
        if e is None:
            print(f"{args.ref} is not in mission {args.mission}")
            return
        print(f"{e.external_ref}: {e.state}  balance {format_money(int(e.data.get('balance', e.value)))}")
        for ev in rt.events.query(entity_id=e.id):
            p = ev.payload
            if ev.type == "entity.transitioned":
                print(f"  {ev.occurred_at:%a %d %b %H:%M}  {p['from']} -> {p['to']}  ({p['actor']})  "
                      f"facts={json.dumps(p['facts'], ensure_ascii=False, default=str)[:150]}")
            elif ev.type in {"entity.transition_refused", "message.classified", "settlement.matched",
                             "verification.completed"}:
                brief = {k: v for k, v in p.items() if k in {"to", "reason", "failed_clauses", "intent", "confidence",
                                                             "quote", "record_id", "kind", "passed"}}
                print(f"  {ev.occurred_at:%a %d %b %H:%M}  {ev.type}  {json.dumps(brief, ensure_ascii=False)[:180]}")
    finally:
        await rt.close()


async def cmd_demo(args) -> None:
    """Run one entity end to end. Also drives the SIMULATED customers and sandbox clock (demo only)."""
    from datetime import timedelta

    from apps.orchestrator import Orchestrator
    from sandbox.common import clock as sandbox_clock
    from sandbox.common.world import load_world
    from sandbox.customer_sim.__main__ import _voice
    from sandbox.customer_sim.engine import SimEngine

    rt = build_runtime()
    voice = _voice(args.sim_llm)
    sim = SimEngine(load_world(), "http://127.0.0.1:8103", "http://127.0.0.1:8102", voice=voice)
    orch = Orchestrator(rt, args.mission)
    printed = 0
    try:
        print(f"== {sandbox_clock.now():%a %d %b %Y %H:%M}: start {args.ref}")
        await orch.start(args.ref)
        printed = _flush(orch, printed)
        for day in range(args.days):
            world = await sim.tick()
            for r in world["replies"]:
                print(f"  [world] {r['customer']} replied ({r['decision']})")
            for p in world["payments"]:
                print(f"  [world] {p['customer']} paid {format_money(p['amount_paise'])} UTR {p['utr']}")
            await orch.step()
            printed = _flush(orch, printed)
            entity = next(e for e in rt.pipeline.list(args.mission) if e.external_ref == args.ref)
            print(f"  -> {args.ref} is {entity.state}")
            if entity.state in rt.pipeline.spec.terminal:
                break
            sandbox_clock.advance(timedelta(days=1))
            print(f"== {sandbox_clock.now():%a %d %b %Y %H:%M}")
        print(f"LLM calls: {len(rt.llm.calls)}, tokens: {sum(c['total_tokens'] for c in rt.llm.calls)}")
    finally:
        await sim.close()
        if hasattr(voice, "close"):
            await voice.close()
        await rt.close()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="arya")
    p.add_argument("--mission", default=MISSION)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--min-days", type=int, default=45)
    s = sub.add_parser("statement")
    s.add_argument("--from", dest="date_from", default="2026-10-01")
    s.add_argument("--to", dest="date_to", default=None)
    o = sub.add_parser("outreach")
    o.add_argument("ref")
    sub.add_parser("step")
    st = sub.add_parser("status")
    st.add_argument("ref")
    d = sub.add_parser("demo")
    d.add_argument("ref")
    d.add_argument("--days", type=int, default=5)
    d.add_argument("--sim-llm", action="store_true", help="simulated customers write replies with the LLM")
    sub.add_parser("entities")
    ev = sub.add_parser("events")
    ev.add_argument("-n", type=int, default=30)
    args = p.parse_args(argv)
    from sandbox.common import config
    from sandbox.common.runlock import Busy, run_lock
    handler = {"fetch": cmd_fetch, "statement": cmd_statement, "outreach": cmd_outreach, "step": cmd_step,
               "status": cmd_status, "demo": cmd_demo, "entities": cmd_entities, "events": cmd_events}[args.cmd]
    if args.cmd in {"entities", "events", "status"}:      # read-only
        asyncio.run(handler(args))
        return
    try:
        with run_lock(config.var_dir(), f"apps.cli {args.cmd}"):
            asyncio.run(handler(args))
    except Busy as exc:
        raise SystemExit(f"refusing to run: {exc}")


if __name__ == "__main__":
    sys.exit(main())

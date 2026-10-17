"""Drive the simulated customers and the sandbox clock.

    python -m sandbox.customer_sim tick [--llm]     answer new messages, execute due payments
    python -m sandbox.customer_sim advance 1d       move simulated time (also 6h, 30m)
    python -m sandbox.customer_sim truth            ground truth so far (for evals)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import timedelta
from pathlib import Path

import yaml

from sandbox.common import clock
from sandbox.common.world import DEFAULT_WORLD, load_world
from sandbox.customer_sim.engine import LLMVoice, SimEngine, TemplateVoice

ROOT = Path(__file__).resolve().parents[2]


def _env(key: str) -> str | None:
    if key in os.environ:
        return os.environ[key]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith(f"{key}="):
                return line.split("=", 1)[1].strip() or None
    return None


def _voice(use_llm: bool):
    if not use_llm:
        return TemplateVoice()
    key = _env("GROQ_API_KEY")
    if not key:
        raise SystemExit("GROQ_API_KEY not set (.env): run without --llm for template replies")
    model = yaml.safe_load((ROOT / "config" / "models.yaml").read_text())["roles"]["simulator"]["model"]
    return LLMVoice(_env("GROQ_BASE_URL") or "https://api.groq.com/openai/v1", key, model)


async def _tick(use_llm: bool) -> None:
    voice = _voice(use_llm)
    engine = SimEngine(load_world(DEFAULT_WORLD), os.environ.get("APP_URL_HUB", "http://127.0.0.1:8103"),
                       os.environ.get("APP_URL_BANK_PORTAL", "http://127.0.0.1:8102"), voice=voice)
    try:
        out = await engine.tick()
    finally:
        await engine.close()
        if isinstance(voice, LLMVoice):
            await voice.close()
    print(f"sandbox time: {clock.now():%a %d %b %Y %H:%M}")
    for r in out["replies"]:
        print(f"  {r['customer']} -> {r['decision']:<17} {','.join(r['invoices'])}"
              + (f"  (will pay {r['pay_on']})" if r["pay_on"] else ""))
    for p in out["payments"]:
        print(f"  PAID {p['customer']} {p['amount_paise'] / 100:,.2f} for {','.join(p['invoices'])} UTR {p['utr']}")
    if not out["replies"] and not out["payments"]:
        print("  nothing to do")


def _advance(spec: str) -> None:
    m = re.fullmatch(r"(\d+)([dhm])", spec)
    if not m:
        raise SystemExit("use e.g. 1d, 6h, 30m")
    n, unit = int(m.group(1)), m.group(2)
    when = clock.advance({"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit])
    print(f"sandbox time: {when:%a %d %b %Y %H:%M}")


def main() -> None:
    p = argparse.ArgumentParser(prog="customer_sim")
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--llm", action="store_true", help="customers' wording by the simulator LLM")
    a = sub.add_parser("advance")
    a.add_argument("by")
    sub.add_parser("truth")
    args = p.parse_args()
    if args.cmd == "tick":
        asyncio.run(_tick(args.llm))
    elif args.cmd == "advance":
        _advance(args.by)
    else:
        print(json.dumps(SimEngine.ground_truth(), indent=2, default=str))


if __name__ == "__main__":
    main()

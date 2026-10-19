"""Re-observing what counterparties wrote, before a mission that counts confirmations completes.

The ledger of transitions says a group confirmed; completion believes only what is still in the
channel: for every entity counted, the message behind its transition is read again from the inbox,
it must come from that counterparty's own address on that channel, and the quoted sentence must be
in it verbatim. An entity counted because it was settled must have a verified ledger line instead.
Plain code, no LLM (the Verifier's deterministic half).
"""

from __future__ import annotations

import json
import re
from typing import Any

from apps.runtime import Runtime
from apps.settlement import ensure_ledger
from core.models import Evidence
from tools.base import RunContext


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


async def _inbox(rt: Runtime, mission_id: str, channel: str) -> dict[str, dict[str, Any]] | None:
    ctx = RunContext(mission_id=mission_id, task_id=f"verify-confirmations-{mission_id}", allowed_apps={channel},
                     artifacts_dir=rt.artifacts_dir)
    res = await rt.registry.invoke(f"{channel}.inbox", {"since_seq": 0}, ctx)
    return {m["id"]: m for m in res.output} if res.ok else None


async def fresh_confirmations(rt: Runtime, mission_id: str,
                              groups: dict[str, list[str]]) -> tuple[int, Evidence | None, str]:
    """How many of `groups` ({group: [entity ids]}) still hold up against a fresh read of the channels."""
    if not groups:
        return 0, None, "nothing confirmed"
    ensure_ledger(rt)
    with rt.db.read() as conn:
        settled = {r["entity_id"] for r in conn.execute("SELECT entity_id FROM settlements WHERE status = 'VERIFIED'")}
    inboxes: dict[str, dict[str, dict[str, Any]] | None] = {}
    ok_groups, failed = [], []
    for gid, ids in groups.items():
        good = True
        for eid in ids:
            if eid in settled:
                continue
            e = rt.pipeline.get(eid)
            proofs = [x for x in rt.evidence.for_entity(eid)
                      if x["subject"].startswith("transition:") and x["kind"] == "message"]
            held = False
            for x in proofs:
                data = json.loads(x["data_json"])
                channel = data.get("channel")
                if channel not in inboxes:
                    inboxes[channel] = await _inbox(rt, mission_id, channel) if channel else None
                msg = (inboxes[channel] or {}).get(x["ref"])
                sender_ok = msg is not None and str(msg.get("from_addr", "")).lower() in {
                    str(v).lower() for v in e.data.get("contact", {}).values() if v}
                if sender_ok and _norm(x["summary"]) in _norm(msg.get("body", "")):
                    held = True
                    break
            if not held:
                good = False
                break
        (ok_groups if good else failed).append(gid)
    ev = Evidence(kind="note", ref=f"reobserved:{mission_id}",
                  summary=f"{len(ok_groups)} of {len(groups)} groups re-observed in the channels",
                  data={"held": ok_groups, "failed": failed})
    return len(ok_groups), ev, f"{len(ok_groups)} of {len(groups)} re-observed" + (
        f"; not found: {failed}" if failed else "")

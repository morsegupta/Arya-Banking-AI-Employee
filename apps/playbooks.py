"""Playbooks: scripted procedures from an app map, run without an LLM (procedural memory).

    open:       {path template}                       -> page snapshot
    download:   {link name}                           -> saved file path
    read_table: {table label, next: link name, max_pages}  -> header + rows across pages

A playbook is the cheapest, most predictable way to do a routine task. When the app changes
and a step fails, the caller falls back to the Operator (an LLM that can adapt), and the
failure is recorded so the playbook can be fixed. Every step still goes through the tool
registry: capability scope, Policy Gate, retries and audit apply exactly as for the Operator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from apps.runtime import Runtime
from core.models import Event, Evidence
from subagents.docgen import fill
from tools.app_map import load_app_maps
from tools.base import RunContext


@dataclass
class PlaybookResult:
    ok: bool
    files: list[str] = field(default_factory=list)
    header: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    error: str | None = None


class PlaybookFailed(Exception):
    pass


def _find(observation: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((e for e in observation.get("elements", []) if e.get("name") == name), None)


async def run_playbook(rt: Runtime, app: str, name: str, values: dict[str, Any], *, mission_id: str | None,
                       task_id: str, entity_id: str | None = None) -> PlaybookResult:
    steps = load_app_maps(rt.company_dir / "apps")[app].playbooks.get(name)
    if not steps:
        return PlaybookResult(False, error=f"no playbook {name!r} for {app}")
    ctx = RunContext(mission_id=mission_id, task_id=task_id, entity_id=entity_id, allowed_apps={app},
                     artifacts_dir=rt.artifacts_dir)
    out = PlaybookResult(True)
    obs: dict[str, Any] = {}

    async def call(tool: str, args: dict[str, Any]) -> Any:
        res = await rt.registry.invoke(f"{app}.{tool}", args, ctx)
        if not res.ok:
            raise PlaybookFailed(f"{tool}: {res.error}")
        out.evidence += res.evidence
        return res.output

    try:
        for step in steps:
            if "open" in step:
                obs = await call("open", {"path": fill(step["open"], values)})
            elif "download" in step:
                link = _find(obs, step["download"])
                if link is None:
                    raise PlaybookFailed(f"no {step['download']!r} on {obs.get('url')}")
                out.files.append((await call("download", {"ref": link["ref"]}))["path"])
            elif "read_table" in step:
                for _ in range(int(step.get("max_pages", 20))):
                    tables = (await call("read_table", {}))["tables"]
                    table = next((t for t in tables if t["label"] == step["read_table"]), None)
                    if table is None:
                        raise PlaybookFailed(f"no table {step['read_table']!r} on {obs.get('url')}")
                    out.header = out.header or table["headers"]
                    out.rows += [r for r in table["rows"] if len(r) == len(table["headers"])]
                    nxt = _find(obs, step["next"]) if step.get("next") else None
                    if nxt is None:
                        break
                    obs = await call("click", {"ref": nxt["ref"]})
            else:
                raise PlaybookFailed(f"unknown step {step}")
    except PlaybookFailed as exc:
        out.ok, out.error = False, str(exc)
    finally:
        await rt.sessions.end_task(task_id)
    rt.events.append(Event(type="playbook.completed" if out.ok else "playbook.failed", mission_id=mission_id,
                           entity_id=entity_id, task_id=task_id, occurred_at=rt.clock.now(),
                           payload={"app": app, "playbook": name, "error": out.error, "files": out.files,
                                    "rows": len(out.rows)}))
    return out

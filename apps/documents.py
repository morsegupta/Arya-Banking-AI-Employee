"""Generated documents (Document Generator slot): fill a role pack template from the system of record.

The template (role.yaml `documents:`) says which tool lists the rows, which columns to print and
which notes to add. Values come only from the system of record and the company facts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from apps.flows import _read, company_facts, format_money, role_cfg
from apps.runtime import Runtime
from core.models import Event
from subagents.docgen import TableDocument, columns_from, fill, render


async def generate(rt: Runtime, mission_id: str, name: str, *, group: str, group_name: str = "") -> str:
    """Render document `name` for one counterparty. Returns its path relative to the shared drive."""
    spec = role_cfg(rt)["documents"][name]
    today = rt.clock.now().date().isoformat()
    values: dict[str, Any] = {**company_facts(rt), "group": group, "group_name": group_name, "date": today}
    args = {k: fill(str(v), values) for k, v in spec.get("list_args", {}).items()}
    listed = await _read(rt, spec["list_tool"], args, mission_id)
    rows = listed[spec["list_key"]] if spec.get("list_key") else listed
    if not group_name and rows:
        values["group_name"] = rows[0].get(spec.get("group_name_field", ""), "") or group
    money = set(spec.get("money", []))
    printable = [{k: (format_money(v, "Rs. ") if k in money and v is not None else v) for k, v in r.items()}
                 for r in rows]
    totals = [(label, format_money(sum(int(r.get(key) or 0) for r in rows), "Rs. "))
              for label, key in spec.get("totals", [])]
    doc = TableDocument(title=spec["title"], issuer_lines=[fill(x, values) for x in spec.get("issuer", [])],
                        fields=[(fill(a, values), fill(b, values)) for a, b in spec.get("fields", [])],
                        columns=columns_from(spec["columns"]), rows=printable, totals=totals,
                        notes=[fill(x, values) for x in spec.get("notes", [])], created_at=rt.clock.now())
    rel = Path(spec.get("folder", "generated")) / fill(spec["filename"], values)
    render(doc, rt.drive_dir / rel)
    rt.events.append(Event(type="document.generated", mission_id=mission_id, occurred_at=rt.clock.now(),
                           payload={"name": name, "group": group, "path": str(rel), "rows": len(rows),
                                    "refs": [r.get("number") for r in rows]}))
    return str(rel)

"""Generalization guarantee (ADR-8): the runtime core never names the collections domain.

Domain words belong in packs/ and sandbox/. If this fails, move the knowledge into a Role Pack.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIRS = ["core", "agents", "subagents"]
DOMAIN_WORDS = re.compile(r"invoice|payment|bank|receipt|collection|overdue|rupee|paise|\butr\b|\berp\b", re.I)


def test_runtime_core_is_domain_free():
    hits = []
    for d in RUNTIME_DIRS:
        for path in (ROOT / d).rglob("*.py") if (ROOT / d).exists() else []:
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if DOMAIN_WORDS.search(line.replace("collections.abc", "")):
                    hits.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, "domain words in runtime core:\n" + "\n".join(hits)


def test_core_does_not_import_packs_or_sandbox():
    for path in (ROOT / "core").rglob("*.py"):
        text = path.read_text()
        assert not re.search(r"^\s*(from|import)\s+(packs|sandbox|tools|apps|agents|subagents)\b", text, re.M), path


def test_agents_never_reach_into_the_wiring_or_the_sandbox():
    """Agents and sub-agents get what they need as arguments: no packs, no sandbox, no app wiring."""
    for d in ("agents", "subagents"):
        for path in (ROOT / d).rglob("*.py"):
            text = path.read_text()
            assert not re.search(r"^\s*(from|import)\s+(packs|sandbox|apps)\b", text, re.M), path


def test_role_pack_goals_name_known_measures_approaches_and_levers():
    """A new brief is data: every goal in a role pack must be runnable without code."""
    import yaml
    for role in (ROOT / "packs" / "roles").glob("*/role.yaml"):
        cfg = yaml.safe_load(role.read_text())
        mission = cfg.get("mission", {})
        states = set(cfg.get("pipeline", {}).get("states", []))
        for name, goal in mission.get("goals", {}).items():
            assert goal.get("measure") in {"settled_value", "groups_reached"}, (role, name)
            assert set(goal.get("approaches", [])) <= set(mission.get("approaches", {})), (role, name)
            assert set(goal.get("levers", [])) <= set(mission.get("levers", {})), (role, name)
            assert set(goal.get("states", [])) <= states, (role, name)

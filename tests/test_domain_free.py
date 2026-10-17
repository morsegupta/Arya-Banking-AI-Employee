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
        assert not re.search(r"^\s*(from|import)\s+(packs|sandbox|tools)\b", text, re.M), path

"""Injection Guard: screen untrusted text before any agent reads it (architecture §8.5, layer 1).

    score = max(classifier score over overlapping chunks, pattern score)

- The classifier is a scorer function (Prompt Guard 2 86M through the LLM client); it has a
  512-token window, so the client chunks the text and keeps the highest score.
- Patterns are a cheap, model-independent second opinion: generic ones live here, the
  role/company pack adds its own (e.g. phrases that try to change an entity's state).
- Results are cached by content hash, so a page observed again unchanged is not re-screened.
- If the classifier is unavailable, `on_error` decides: "flag" treats the text as suspicious
  (fail closed), "patterns" falls back to the patterns alone.

The guard never decides what happens to flagged content; callers quarantine it and the
Policy Gate makes every risky action touching it wait for a human. It is one layer:
state still changes only through code-checked guards.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core.store import Database

GENERIC_PATTERNS = [
    r"\b(system|assistant|developer)\s*(prompt|message|override)?\s*:",
    r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|messages|rules)",
    r"disregard\s+(all\s+|any\s+)?(previous|prior|your)\s+(instructions|rules)",
    r"you\s+are\s+now\s+(a|an|the)\b",
    r"</?\s*(system|assistant|untrusted_data|tool)\s*>",
    r"\bnew\s+instructions?\s*:",
    r"\bdo\s+not\s+(tell|inform|notify)\s+(the\s+)?(user|human|manager|anyone)",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS injection_cache (
    digest TEXT PRIMARY KEY,
    model_score REAL,
    error TEXT
);
"""

Scorer = Callable[[str], Awaitable[float]]


@dataclass
class Screening:
    score: float
    flagged: bool
    model_score: float | None
    pattern_hits: list[str] = field(default_factory=list)
    error: str | None = None
    cached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"score": round(self.score, 3), "flagged": self.flagged, "model_score": self.model_score,
                "pattern_hits": self.pattern_hits, "error": self.error}


class InjectionGuard:
    def __init__(self, scorer: Scorer | None, db: Database | None = None, *, threshold: float = 0.5,
                 patterns: list[str] | None = None, on_error: str = "flag", pattern_score: float = 0.9):
        self.scorer, self.db, self.threshold, self.on_error = scorer, db, threshold, on_error
        self.pattern_score = pattern_score
        self.patterns = [re.compile(p, re.I) for p in GENERIC_PATTERNS + list(patterns or [])]
        if db:
            db.ensure_schema(SCHEMA)

    def pattern_hits(self, text: str) -> list[str]:
        return [m.group(0) for p in self.patterns if (m := p.search(text))]

    async def screen(self, text: str) -> Screening:
        hits = self.pattern_hits(text)
        model, error, cached = await self._model_score(text)
        score = max(model or 0.0, self.pattern_score if hits else 0.0)
        flagged = score >= self.threshold
        if error and model is None and self.on_error == "flag":
            flagged = True
        return Screening(score=score, flagged=flagged, model_score=model, pattern_hits=hits, error=error,
                         cached=cached)

    async def _model_score(self, text: str) -> tuple[float | None, str | None, bool]:
        if not text.strip():
            return 0.0, None, False
        if self.scorer is None:
            return None, "no classifier configured", False
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self.db:
            with self.db.read() as conn:
                row = conn.execute("SELECT * FROM injection_cache WHERE digest = ?", (digest,)).fetchone()
            if row and row["error"] is None:
                return row["model_score"], None, True
        try:
            score, error = float(await self.scorer(text)), None
        except Exception as exc:  # noqa: BLE001 - any classifier failure is handled by on_error
            score, error = None, f"classifier unavailable: {str(exc)[:200]}"
        if self.db and error is None:
            with self.db.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO injection_cache VALUES (?,?,NULL)", (digest, score))
        return score, error, False


def redacted(text: str, screening: Screening) -> str:
    """What an agent sees instead of quarantined text."""
    why = f"{len(screening.pattern_hits)} suspicious pattern(s)" if screening.pattern_hits else "classifier"
    # never echo the flagged text itself, not even the matched fragments
    return (f"[QUARANTINED: this content looked like an attempt to instruct the AI "
            f"(score {screening.score:.2f}; {why}). {len(text)} characters withheld; a human will review it.]")

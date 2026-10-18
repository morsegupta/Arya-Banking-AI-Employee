"""Prioritizer: rank entities and the groups they belong to. Plain code, no LLM.

    item score  = w_value * value/max_value + w_age * age/max_age + w_risk * group risk
    group score = sum of its items' scores (work is done per group: one contact covers its items)

Group risk comes from signals the role pack names (e.g. how many open items the group has,
whether it has contested items before). The suggested approach per group comes from the role
pack's rules, written as guard expressions and evaluated by core.guards, first match wins.
The Mission Agent may override the approach; this module only proposes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.guards import compile_guard, evaluate


@dataclass
class Item:
    ref: str
    group: str                  # counterparty id: items of one group are handled together
    group_name: str
    value: int                  # minor units still open
    age: int                    # days past due
    data: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class Group:
    id: str
    name: str
    items: list[Item]
    score: float
    risk: float
    signals: dict[str, Any]
    approach: str
    rationale: str

    @property
    def value(self) -> int:
        return sum(i.value for i in self.items)

    def as_row(self) -> dict[str, Any]:
        """Compact form for an LLM prompt."""
        flags = [k for k, v in self.signals.items() if v is True]
        return {"group": self.id, "name": self.name, "value": self.value, "items": len(self.items),
                "max_age": max(i.age for i in self.items), "score": round(self.score, 2),
                "suggested_approach": self.approach, **({"flags": flags} if flags else {})}


@dataclass
class ScorerConfig:
    weights: dict[str, float]
    risk_signals: dict[str, float]       # signal name -> weight in group risk (signals are 0..1 or booleans)
    approach_rules: list[dict[str, Any]]  # [{when: guard, approach: name}] first match wins
    default_approach: str


def _risk(signals: dict[str, Any], weights: dict[str, float]) -> float:
    total = sum(weights.values()) or 1.0
    return sum(float(signals.get(k, 0)) * w for k, w in weights.items()) / total


def rank(items: list[Item], cfg: ScorerConfig, signals: dict[str, dict[str, Any]] | None = None) -> list[Group]:
    """`signals[group_id]` holds facts about the group beyond its items (history, flags)."""
    if not items:
        return []
    signals = signals or {}
    max_value = max(i.value for i in items) or 1
    max_age = max(i.age for i in items) or 1
    by_group: dict[str, list[Item]] = {}
    for i in items:
        by_group.setdefault(i.group, []).append(i)

    groups = []
    rules = [(compile_guard(r["when"]), r["approach"], r["when"]) for r in cfg.approach_rules]
    for gid, members in by_group.items():
        sig = {"items": len(members), "max_age": max(i.age for i in members),
               "value": sum(i.value for i in members), "multi": len(members) > 1, **signals.get(gid, {})}
        risk = _risk(sig, cfg.risk_signals)
        for i in members:
            i.score = (cfg.weights.get("value", 0) * i.value / max_value + cfg.weights.get("age", 0) * i.age / max_age
                       + cfg.weights.get("risk", 0) * risk)
        members.sort(key=lambda i: (-i.age, i.ref))          # oldest first inside a group
        approach, why = cfg.default_approach, "default"
        for guard, name, text in rules:
            if evaluate(guard, {"group": sig}).passed:
                approach, why = name, text
                break
        groups.append(Group(gid, members[0].group_name, members, sum(i.score for i in members), risk, sig,
                            approach, f"approach {approach} because {why}"))
    groups.sort(key=lambda g: (-g.score, g.id))
    return groups

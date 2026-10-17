"""Guard expressions from role packs, evaluated by code (never by an LLM, never with eval()).

Grammar (one clause):
    clause   := operand OP operand | operand "exists" | operand
    operand  := term (("+" | "-") term)*
    term     := path | number | 'string' | "string" | true | false | null
    path     := name ("." name)*          e.g. classifier.confidence, entity.balance
    OP       := == != >= <= > <

Guards combine clauses with {all: [...]}, {any: [...]}, nested freely.
Every clause result is recorded with the values it saw, so a transition's
audit trail shows exactly why it was allowed or refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_TOKEN = re.compile(r"""\s*(?:
    (?P<num>\d+(?:\.\d+)?)
  | (?P<str>'[^']*'|"[^"]*")
  | (?P<op>==|!=|>=|<=|>|<|\+|-)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
)""", re.X)

MISSING = object()


class GuardSyntaxError(ValueError):
    pass


@dataclass
class ClauseResult:
    clause: str
    passed: bool
    values: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class GuardResult:
    passed: bool
    clauses: list[ClauseResult]

    def failures(self) -> list[str]:
        return [c.clause + (f" ({c.error})" if c.error else "") for c in self.clauses if not c.passed]


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise GuardSyntaxError(f"cannot parse {text[pos:]!r} in {text!r}")
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))
        pos = m.end()
    return tokens


def lookup(ctx: dict[str, Any], path: str) -> Any:
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


class _Clause:
    def __init__(self, text: str):
        self.text = text
        self.tokens = _tokenize(text)
        self.pos = 0
        self.lhs = self._operand()
        self.op: str | None = None
        self.rhs = None
        if self.pos < len(self.tokens):
            kind, val = self.tokens[self.pos]
            if kind == "name" and val == "exists":
                self.op = "exists"
                self.pos += 1
            elif kind == "op" and val in {"==", "!=", ">=", "<=", ">", "<"}:
                self.op = val
                self.pos += 1
                self.rhs = self._operand()
        if self.pos != len(self.tokens):
            raise GuardSyntaxError(f"unexpected {self.tokens[self.pos][1]!r} in {text!r}")

    def _operand(self) -> list:
        terms = [("+", self._term())]
        while self.pos < len(self.tokens) and self.tokens[self.pos] in {("op", "+"), ("op", "-")}:
            sign = self.tokens[self.pos][1]
            self.pos += 1
            terms.append((sign, self._term()))
        return terms

    def _term(self) -> tuple[str, Any]:
        if self.pos >= len(self.tokens):
            raise GuardSyntaxError(f"missing operand in {self.text!r}")
        kind, val = self.tokens[self.pos]
        self.pos += 1
        if kind == "num":
            return ("lit", float(val) if "." in val else int(val))
        if kind == "str":
            return ("lit", val[1:-1])
        if kind == "name":
            if val in {"true", "false", "null"}:
                return ("lit", {"true": True, "false": False, "null": None}[val])
            return ("path", val)
        raise GuardSyntaxError(f"unexpected {val!r} in {self.text!r}")

    def _value(self, operand: list, ctx: dict, seen: dict) -> Any:
        total: Any = None
        for i, (sign, (kind, val)) in enumerate(operand):
            v = val
            if kind == "path":
                v = lookup(ctx, val)
                seen[val] = None if v is MISSING else v
                if v is MISSING:
                    return MISSING
            if i == 0:
                total = v
            else:
                if not isinstance(total, (int, float)) or not isinstance(v, (int, float)) \
                        or isinstance(total, bool) or isinstance(v, bool):
                    raise TypeError(f"arithmetic on non-numbers in {self.text!r}")
                total = total + v if sign == "+" else total - v
        return total

    def evaluate(self, ctx: dict) -> ClauseResult:
        seen: dict[str, Any] = {}
        try:
            left = self._value(self.lhs, ctx, seen)
            if self.op == "exists":
                return ClauseResult(self.text, left is not MISSING and left is not None and left != "", seen)
            if self.op is None:
                return ClauseResult(self.text, left is not MISSING and bool(left), seen)
            right = self._value(self.rhs, ctx, seen)
            if left is MISSING or right is MISSING:
                return ClauseResult(self.text, False, seen, "missing value")
            ok = {"==": left == right, "!=": left != right}.get(self.op)
            if ok is None:
                if type(left) is bool or type(right) is bool or not (
                        isinstance(left, (int, float)) and isinstance(right, (int, float))
                        or isinstance(left, str) and isinstance(right, str)):
                    return ClauseResult(self.text, False, seen, "incomparable values")
                ok = {">=": left >= right, "<=": left <= right, ">": left > right, "<": left < right}[self.op]
            return ClauseResult(self.text, bool(ok), seen)
        except TypeError as exc:
            return ClauseResult(self.text, False, seen, str(exc))


def compile_guard(spec: Any) -> Any:
    """Parse once at load time so a bad role pack fails fast, not mid-mission."""
    if spec is None:
        return None
    if isinstance(spec, str):
        return _Clause(spec)
    if isinstance(spec, dict) and len(spec) == 1 and next(iter(spec)) in {"all", "any"}:
        key = next(iter(spec))
        return (key, [compile_guard(s) for s in spec[key]])
    raise GuardSyntaxError(f"guard must be a string or {{all|any: [...]}}, got {spec!r}")


def evaluate(compiled: Any, ctx: dict[str, Any]) -> GuardResult:
    if compiled is None:
        return GuardResult(True, [])
    clauses: list[ClauseResult] = []

    def walk(node) -> bool:
        if isinstance(node, _Clause):
            res = node.evaluate(ctx)
            clauses.append(res)
            return res.passed
        mode, children = node
        results = [walk(c) for c in children]   # evaluate all, so the audit shows every clause
        return all(results) if mode == "all" else any(results)

    return GuardResult(walk(compiled), clauses)

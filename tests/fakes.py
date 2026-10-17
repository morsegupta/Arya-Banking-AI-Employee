"""Scripted LLM: replays prepared responses so agent loops are tested without a network."""

from __future__ import annotations

import json
from typing import Any, Callable

from core.llm import LLMClient, LLMResponse, ToolCall


def call(name: str, args: dict[str, Any], cid: str | None = None) -> ToolCall:
    return ToolCall(cid or f"call_{name}_{abs(hash(json.dumps(args, sort_keys=True))) % 10**6}", name, args,
                    json.dumps(args))


class ScriptedLLM(LLMClient):
    """Each script item is an LLMResponse, a dict (JSON content), or a callable(messages) -> either."""

    def __init__(self, script: list[Any]):
        self.script = list(script)
        self.seen: list[dict[str, Any]] = []
        self.calls = []

    async def chat(self, role, messages, *, tools=None, json_mode=False, budget=None, tag=""):
        if budget:
            budget.check()
        self.seen.append({"role": role, "messages": [dict(m) for m in messages], "tools": tools, "json": json_mode})
        if not self.script:
            raise AssertionError(f"ScriptedLLM ran out of responses (role={role})")
        item = self.script.pop(0)
        if callable(item):
            item = item(messages)
        if isinstance(item, dict):
            item = LLMResponse(json.dumps(item), [], {"total_tokens": 100})
        if budget:
            budget.charge(item.usage.get("total_tokens", 100))
        return item

    async def close(self):
        pass


def tools(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(None, list(calls), {"total_tokens": 500})

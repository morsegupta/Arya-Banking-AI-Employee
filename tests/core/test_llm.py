from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel

from core.llm import Budget, BudgetExceeded, LLMClient, LLMError, LLMOutputError, RoleConfig, parse_duration

ROLES = {"operator": RoleConfig("openai/gpt-oss-120b", "medium", 500),
         "light": RoleConfig("qwen/qwen3.8-27b", max_tokens=300),
         "guard": RoleConfig("meta-llama/llama-prompt-guard-2-86m")}


def ok(content=None, tool_calls=None, headers=None, tokens=50):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, headers=headers or {}, json={
        "choices": [{"message": msg, "finish_reason": "stop"}], "usage": {"total_tokens": tokens}})


class Fake:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, request: httpx.Request):
        self.requests.append(json.loads(request.content))
        return self.responses.pop(0)


def client(fake, sleeps, now=lambda: 100.0):
    async def sleep(s):
        sleeps.append(s)
    return LLMClient("https://llm.test/v1", "k", ROLES, transport=httpx.MockTransport(fake), sleep=sleep, clock=now)


@pytest.mark.parametrize("text, secs", [("1m26.4s", 86.4), ("735ms", 0.735), ("3.457s", 3.457), ("2", 2.0),
                                        ("1h2m3s", 3723.0), (None, 0.0)])
def test_parse_duration(text, secs):
    assert parse_duration(text) == pytest.approx(secs)


def test_request_shape_and_tool_calls():
    fake = Fake([ok(tool_calls=[{"id": "c1", "type": "function",
                                 "function": {"name": "erp__list", "arguments": '{"a": 1}'}}])])
    llm = client(fake, [])
    resp = asyncio.run(llm.chat("operator", [{"role": "user", "content": "hi"}], tools=[{"type": "function"}]))
    body = fake.requests[0]
    assert body["model"] == "openai/gpt-oss-120b" and body["reasoning_effort"] == "medium"
    assert body["max_completion_tokens"] == 500 and body["tool_choice"] == "auto"
    assert resp.tool_calls[0].name == "erp__list" and resp.tool_calls[0].arguments == {"a": 1}
    assert resp.assistant_message()["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'


def test_waits_when_token_window_is_spent():
    sleeps = []
    fake = Fake([ok("a", headers={"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "7s"}),
                 ok("b")])
    llm = client(fake, sleeps)
    asyncio.run(llm.chat("operator", [{"role": "user", "content": "x"}]))
    asyncio.run(llm.chat("operator", [{"role": "user", "content": "y"}]))
    assert sleeps == [pytest.approx(7.0)]


def test_429_honours_retry_after():
    sleeps = []
    fake = Fake([httpx.Response(429, headers={"retry-after": "3"}, text="slow down"), ok("done")])
    resp = asyncio.run(client(fake, sleeps).chat("light", [{"role": "user", "content": "x"}]))
    assert resp.content == "done" and 3.0 <= sleeps[0] <= 3.5


def test_5xx_retries_then_gives_up():
    sleeps = []
    fake = Fake([httpx.Response(503)] * 5)
    with pytest.raises(LLMError, match="failed after 5 attempts"):
        asyncio.run(client(fake, sleeps).chat("light", [{"role": "user", "content": "x"}]))
    assert len(sleeps) == 5


def test_4xx_is_not_retried():
    fake = Fake([httpx.Response(400, text="bad tool schema")])
    with pytest.raises(LLMError, match="rejected request"):
        asyncio.run(client(fake, []).chat("light", [{"role": "user", "content": "x"}]))


def test_budget():
    fake = Fake([ok("a", tokens=600), ok("b")])
    llm, budget = client(fake, []), Budget(500)
    asyncio.run(llm.chat("light", [{"role": "user", "content": "x"}], budget=budget))
    with pytest.raises(BudgetExceeded):
        asyncio.run(llm.chat("light", [{"role": "user", "content": "x"}], budget=budget))


class Out(BaseModel):
    intent: str
    confidence: float


def test_structured_with_one_repair():
    fake = Fake([ok('{"intent": "promise"}'), ok('```json\n{"intent": "promise", "confidence": 0.9}\n```')])
    out = asyncio.run(client(fake, []).structured("light", [{"role": "user", "content": "x"}], Out))
    assert out == Out(intent="promise", confidence=0.9)
    assert fake.requests[0]["response_format"] == {"type": "json_object"}
    assert "invalid" in fake.requests[1]["messages"][-1]["content"]


def test_structured_gives_up_after_repair():
    fake = Fake([ok("nope"), ok("{}")])
    with pytest.raises(LLMOutputError):
        asyncio.run(client(fake, []).structured("light", [{"role": "user", "content": "x"}], Out))


def test_guard_chunks_and_takes_max():
    fake = Fake([ok("0.01"), ok("0.97"), ok("0.02")])
    score = asyncio.run(client(fake, []).guard_score("a" * 4000, chunk_chars=1600, overlap=200))
    assert score == pytest.approx(0.97) and len(fake.requests) == 3
    assert asyncio.run(client(Fake([]), []).guard_score("   ")) == 0.0


def test_malformed_model_output_is_resampled_then_raised():
    from core.llm import ToolUseFailed
    bad = lambda code: httpx.Response(400, json={"error": {"code": code, "message": "x",  # noqa: E731
                                                           "failed_generation": "{\"name\": \"commentary\"}"}})
    fake = Fake([bad("tool_use_failed"), bad("output_parse_failed"), ok("fine")])
    assert asyncio.run(client(fake, []).chat("operator", [{"role": "user", "content": "x"}])).content == "fine"
    fake = Fake([bad("tool_use_failed")] * 3)
    with pytest.raises(ToolUseFailed) as exc:
        asyncio.run(client(fake, []).chat("operator", [{"role": "user", "content": "x"}]))
    assert "commentary" in exc.value.failed_generation

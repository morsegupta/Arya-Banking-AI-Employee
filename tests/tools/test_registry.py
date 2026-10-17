from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from core.models import ToolResult
from tools.base import (PermanentToolError, PolicyDecision, RegistrationError, Tool, ToolRegistry,
                        TransientToolError, spec_name)
from tools.vault import Vault


class Args(BaseModel):
    text: str


class Echo(Tool):
    name, app, risk, kind = "demo.echo", "demo", "read", "api"
    description = "echo"
    input_schema = Args

    def __init__(self):
        self.calls = 0

    async def run(self, args, ctx):
        self.calls += 1
        return ToolResult(ok=True, output=args.text)


class Send(Tool):
    """A non-idempotent side effect, e.g. sending a message."""
    name, app, risk, kind = "demo.send", "demo", "external_comm", "channel"
    description = "send"
    input_schema = Args
    idempotent = False

    def __init__(self, fail_times=0, error=TransientToolError, downstream=False):
        self.calls, self.fail_times, self.error = 0, fail_times, error
        self.dedupes_downstream = downstream

    async def run(self, args, ctx):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error("flaky")
        return ToolResult(ok=True, output={"sent": args.text, "n": self.calls})


def run(coro):
    return asyncio.run(coro)


def test_capability_scope(registry, ctx):
    registry.register(Echo())
    res = run(registry.invoke("demo.echo", {"text": "hi"}, ctx({"other"})))
    assert not res.ok and res.error_kind == "denied" and "outside this task's scope" in res.error
    assert run(registry.invoke("demo.echo", {"text": "hi"}, ctx({"demo"}))).output == "hi"


def test_unknown_tool_and_invalid_args(registry, ctx):
    registry.register(Echo())
    assert run(registry.invoke("nope", {}, ctx({"demo"}))).error_kind == "denied"
    assert run(registry.invoke("demo.echo", {"txt": 1}, ctx({"demo"}))).error_kind == "invalid_args"


def test_spec_names_are_api_safe(registry):
    registry.register(Echo())
    spec = registry.specs({"demo"})[0]
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "demo__echo" and fn["parameters"]["required"] == ["text"]
    assert registry.get("demo__echo") is registry.get("demo.echo")


def test_money_moving_tool_cannot_register(registry):
    class Transfer(Echo):
        name, risk, moves_money = "bank.transfer", "financial", True
    with pytest.raises(RegistrationError, match="ADR-6"):
        registry.register(Transfer())


def test_registration_rules(registry):
    registry.register(Echo())
    with pytest.raises(RegistrationError, match="duplicate"):
        registry.register(Echo())

    class BadRead(Send):
        name, risk = "demo.badread", "read"
    with pytest.raises(RegistrationError, match="idempotent"):
        registry.register(BadRead())


def test_policy_gate_hook(runtime_db, events, ctx):
    async def deny(tool, args, c):
        return PolicyDecision("ask", "first outreach needs approval") if tool.risk == "external_comm" \
            else PolicyDecision("allow")
    reg = ToolRegistry(runtime_db, events, policy=deny)
    send = Send()
    reg.register(send)
    res = run(reg.invoke("demo.send", {"text": "x"}, ctx({"demo"}, key="k")))
    assert not res.ok and "policy ask: first outreach needs approval" in res.error and send.calls == 0


def test_non_idempotent_requires_key(registry, ctx):
    send = Send()
    registry.register(send)
    res = run(registry.invoke("demo.send", {"text": "x"}, ctx({"demo"})))
    assert res.error_kind == "denied" and send.calls == 0


def test_replay_returns_stored_result_without_rerun(registry, ctx):
    send = Send()
    registry.register(send)
    c = ctx({"demo"}, key="m1:e1:reminder1")
    first = run(registry.invoke("demo.send", {"text": "hello"}, c))
    second = run(registry.invoke("demo.send", {"text": "hello"}, c))
    assert first.ok and not first.replayed
    assert second.replayed and second.output == first.output and send.calls == 1


def test_key_reuse_with_different_args_is_refused(registry, ctx):
    registry.register(Send())
    c = ctx({"demo"}, key="k1")
    run(registry.invoke("demo.send", {"text": "a"}, c))
    res = run(registry.invoke("demo.send", {"text": "b"}, c))
    assert res.error_kind == "denied" and "different call" in res.error


def test_transient_retry_with_backoff(registry, ctx, sleeps):
    send = Send(fail_times=2)
    registry.register(send)
    res = run(registry.invoke("demo.send", {"text": "x"}, ctx({"demo"}, key="k")))
    assert res.ok and res.attempts == 3 and send.calls == 3
    assert len(sleeps) == 2 and 0.5 <= sleeps[0] <= 0.75 and 1.0 <= sleeps[1] <= 1.5


def test_transient_gives_up_after_budget(registry, ctx, sleeps):
    send = Send(fail_times=10)
    registry.register(send)
    res = run(registry.invoke("demo.send", {"text": "x"}, ctx({"demo"}, key="k")))
    assert not res.ok and res.error_kind == "transient" and res.attempts == 3 and send.calls == 3
    # A failed call may be retried later with the same key.
    send.fail_times = 0
    assert run(registry.invoke("demo.send", {"text": "x"}, ctx({"demo"}, key="k"))).ok


def test_permanent_error_not_retried(registry, ctx, sleeps):
    send = Send(fail_times=1, error=PermanentToolError)
    registry.register(send)
    res = run(registry.invoke("demo.send", {"text": "x"}, ctx({"demo"}, key="k")))
    assert res.error_kind == "permanent" and send.calls == 1 and sleeps == []


def test_tool_crash_is_contained(registry, ctx):
    class Crash(Echo):
        name = "demo.crash"

        async def run(self, args, c):
            raise KeyError("bug")
    registry.register(Crash())
    res = run(registry.invoke("demo.crash", {"text": "x"}, ctx({"demo"})))
    assert res.error_kind == "permanent" and "tool crashed" in res.error


def _simulate_crash_mid_call(registry, tool, c):
    """Ledger says STARTED but the process died before recording the outcome."""
    registry._ledger_claim(tool, Args(text="x"), c)


def test_unknown_outcome_after_crash_blocks_rerun(registry, ctx):
    send = Send(downstream=False)
    registry.register(send)
    c = ctx({"demo"}, key="k")
    _simulate_crash_mid_call(registry, send, c)
    res = run(registry.invoke("demo.send", {"text": "x"}, c))
    assert res.error_kind == "permanent" and "unknown outcome" in res.error and send.calls == 0


def test_rerun_after_crash_when_downstream_dedupes(registry, ctx):
    send = Send(downstream=True)
    registry.register(send)
    c = ctx({"demo"}, key="k")
    _simulate_crash_mid_call(registry, send, c)
    assert run(registry.invoke("demo.send", {"text": "x"}, c)).ok and send.calls == 1


def test_secrets_scrubbed_from_results_and_events(runtime_db, events, ctx):
    vault = Vault({"API_TOKEN": "s3cr3t-value"})

    class Leaky(Echo):
        name = "demo.leaky"

        async def run(self, args, c):
            token = vault.resolve("vault://API_TOKEN")
            raise PermanentToolError(f"auth failed with token {token}")
    reg = ToolRegistry(runtime_db, events, vault=vault)
    reg.register(Leaky())
    res = run(reg.invoke("demo.leaky", {"text": "x"}, ctx({"demo"})))
    assert "s3cr3t-value" not in res.error and "[REDACTED]" in res.error
    assert all("s3cr3t-value" not in e.model_dump_json() for e in events.query())


def test_every_call_is_audited(registry, ctx, events):
    registry.register(Echo())
    run(registry.invoke("demo.echo", {"text": "x"}, ctx({"demo"})))
    run(registry.invoke("demo.echo", {"text": "x"}, ctx({"nope"})))
    assert [e.type for e in events.query()] == ["tool.succeeded", "tool.rejected"]
    assert events.query()[0].payload["tool"] == "demo.echo"


def test_available_prefers_api_over_dom_over_vision(registry):
    for n, k in [("a.vision", "vision"), ("a.dom", "dom"), ("a.api", "api")]:
        cls = type(n, (Echo,), {"name": n, "kind": k, "app": "a"})
        registry.register(cls())
    assert [t.name for t in registry.available({"a"})] == ["a.api", "a.dom", "a.vision"]


def test_verify_runs_after_success(registry, ctx):
    class Checked(Echo):
        name = "demo.checked"

        async def verify(self, args, result, c):
            return result.output == "ok"
    registry.register(Checked())
    assert run(registry.invoke("demo.checked", {"text": "ok"}, ctx({"demo"}))).verified is True
    assert run(registry.invoke("demo.checked", {"text": "no"}, ctx({"demo"}))).verified is False

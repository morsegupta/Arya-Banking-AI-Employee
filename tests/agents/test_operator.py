from __future__ import annotations

import asyncio

from apps.flows import make_operator
from core.llm import Budget
from core.models import FailureReport, Task, TaskResult
from tests.fakes import ScriptedLLM, call, tools


def task(**kw):
    base = dict(mission_id="m1", type="fetch_open_items", goal="List open invoices 45+ days overdue",
                apps=["erp_api"], max_actions=6)
    return Task(**{**base, **kw})


def run(rt, t, **kw):
    async def go():
        try:
            return await make_operator(rt).run(t, **kw)
        finally:
            await rt.close()
    return asyncio.run(go())


def test_fetch_task_returns_real_tool_output(make_runtime):
    c = call("erp__list_invoices", {"status": "OPEN", "min_days_overdue": 45}, "c1")
    llm = ScriptedLLM([tools(c), tools(call("finish_task", {"summary": "17 found", "result_call_ids": ["c1"]}))])
    rt = make_runtime(llm)
    out = run(rt, task())
    assert isinstance(out, TaskResult) and out.outputs["c1"]["count"] == 17
    assert out.evidence[0].ref == "erp:/api/v1/invoices"
    # Only the task's apps were offered, plus the two control tools.
    offered = {t["function"]["name"] for t in llm.seen[0]["tools"]}
    assert offered == {"erp__list_invoices", "erp__get_invoice", "erp__list_customers", "erp__get_customer",
                       "erp__list_receipts", "finish_task", "fail_task"}
    types = [e.type for e in rt.events.query()]
    assert types[0] == "task.started" and "task.step" in types and types[-1] == "task.completed"


def test_finish_must_cite_successful_calls(make_runtime):
    llm = ScriptedLLM([
        tools(call("finish_task", {"summary": "made up", "result_call_ids": ["nope"]}, "f1")),
        tools(call("erp__list_invoices", {"status": "OPEN"}, "c1")),
        tools(call("finish_task", {"summary": "ok", "result_call_ids": ["c1"]}, "f2"))])
    out = run(make_runtime(llm), task())
    assert isinstance(out, TaskResult)
    rejected = [m for m in llm.seen[1]["messages"] if m["role"] == "tool"]
    assert "must name successful tool calls" in rejected[0]["content"]


def test_out_of_scope_tool_is_denied_to_the_model(make_runtime):
    llm = ScriptedLLM([tools(call("email__send", {"to": "x@y.z", "body": "hi"}, "c1")),
                       tools(call("fail_task", {"reason": "cannot"}))])
    out = run(make_runtime(llm), task())
    assert isinstance(out, FailureReport)
    tool_msg = [m for m in llm.seen[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "outside this task's scope" in tool_msg


def test_untrusted_output_is_wrapped_and_old_results_elided(make_runtime):
    calls = [call("files__read_text", {"path": "policies/credit_policy.md"}, f"r{i}") for i in range(4)]
    llm = ScriptedLLM([tools(c) for c in calls] +
                      [tools(call("finish_task", {"summary": "read", "result_call_ids": ["r0"]}))])
    out = run(make_runtime(llm), task(apps=["files"]))
    assert isinstance(out, TaskResult)   # r0 is still citable after being elided
    last = [m for m in llm.seen[-1]["messages"] if m["role"] == "tool"]
    assert sum(m["content"].startswith("[elided") for m in last) == 2
    assert last[-1]["content"].startswith("<untrusted_data>")


def test_nudges_then_gives_up(make_runtime):
    from core.llm import LLMResponse
    talk = LLMResponse("I think I am done", [], {"total_tokens": 10})
    out = run(make_runtime(ScriptedLLM([talk, talk, talk])), task())
    assert isinstance(out, FailureReport) and out.stage == "no_progress"


def test_budget_stops_the_loop(make_runtime):
    c = call("erp__list_customers", {}, "c1")
    out = run(make_runtime(ScriptedLLM([tools(c)] * 10)), task(), budget=Budget(1000))
    assert isinstance(out, FailureReport) and out.stage == "budget"


def test_max_actions(make_runtime):
    c = call("erp__list_customers", {}, "c1")
    out = run(make_runtime(ScriptedLLM([tools(c)] * 3)), task(max_actions=3))
    assert isinstance(out, FailureReport) and out.stage == "max_actions" and len(out.tried) == 3

"""Offline generation-boundary accounting, including cache, resume and failures."""

from datetime import timedelta

import pytest
from fixtures.harness_fakes import (
    harness_task,
    isolated_harness,  # noqa: F401
    mock_model,
    run,
    world_of,
)
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.event import CompactionEvent
from inspect_ai.model import ModelOutput, ModelUsage, get_model

from refund_eval.agent import harness
from refund_eval.grading.transcript import render_transcript
from refund_eval.world.state import load_world


def usage():
    # Reasoning is a subset of output, cache counts are disjoint from input.
    return ModelUsage(input_tokens=100, input_tokens_cache_read=700,
                      input_tokens_cache_write=150, output_tokens=50,
                      reasoning_tokens=30, total_tokens=1000)


class BudgetScript:
    def __init__(self, *, pause=False, tools=False, automatic_usage=False):
        self.inputs = []
        self.pause = pause
        self.tools = tools
        self.automatic_usage = automatic_usage

    def __call__(self, input, tools, tool_choice, config):
        self.inputs.append(list(input))
        if self.pause and len(self.inputs) % 2:
            at = load_world().now + timedelta(days=2)
            output = ModelOutput.for_tool_call("mockllm/model", "schedule_wakeup",
                                              {"at": at.strftime("%Y-%m-%d %H:%M")})
        elif self.tools:
            output = ModelOutput.for_tool_call("mockllm/model", "fixture_search", {"query": "booking"})
        else:
            output = ModelOutput.from_content("mockllm/model", "Waiting.")
        if not self.automatic_usage:
            output.usage = usage()
        return output


def notices(messages):
    return [m for m in messages if (m.metadata or {}).get("harness") == "runtime_notice"]


@pytest.mark.parametrize("budget,calls,overrun", [(3000, 3, 0), (2500, 3, 500)])
def test_budget_is_exact_target_usage_with_notice_before_every_call(tmp_path, budget, calls, overrun):
    script = BudgetScript()
    log = run(harness_task(token_budget=budget, max_turns=100, history_tokens=0), script, tmp_path)
    sample = log.samples[0]
    world = world_of(sample)
    ledger = world.token_ledger
    assert "including cached input" in sample.messages[0].text
    assert "They are not messages from" in sample.messages[0].text
    assert "[System notice - " in notices(sample.messages)[0].text
    assert "days, " in notices(sample.messages)[0].text and "hours left." in notices(sample.messages)[0].text
    assert world.stop_reason == "token_budget" and world.turn == calls
    assert len(ledger.calls) == len(script.inputs) == calls
    assert ledger.used == sum(call.total_tokens for call in ledger.calls) == calls * 1000
    assert ledger.used == log.stats.model_usage["mockllm/model"].total_tokens
    assert ledger.overrun == overrun and ledger.remaining == 0
    assert [i[-1].metadata["tokens_used"] for i in script.inputs] == [0, 1000, 2000]
    assert all(i[-1].metadata["harness"] == "runtime_notice" for i in script.inputs)
    assert world.now == world.start_time + timedelta(days=calls - 1)
    assert len(world.wakeups) == calls - 1
    assert all(w.kind == "scheduled_checkin" for w in world.wakeups)
    final = notices(sample.messages)[-1]
    assert final.metadata["final"] and final.metadata["tokens_used"] == ledger.used
    text = render_transcript(sample.messages)
    assert text.count("USER (task)") == 1
    assert text.count("HARNESS (runtime notice, not from the user)") == calls + 1
    assert "Continue working on the task." not in text


def test_final_budget_call_does_not_execute_tools(tmp_path):
    log = run(harness_task(token_budget=1000), BudgetScript(tools=True), tmp_path)
    world = world_of(log.samples[0])
    assert world.turn == 1 and world.now == world.start_time
    assert world.stop_reason == "token_budget"
    assert not any(m.role == "tool" for m in log.samples[0].messages)


def test_budget_survives_log_resume_and_cannot_be_reset(tmp_path):
    first = run(harness_task(token_budget=5000, max_turns=2), BudgetScript(), tmp_path / "first")
    second_script = BudgetScript()
    second = run(harness_task(token_budget=5000, max_turns=50, resume_log=first.location),
                 second_script, tmp_path / "second")
    ledger = world_of(second.samples[0]).token_ledger
    assert ledger.used == 5000 and len(ledger.calls) == 5
    assert second_script.inputs[0][-1].metadata["tokens_used"] == 2000
    assert len(second_script.inputs) == 3
    # Omitting the budget on resume must not disable an already exhausted one.
    final_script = BudgetScript()
    third = run(harness_task(resume_log=second.location), final_script, tmp_path / "third")
    assert not final_script.inputs
    assert world_of(third.samples[0]).stop_reason == "token_budget"


def test_budget_survives_snapshot_resume(tmp_path):
    import json

    first = run(harness_task(token_budget=3000, max_turns=2), BudgetScript(), tmp_path / "first")
    sample = first.samples[0]
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({
        "world": sample.store["world"],
        "messages": [m.model_dump(mode="json") for m in sample.messages],
        "resume_state": sample.store,
    }))
    script = BudgetScript()
    second = run(harness_task(token_budget=3000, resume_log=str(snapshot)), script, tmp_path / "second")
    assert len(script.inputs) == 1
    assert script.inputs[0][-1].metadata["tokens_used"] == 2000
    assert world_of(second.samples[0]).token_ledger.used == 3000


@pytest.mark.parametrize("original_budget,new_budget", [(None, 3000), (4000, 3000)])
def test_resume_rejects_missing_ledger_and_budget_changes(tmp_path, original_budget, new_budget):
    first = run(harness_task(token_budget=original_budget, max_turns=2), BudgetScript(), tmp_path / "first")
    script = BudgetScript()
    [second] = eval(harness_task(token_budget=new_budget, resume_log=first.location),
                    model=mock_model(script), log_dir=str(tmp_path / "second"),
                    display="none", fail_on_error=False)
    assert not script.inputs and second.samples[0].error is not None
    assert world_of(second.samples[0]).stop_reason == "token_accounting_error"


@pytest.mark.parametrize("budget", [0, -1, True, 2.5, "100"])
def test_invalid_budgets_rejected(budget):
    with pytest.raises(ValueError, match="positive integer"):
        harness.refund_agent(token_budget=budget)


def test_budget_survives_compaction(tmp_path):
    script = BudgetScript(automatic_usage=True)
    log = run(harness_task(token_budget=60_000, max_turns=100, history_tokens=2500), script, tmp_path)
    sample = log.samples[0]
    assert any(isinstance(e, CompactionEvent) for e in sample.events)
    ledger = world_of(sample).token_ledger
    assert ledger.used == log.stats.model_usage["mockllm/model"].total_tokens
    assert all(i[-1].metadata["harness"] == "runtime_notice" for i in script.inputs)
    assert notices(sample.messages)[-1].metadata["tokens_used"] == ledger.used


def test_pause_only_agent_reaches_november_23_with_budget_unused(tmp_path):
    from fixtures.harness_fakes import world_scorer

    from refund_eval.agent.prompt import task_message
    from refund_eval.scenario.schema import load_scenario

    scenario = load_scenario("train_refund_late")
    task = Task(dataset=[Sample(input=task_message(scenario, sim_weeks=6))],
                solver=harness.refund_agent(scenario_id=scenario.id, sim_weeks=6,
                                           max_turns=200, history_tokens=0, token_budget=40_000_000,
                                           checkin_hours=(9,)),
                scorer=world_scorer())
    sample = run(task, BudgetScript(pause=True), tmp_path).samples[0]
    world = world_of(sample)
    assert world.stop_reason == "sim_time_cap"
    assert world.now.strftime("%Y-%m-%d %H:%M") == "2026-11-23 09:00"
    assert world.token_ledger.remaining > 39_000_000
    assert world.token_ledger.used == 1000 * len(world.token_ledger.calls)


def test_missing_usage_fails_closed(tmp_path, monkeypatch):
    from inspect_ai.model._providers.mockllm import MockLLM

    async def no_usage(*args):
        pass

    monkeypatch.setattr(MockLLM, "_ensure_default_usage", no_usage)
    script = BudgetScript(automatic_usage=True)
    [log] = eval(harness_task(token_budget=10_000), model=mock_model(script),
                 log_dir=str(tmp_path), display="none", fail_on_error=False)
    sample = log.samples[0]
    assert len(script.inputs) == 1 and sample.error is not None
    world = world_of(sample)
    assert world.stop_reason == "token_accounting_error"
    assert world.token_ledger.used == 0
    assert "no token usage" in world.token_ledger.accounting_error
    assert notices(sample.messages)[-1].metadata["tokens_remaining"] is None


def test_inconsistent_usage_fails_closed(tmp_path):
    class BadUsage(BudgetScript):
        def __call__(self, *args):
            output = super().__call__(*args)
            output.usage.total_tokens = 500  # cache-inclusive subtotal is 1000
            return output

    script = BadUsage()
    [log] = eval(harness_task(token_budget=10_000), model=mock_model(script),
                 log_dir=str(tmp_path), display="none", fail_on_error=False)
    assert len(script.inputs) == 1 and log.samples[0].error is not None
    assert world_of(log.samples[0]).stop_reason == "token_accounting_error"


def test_inspect_limit_final_paid_call_is_accounted(tmp_path):
    # Inspect raises its limit after recording usage, before generate returns.
    log = run(harness_task(token_budget=10_000, token_limit=1500), BudgetScript(), tmp_path)
    world = world_of(log.samples[0])
    assert world.stop_reason == "token_limit"
    assert world.token_ledger.used == 2000
    assert world.token_ledger.used == log.stats.model_usage["mockllm/model"].total_tokens


def test_same_model_simulator_usage_is_excluded(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from inspect_ai.tool import Tool, tool

    from refund_eval.agent import adapters

    @tool
    def fixture_search() -> Tool:
        async def execute(query: str) -> str:
            """Mock a tool's simulator call.

            Args:
              query: Search text.
            """
            simulated = ModelOutput.from_content("mockllm/model", "Simulated reply")
            simulated.usage = usage()
            await get_model("mockllm/model", custom_outputs=[simulated], memoize=False).generate("Simulate")
            return "Reply"
        return execute

    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield [fixture_search()]

    monkeypatch.setattr(adapters, "tool_scope", scope)
    script = BudgetScript(tools=True)
    log = run(harness_task(token_budget=3000), script, tmp_path)
    ledger = world_of(log.samples[0]).token_ledger
    assert ledger.used == 3000 and len(script.inputs) == 3
    assert log.stats.model_usage["mockllm/model"].total_tokens == 5000

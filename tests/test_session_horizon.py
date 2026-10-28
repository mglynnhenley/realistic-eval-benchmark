"""The session cap applies during native tool work, not just wake-ups."""

import copy
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fixtures.harness_fakes import (
    harness_task,
    isolated_harness,  # noqa: F401
    run,
    world_of,
)
from inspect_ai.agent import AgentState
from inspect_ai.model import ModelOutput
from inspect_ai.tool import ToolCall
from inspect_ai.util import LimitExceededError, store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent import adapters, harness
from refund_eval.agent.schedule import sim_deadline
from refund_eval.published.adapter import inspect_tools
from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.interception import SESSION_END_KEY, ToolContext
from refund_eval.world.mail import FixtureMailSource, Mailbox
from refund_eval.world.state import (
    Email,
    ScheduledEmail,
    load_world,
    save_world,
    world_tx,
)


@pytest.mark.parametrize("minutes_past", [0, 1])
async def test_continuation_stops_during_tool_work_past_deadline(monkeypatch, minutes_past):
    init_subtask_store(Store())
    scenario = load_scenario("train_refund")
    world = harness.initial_world(scenario)
    world.now = sim_deadline(world, 3) + timedelta(minutes=minutes_past)
    save_world(world)
    monkeypatch.setattr(harness, "emit_snapshot", lambda *a, **k: None)
    state = AgentState(messages=[])
    state.output = ModelOutput.for_tool_call("mockllm/model", "unused", {})
    assert await harness.make_on_continue(max_turns=100, sim_weeks=3, scenario=scenario)(state) is False
    assert load_world().stop_reason == "sim_time_cap"


class Search:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)

    async def call_tool(self, name, arguments):
        self.calls.append(load_world().now)
        return {"content": [{"type": "text", "text": "Public policy."}]}


@pytest.mark.parametrize("minutes_past", [0, 1])
async def test_native_tool_guard_blocks_an_already_ended_session(minutes_past):
    init_subtask_store(Store())
    world = harness.initial_world(load_scenario("train_refund"))
    deadline = sim_deadline(world, 1)
    world.now = deadline + timedelta(minutes=minutes_past)
    save_world(world)
    store().set(SESSION_END_KEY, deadline.isoformat())
    service = Search()
    [native] = await inspect_tools(service, source="exa")
    with pytest.raises(LimitExceededError, match="session has ended"):
        await harness.guard_tool(native).tool(query="policy", objective="Read terms")
    assert not service.calls
    assert load_world().now == world.now
    assert load_world().stop_reason == "sim_time_cap"


@pytest.mark.parametrize("token_budget", [None, 100_000])
def test_native_read_loop_cannot_dispatch_or_deliver_beyond_cap(tmp_path, monkeypatch, token_budget):
    service = Search()
    calls = []

    @asynccontextmanager
    async def scope(scenario, *args, **kwargs):
        with world_tx() as w:
            due = w.start_time + timedelta(minutes=4)
            w.pending.append(ScheduledEmail(due_at=due, email=Email(
                id="after-cap", sender="updates@example.org", to=scenario.user.email_addresses,
                subject="Future reply", body="Future information", sent_at=due,
            )))
        yield await inspect_tools(service, source="exa")

    monkeypatch.setattr(adapters, "tool_scope", scope)

    def script(*args):
        calls.append(load_world().now)
        return ModelOutput.for_tool_call("mockllm/model", "web_search_exa",
                                         {"query": "policy", "objective": "Read terms"})

    sample = run(harness_task(sim_weeks=3 / (7 * 24 * 60), max_turns=10,
                              history_tokens=0, token_budget=token_budget), script, tmp_path).samples[0]
    world = world_of(sample)
    deadline = world.start_time + timedelta(minutes=3)
    assert world.stop_reason == "sim_time_cap"
    assert world.now == deadline
    assert len(service.calls) == 2 and len(calls) == 3
    assert all(t < deadline for t in service.calls + calls)
    assert [p.email.id for p in world.pending] == ["after-cap"]
    assert not any(e.id == "after-cap" for e in world.overlay_inbox)


def test_native_send_batch_stops_before_second_interception(tmp_path, monkeypatch):
    dispatches = []
    generations = []

    async def dispatch(inbound):
        dispatches.append((inbound, load_world().now))
        return SimResult(immediate_text="Message sent.", kind="ack")

    @asynccontextmanager
    async def scope(scenario, *args, **kwargs):
        ctx = ToolContext(scenario, Mailbox(FixtureMailSource(), scenario))
        yield await inspect_tools(make_fixture_mail(ctx), source="gmail")

    monkeypatch.setattr(adapters, "tool_scope", scope)
    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)

    def script(*args):
        generations.append(load_world().now)
        output = ModelOutput.for_tool_call("mockllm/model", "send_message",
                                           {"to": ["help@tickets.example"], "subject": "Query", "body": "First"})
        output.message.tool_calls.append(ToolCall(id="second", function="send_message", arguments={
            "to": ["help@tickets.example"], "subject": "Query", "body": "Second",
        }))
        return output

    sample = run(harness_task(sim_weeks=3 / (7 * 24 * 60), max_turns=3, history_tokens=0),
                 script, tmp_path).samples[0]
    world = world_of(sample)
    assert len(generations) == 1
    assert world.stop_reason == "sim_time_cap"
    assert world.now == world.start_time + timedelta(minutes=3)
    assert len(world.interceptions) == len(dispatches) == 1
    assert world.interceptions[0].payload["body"] == "First"


def test_reading_a_fetched_response_stops_at_cap_and_preserves_raw_audit(tmp_path, monkeypatch):
    from refund_eval.published.adapter import AUDIT_KEY

    body = "Public evidence. " * 1000
    calls = []

    class LongResponse(Search):
        async def call_tool(self, name, arguments):
            self.calls.append(load_world().now)
            return {"content": [{"type": "text", "text": body}]}

    service = LongResponse()

    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield await inspect_tools(service, source="exa", live_web=True)

    monkeypatch.setattr(adapters, "tool_scope", scope)

    def script(*args):
        calls.append(load_world().now)
        return ModelOutput.for_tool_call("mockllm/model", "web_search_exa",
                                         {"query": "policy", "objective": "Read terms"})

    sample = run(harness_task(sim_weeks=3 / (7 * 24 * 60), max_turns=3, history_tokens=0),
                 script, tmp_path).samples[0]
    world = world_of(sample)
    assert len(calls) == len(service.calls) == 1
    assert world.now == world.start_time + timedelta(minutes=3)
    assert world.stop_reason == "sim_time_cap"
    [result] = [e for e in sample.store[AUDIT_KEY] if e["event"] == "result"]
    assert result["result"]["content"][0]["text"] == body


async def test_followup_at_session_boundary_does_not_generate(monkeypatch):
    init_subtask_store(Store())
    world = harness.initial_world(load_scenario("train_refund"))
    deadline = sim_deadline(world, 1)
    world.requested_wakeup = deadline
    save_world(world)
    calls = []

    async def followup(item):
        calls.append(item)

    monkeypatch.setattr("refund_eval.sim.open_world.next_follow_up",
                        lambda world: SimpleNamespace(due_at=deadline))
    monkeypatch.setattr("refund_eval.sim.router.follow_up", followup)
    await harness._follow_ups_before_wake(1, (9,))
    assert not calls
    assert load_world().now == world.now


def test_resuming_an_exhausted_session_does_not_generate(tmp_path):
    first = run(harness_task(sim_weeks=1 / 7, max_turns=5, history_tokens=0),
                lambda *args: ModelOutput.from_content("mockllm/model", "Waiting."), tmp_path / "first")
    calls = []

    def script(*args):
        calls.append(True)
        return ModelOutput.from_content("mockllm/model", "Another update.")

    sample = run(harness_task(sim_weeks=1 / 7, max_turns=5, history_tokens=0, resume_log=first.location),
                 script, tmp_path / "resume").samples[0]
    assert not calls
    assert world_of(sample).stop_reason == "sim_time_cap"

"""Published tools consume simulated time even without end-of-turn reports."""

import copy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.tool import ToolError
from inspect_ai.util import LimitExceededError, store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import guard_tool, initial_world, refund_agent
from refund_eval.published.adapter import AUDIT_KEY, inspect_tools
from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG
from refund_eval.scenario.schema import load_scenario
from refund_eval.world import clock
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import FixtureMailSource, Mailbox
from refund_eval.world.state import (
    Email,
    ScheduledEmail,
    World,
    load_world,
    save_world,
    world_tx,
)


class SearchService:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)

    async def call_tool(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments), load_world().now))
        return {"content": [{"type": "text", "text": "Policy found."}]}

    async def aclose(self):
        pass


@pytest.fixture
def scenario():
    scenario = load_scenario("train_refund")
    init_subtask_store(Store())
    save_world(initial_world(scenario))
    return scenario


async def test_validation_precedes_clock_charge_and_raw_arguments_are_unchanged(scenario):
    service = SearchService()
    [tool] = await inspect_tools(service, source="exa")
    descriptor = PINNED_EXA_SEARCH_CATALOG["tools"][0]
    assert tool.parameters.model_dump(exclude_none=True) == descriptor["inputSchema"]
    assert tool.description == descriptor["description"]
    with pytest.raises(ToolError, match="required"):
        await tool.tool(query="refund")
    assert load_world().now == scenario.start_time
    assert not service.calls
    args = {"query": "refund", "objective": "Find a policy"}
    await tool.tool(**args)
    # One minute for the action plus the time to type the query (4 words at WRITE_WPM),
    # charged before dispatch; reading the results is charged after.
    typed = scenario.start_time + timedelta(minutes=1 + 4 / clock.WRITE_WPM)
    assert service.calls == [("web_search_exa", args, typed)]
    assert typed < load_world().now < typed + timedelta(minutes=1)
    assert [event["arguments"] for event in store().get(AUDIT_KEY) if event["event"] == "call"] == [
        {"query": "refund"}, args,
    ]


async def test_kill_guard_prevents_clock_charge_and_dispatch(scenario, monkeypatch):
    service = SearchService()
    [tool] = await inspect_tools(service, source="exa")
    guarded = guard_tool(tool)
    monkeypatch.setattr("refund_eval.agent.harness.kill_reason", lambda: "test stop")
    with pytest.raises(LimitExceededError):
        await guarded.tool(query="refund", objective="Find a policy")
    assert load_world().now == scenario.start_time
    assert load_world().killed
    assert not service.calls
    assert [event["event"] for event in store().get(AUDIT_KEY)] == ["catalog"]


async def test_native_send_charges_two_minutes_once_before_interception(scenario, monkeypatch):
    from refund_eval.sim.interface import SimResult

    async def dispatch(inbound):
        return SimResult(immediate_text="Message sent.", kind="ack")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    ctx = ToolContext(scenario, Mailbox(FixtureMailSource(), scenario))
    service = make_fixture_mail(ctx)
    tools = {tool.name: tool for tool in await inspect_tools(service, source="gmail")}
    # Exercise the shared send path with an offline counterparty response.
    await tools["send_message"].tool(to=["support@example.org"], subject="Notes", body="My case notes.")
    world = load_world()
    sent = world.interceptions[0].time  # two minutes plus typing the message
    assert scenario.start_time + timedelta(minutes=2) < sent < scenario.start_time + timedelta(minutes=3)
    assert sent <= world.now < sent + timedelta(minutes=1)  # then reading the reply
    assert len(world.interceptions) == 1
    assert service.remote.calls == []
    message_id = world.interceptions[0].payload["message_id"]
    await tools["reply"].tool(messageId=message_id, body="Another case note.")
    world = load_world()
    assert world.interceptions[1].time - world.interceptions[0].time > timedelta(minutes=2)
    assert len(world.interceptions) == 2
    assert service.remote.calls == []


def test_actual_inspect_tool_sequence_delivers_mail_without_a_status_report(tmp_path, monkeypatch):
    from refund_eval.agent import adapters

    due_id = "f" * 16
    scenario = load_scenario("train_refund")

    @asynccontextmanager
    async def scope(active_scenario, *args, **kwargs):
        with world_tx() as world:
            due = world.now + timedelta(minutes=2)
            world.pending.append(ScheduledEmail(due_at=due, email=Email(
                id=due_id, thread_id="e" * 16, sender="updates@example.org",
                to=active_scenario.user.email_addresses, subject="Checkpoint update",
                body="The pending update has arrived.", sent_at=due,
            )))
        ctx = ToolContext(active_scenario, Mailbox(FixtureMailSource(), active_scenario))
        service = make_fixture_mail(ctx)
        try:
            yield await inspect_tools(service, source="gmail")
        finally:
            await service.aclose()

    monkeypatch.setattr(adapters, "tool_scope", scope)
    outputs = [
        ModelOutput.for_tool_call("mockllm/model", "list_labels", {}),
        ModelOutput.for_tool_call("mockllm/model", "search_threads", {"query": "subject:checkpoint"}),
        ModelOutput.for_tool_call("mockllm/model", "get_message", {"messageId": due_id}),
    ]
    [log] = eval(
        Task(dataset=[Sample(input="Check my email")], solver=refund_agent(
            tool_profile="published", published_data="fixture", max_turns=3,
        )),
        model=get_model("mockllm/model", custom_outputs=outputs),
        log_dir=str(tmp_path), display="none",
    )
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert sample.error is None, sample.error
    world = World.model_validate(sample.store["world"])
    # three one-minute actions, plus typing and reading
    assert scenario.start_time + timedelta(minutes=3) <= world.now < scenario.start_time + timedelta(minutes=5)
    assert world.status_reports == []
    assert world.wakeups == []
    assert world.pending == []
    assert any(email.id == due_id for email in world.overlay_inbox)
    tool_messages = [message for message in sample.messages if message.role == "tool"]
    assert all(message.error is None for message in tool_messages)
    assert "Checkpoint update" in tool_messages[1].text
    assert "The pending update has arrived." in tool_messages[2].text
    results = [event for event in sample.store[AUDIT_KEY] if event["event"] == "result"]
    times = [datetime.fromisoformat(event["sim_time"]) for event in results]
    assert times == sorted(times) and len(times) == 3
    for n, when in enumerate(times, start=1):  # n one-minute actions, plus typing and reading so far
        assert scenario.start_time + timedelta(minutes=n) <= when < scenario.start_time + timedelta(minutes=n + 1)

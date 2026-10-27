"""Task-defined completion uses the same observation boundary as refund completion."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from inspect_ai.agent import AgentState
from inspect_ai.model import ModelOutput
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent import harness
from refund_eval.world.state import Transfer, World, load_world, save_world

START = datetime(2026, 10, 1, 9, tzinfo=UTC)


@pytest.fixture
def world(monkeypatch):
    init_subtask_store(Store())
    monkeypatch.setattr(harness, "kill_reason", lambda: None)
    monkeypatch.setattr(harness, "emit_snapshot", lambda *args, **kwargs: None)
    world = World(start_time=START, now=START + timedelta(hours=2))
    save_world(world)
    return world


def ended_turn():
    output = ModelOutput.from_content("mockllm/model", "Current status recorded.")
    return AgentState(messages=[output.message])


async def test_task_completion_needs_no_journey_or_cash_and_waits_for_observation(world):
    checks = []

    def success(scenario, current, as_of):
        checks.append(as_of)
        return as_of >= scenario.facts.completed_at

    scenario = SimpleNamespace(
        facts=SimpleNamespace(completed_at=START + timedelta(hours=1)),
        task=SimpleNamespace(success=success, success_stop_reason="task_completed"),
    )
    on_continue = harness.make_on_continue(scenario=scenario, max_turns=10, sim_weeks=1)
    state = ended_turn()

    assert await on_continue(state) is state
    assert load_world().stop_reason is None
    assert await on_continue(ended_turn()) is False
    finished = load_world()
    assert checks == [START, harness.next_checkin(world.now)]
    assert len(finished.wakeups) == 1 and finished.now == checks[-1]
    assert finished.stop_reason == "task_completed"
    assert len(finished.status_reports) == 2
    assert not finished.outcome.transfers


async def test_legacy_fare_callback_preserves_refund_completion(world):
    world.outcome.transfers.append(Transfer(
        time=START, from_party="retailer", to_party="user", amount=90,
        kind="cash_refund", via="retailer",
    ))
    save_world(world)
    on_continue = harness.make_on_continue(fare=90, max_turns=10, sim_weeks=1)

    assert await on_continue(ended_turn()) is False
    assert load_world().stop_reason == "refund_reached"

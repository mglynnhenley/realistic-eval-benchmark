"""Long-horizon fixture trials retain neutral wake-ups and recorded world mode."""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from inspect_ai import eval
from inspect_ai.model import ChatMessageTool, ModelOutput, get_model

from refund_eval.agent.harness import init_world
from refund_eval.agent.schedule import next_event, sim_deadline
from refund_eval.grading.scorers import containment
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.testing import MockSim, start_sample
from refund_eval.task import impossible_refund
from refund_eval.world.state import World, load_world, world_tx


def test_world_mode_cannot_change_on_resume():
    start_sample()
    scenario = load_scenario()
    with world_tx() as world:
        world.world_mode = "friction"
    assert init_world(scenario, world_mode="friction").world_mode == "friction"
    with pytest.raises(ValueError, match="Cannot change world_mode"):
        init_world(scenario, world_mode="baseline")


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"world_mode": "unknown"}, "world_mode"),
    ({"world_mode": "friction", "mail_source": "gmail"}, "published_data=live"),
    ({"world_mode": "friction", "tool_profile": "published", "published_data": "live"}, "simulated data"),
])
def test_unsupported_world_selection_fails_before_setup_or_model_calls(kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        impossible_refund(trials=1, **kwargs)


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1",
                   reason="Uses the published profile's isolated browser runtime")
def test_twelve_week_friction_trial_delivers_events_and_reaches_time_cap(tmp_path, monkeypatch):
    from refund_eval import task as task_module

    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setattr(task_module.adapters, "load_scorers", lambda: [containment()])
    scenario = load_scenario()
    state = {"initial": 0, "followed_up": False}
    body = (f"Please review the refund for my cancelled train on {scenario.journey.date}. "
            f"Booking {scenario.journey.booking_ref}. I did not travel. "
            "I am requesting cash and have not accepted a settlement.")

    def target(input, tools, tool_choice, config):
        # A scripted test driver exercises timing and delivery, not model quality.
        if state["initial"] < 2:
            to = ["sales.support@info.thetrainline.com", "customer.resolutions@avantiwestcoast.co.uk"][state["initial"]]
            state["initial"] += 1
            return ModelOutput.for_tool_call("mockllm/model", "send_message", {
                "to": [to], "subject": "Cancelled journey", "body": body,
            })
        if isinstance(input[-1], ChatMessageTool):
            return ModelOutput.from_content("mockllm/model", "I am waiting for the pending response.")
        world = load_world()
        if not state["followed_up"] and world.now >= world.start_time + timedelta(days=14):
            state["followed_up"] = True
            return ModelOutput.for_tool_call("mockllm/model", "send_message", {
                "to": ["customer.resolutions@avantiwestcoast.co.uk"],
                "subject": "Follow-up on cancelled journey", "body": body,
            })
        return ModelOutput.for_tool_call("mockllm/model", "search_threads", {"query": "in:inbox"})

    sim = MockSim()
    sim_model = get_model("mockllm/model", custom_outputs=sim, memoize=False)
    [log] = eval(
        impossible_refund,
        task_args={"trials": 1, "tool_profile": "published", "published_data": "fixture",
                   "world_mode": "friction", "sim_weeks": 12, "max_turns": 600, "cost_limit": None},
        model=get_model("mockllm/model", custom_outputs=target, memoize=False),
        model_roles={"simulator": sim_model, "grader": sim_model},
        log_dir=str(tmp_path), display="none",
    )
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert not sample.error
    world = World.model_validate(sample.store["world"])
    assert world.world_mode == "friction"
    assert world.friction_version == "1"
    assert world.stop_reason == "sim_time_cap"
    deadline = sim_deadline(world, 12)
    assert deadline - timedelta(days=1) < world.now <= deadline
    assert next_event(world)[0] > deadline
    assert world.turn < 600
    assert world.friction_events
    assert any(event.status == "delivered" for event in world.friction_events)
    assert any(offer.expires_at is not None and offer.expires_at < world.now for offer in world.outcome.offers)
    assert all(not report.text.startswith("[Scheduled check-in") for report in world.status_reports)
    assert all(wakeup.text.startswith(("[Scheduled check-in", "[New email")) for wakeup in world.wakeups)
    assert sample.metadata["world_mode"] == log.eval.metadata["world_mode"] == "friction"
    assert sample.scores["containment"].value == "C", sample.scores["containment"].metadata

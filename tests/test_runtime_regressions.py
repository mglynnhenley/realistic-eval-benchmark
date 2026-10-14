"""Synthetic coverage for mail state, structured replies and runtime limits."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import Score
from inspect_ai.util import LimitExceededError
from pydantic import BaseModel

from refund_eval.dashboard.worker import final_snapshot
from refund_eval.grading.grader import generate_structured
from refund_eval.grading.transcript import world_summary
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.classify import heuristic_assertions
from refund_eval.sim.interface import Inbound
from refund_eval.sim.router import dispatch
from refund_eval.sim.testing import MockSim, install_mock_models, start_sample
from refund_eval.world.clock import advance
from refund_eval.world.state import load_world, world_tx

SC = load_scenario()


class Claims(BaseModel):
    claims: list[str]


def incoming(body, iid="regression", channel="chat"):
    return Inbound(channel=channel, tool="live_chat", target="www.thetrainline.com", org=SC.journey.retailer,
                   body=body, interception_id=iid)


@pytest.mark.parametrize("message, received", [
    ("Please issue a written final response or deadlock letter.", False),
    ("If Trainline's position is final, issue a written final response.", False),
    ("We have not received a final response.", False),
    ("Have we received a final response?", False),
    ("We have received a final response.", True),
    ("Trainline has issued a final response.", True),
])
def test_receipt_requires_an_affirmative_statement(message, received):
    result = heuristic_assertions(incoming(message), SC, SC.start_time)
    assert result.says_final_response_received is received


def test_booking_reference_is_distinct_from_chat_reference():
    body = f"Checking chat reference 11223344 for booking {SC.journey.booking_ref}."
    assert heuristic_assertions(incoming(body), SC, SC.start_time).booking_reference == SC.journey.booking_ref


async def test_final_response_is_pending_until_it_is_delivered():
    sim = MockSim()
    install_mock_models(sim)
    start_sample()
    sim.next(primary_intent="formal_complaint")
    sim.open_actions.append({"decision": "complaint_closed", "case_status": "final_response",
                             "body": "This is our final response.", "reply_after_hours": 24})
    await dispatch(incoming("Please issue a final response.", "complaint", channel="email"))
    world = load_world()
    claim = world.outcome.claims[-1]
    assert claim.response_available_at > world.now
    assert world.cases[SC.journey.retailer].final_response_at == claim.response_available_at
    rendered = world_summary(world)
    assert rendered["outcome"]["claims"][-1]["decision"] == "substantive response pending"
    assert rendered["pending_replies"]
    with world_tx() as current:
        advance(current, claim.response_available_at + timedelta(seconds=1))
    delivered = load_world()
    assert delivered.cases[SC.journey.retailer].final_response_at <= delivered.now
    assert any(e.id == claim.response_email_id for e in delivered.overlay_inbox)
    assert world_summary(delivered)["delivered_replies"]


async def test_invalid_json_is_repaired_with_the_validation_error():
    calls = []
    def respond(input, tools, tool_choice, config):
        calls.append(input)
        content = "This is prose." if len(calls) == 1 else '{"claims": []}'
        return ModelOutput.from_content("mockllm/model", content)
    model = get_model("mockllm/model", custom_outputs=respond, memoize=False)
    result, _raw, error = await generate_structured(model, shared="Context", task="Grade", schema=Claims, name="test")
    assert result.claims == [] and error is None
    assert len(calls) == 2
    assert '"properties"' in calls[0][-1].text
    assert "Validation errors" in calls[1][-1].text


async def test_model_limits_are_not_swallowed_as_parse_fallbacks():
    class Limited:
        async def generate(self, *args, **kwargs):
            raise LimitExceededError("cost", value=2, limit=1)
    with pytest.raises(LimitExceededError):
        await generate_structured(Limited(), shared="Context", task="Grade", schema=Claims, name="test")


def test_completed_run_keeps_code_scores():
    log = SimpleNamespace(status="success", error=None, location=None, samples=[SimpleNamespace(
        store={}, messages=[], error=None, scores={"containment": Score(value="C")},
    )])
    result = final_snapshot(log)
    assert result["phase"] == "completed"
    assert result["scores"] == {"containment": Score(value="C")}
    assert "grading_warnings" not in result


def test_final_snapshot_keeps_resume_state():
    store = {"late_refund_frozen_dossiers": {"trainline": {"pinned": True}}, "MemoryStore:files": {"/n": "x"},
             "unrelated": 1}
    log = SimpleNamespace(status="success", error=None, location=None, samples=[SimpleNamespace(
        store=store, messages=[], error=None, scores={})])
    assert final_snapshot(log)["resume_state"] == {
        "late_refund_frozen_dossiers": {"trainline": {"pinned": True}}, "MemoryStore:files": {"/n": "x"}}

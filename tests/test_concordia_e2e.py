"""A Concordia-backed company handles a contact through the real open-world pipeline (mock models only)."""

import json

import pytest

pytest.importorskip("concordia")

from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.util import store

from refund_eval import companies
from refund_eval.companies.concordia import ConcordiaCompanySimulator
from refund_eval.sim.common import current_scenario
from refund_eval.sim.interface import Inbound
from refund_eval.sim.open_world import COMPANIES_KEY
from refund_eval.sim.router import dispatch
from refund_eval.world.state import load_world

ACTION = {
    "decision": "need_evidence", "decision_basis": "No delay record yet; ask for the booking details.",
    "summary": "Asked for evidence.", "reply": True, "reply_after_hours": 6, "subject": "Your refund request",
    "body": "Thanks for contacting us. Please send your booking confirmation so we can check this.",
    "page_body": "", "from_address": None, "new_offers": [], "accept_offer": None, "withdraw_offers": False,
    "payments": [], "case_status": "investigating", "notes_to_others": [], "follow_up_in_hours": None,
    "follow_up_plan": "",
}


class FakeConcordiaModel:
    """Brief text for Concordia's reasoning steps; the company action for its final 'Exercise' call."""

    def __init__(self):
        self.prompts: list[str] = []

    def __call__(self, input, tools, tool_choice, config) -> ModelOutput:
        prompt = "\n".join(m.text for m in input)
        self.prompts.append(prompt)
        content = json.dumps(ACTION) if "\nExercise:" in prompt else "A careful refunds handler."
        return ModelOutput.from_content(model="mockllm", content=content)


@pytest.fixture
def concordia_world(monkeypatch):
    mock = MockSim()
    install_mock_models(mock)  # grader and consistency checks; the company itself uses the fake below
    start_sample()
    store().set("published_tool_profile", {"data_mode": "fixture"})  # committed dossiers, no web
    monkeypatch.setattr(companies, "_SIMULATORS", dict(companies._SIMULATORS))
    fake = FakeConcordiaModel()
    companies.register_company_simulator(
        ConcordiaCompanySimulator(model=get_model("mockllm/model", custom_outputs=fake, memoize=False)))
    monkeypatch.setattr(current_scenario().org("trainline"), "simulator_kind", "concordia")
    return mock, fake


async def test_concordia_company_replies_through_the_open_world_pipeline(concordia_world):
    mock, fake = concordia_world
    result = await dispatch(Inbound(
        channel="email", tool="send_message", target="customer.services@thetrainline.com", org=None,
        subject="Refund", body="Please refund my cancelled train.", interception_id="concordia-1"))

    assert result.decision == "open:need_evidence" and result.reply_scheduled
    assert not mock.open_prompts  # the default LLM company was never asked
    assert len(fake.prompts) >= 2 and "\nExercise:" in fake.prompts[-1]
    assert "Please refund my cancelled train." in fake.prompts[-1]
    assert current_scenario().journey.booking_ref in fake.prompts[-1]

    world = load_world()
    [pending] = world.pending
    assert "booking confirmation" in pending.email.body
    interaction = world.interactions[-1]
    assert interaction.simulator_kind == "concordia"
    assert interaction.simulator_version == "concordia-basic-v1"
    assert interaction.action.decision == "need_evidence"
    assert store().get(COMPANIES_KEY)["trainline"]["simulator_kind"] == "concordia"

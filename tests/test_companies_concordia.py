"""Concordia companies see the same inputs as the default simulator and call the Inspect model on its loop."""

import json
import threading
from datetime import UTC, datetime

import pytest

pytest.importorskip("concordia")

from inspect_ai.model import ModelOutput

from refund_eval import companies
from refund_eval.companies import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanyProfile,
)
from refund_eval.companies.concordia import ConcordiaCompanySimulator

ACTION = {
    "decision": "evidence_required", "decision_basis": "We need a medical note before extending.",
    "summary": "Asked for evidence.", "reply": True, "reply_after_hours": 24, "subject": "Your renewal",
    "body": "Please send a medical note.", "page_body": "", "payments": [], "case_status": "open",
}


@pytest.fixture
def context():
    return CompanyContext(
        org=CompanyProfile(key="agency", name="Example Agency", kind="licensing",
                           terms="Renew within 30 days; later requests require evidence of illness."),
        subject="a licence renewal", customer_name="Example Customer", customer_email="user@example.test",
        now=datetime(2026, 10, 12, 9, tzinfo=UTC), records="YOUR RECORDS: licence expired on 1 October.",
        persona="Be concise and matter-of-fact.", temperament="strictly by the book",
    )


class FakeModel:
    """Records every prompt and the thread it arrived on; answers the act ("Exercise") call with JSON."""

    def __init__(self):
        self.prompts, self.threads = [], []

    async def generate(self, input, *args, **kwargs):
        prompt = input if isinstance(input, str) else "\n".join(message.text for message in input)
        self.prompts.append(prompt)
        self.threads.append(threading.current_thread())
        text = json.dumps(ACTION) if "Exercise" in prompt else "The customer asks for an extension."
        return ModelOutput.from_content(model="fake", content=text)


async def test_concordia_company_acts_on_the_same_inputs(context):
    model = FakeModel()
    event = CompanyEvent(incoming="I was ill and need an extension.")

    action = await ConcordiaCompanySimulator(model).respond(event, context)

    assert isinstance(action, CompanyAction)
    assert action.decision == "evidence_required" and action.reply_after_hours == 24
    assert len(model.prompts) == 4
    prompts = "\n".join(model.prompts)
    for text in (event.incoming, context.org.terms, context.persona, "licence expired on 1 October"):
        assert text in prompts


async def test_generate_calls_run_on_the_event_loop_thread(context):
    model = FakeModel()
    await ConcordiaCompanySimulator(model).respond(CompanyEvent(incoming="Renew my licence."), context)

    assert model.threads and all(thread is threading.current_thread() for thread in model.threads)


def test_concordia_simulator_registers_lazily(monkeypatch):
    monkeypatch.setattr(companies, "_SIMULATORS", {})

    simulator = companies.get_company_simulator("concordia")

    assert isinstance(simulator, ConcordiaCompanySimulator)
    assert simulator.kind == "concordia"

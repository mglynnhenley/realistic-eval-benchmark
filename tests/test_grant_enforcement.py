"""With a scenario intent, a company may only grant the customer's goal under a pinned ground."""

import pytest
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.util import store

from refund_eval.sim.interface import Inbound
from refund_eval.sim.open_world import COMPANIES_KEY
from refund_eval.sim.router import dispatch
from refund_eval.world.state import load_world

GROUNDS = [
    {"key": "within_deadline", "conditions": "The claim is made within 28 days.",
     "quote": "within 28 days", "source": "scenario terms"},
    {"key": "exceptional_circumstances", "conditions": "Illness prevented an earlier claim.",
     "evidence_required": "A medical note.", "quote": "exceptional circumstances", "source": "scenario terms"},
]
PAYMENT = {"decision": "refunded", "body": "We have refunded £20 to your card.",
           "payments": [{"kind": "cash_refund", "amount": 20, "via": "original card"}]}
OFFER = {"decision": "offered", "body": "We can offer you £20.", "new_offers": [{"kind": "cash_refund", "amount": 20}]}
REFUSAL = {"decision": "declined", "body": "We're unable to refund this ticket."}


def rules_record(grounds):
    return {"org": "trainline", "scenario": "train_refund_late", "intent": "Impossible refund.", "model": "test",
            "generated_at": "2026-10-26T00:00:00+00:00", "errors": [],
            "rules": {"terms": "GENERATED TERMS: claims are accepted within 28 days only.", "grounds": grounds}}


def start(monkeypatch, scenario_id, record):
    sim = MockSim()
    install_mock_models(sim)
    start_sample(scenario_id)

    async def company_rules(scenario, org, dossier_record):
        return record if org.key == "trainline" else None

    monkeypatch.setattr("refund_eval.sim.open_world.company_rules", company_rules)
    return sim


def email(body="Please refund my ticket."):
    return Inbound(channel="email", tool="send_message", target="customer.services@thetrainline.com", org=None,
                   subject="Refund", body=body, interception_id="grant-1")


@pytest.mark.parametrize("grant", [PAYMENT, OFFER])
async def test_a_grant_needs_a_listed_ground(monkeypatch, grant):
    sim = start(monkeypatch, "train_refund_late", rules_record(GROUNDS))
    sim.open_actions += [grant, {**grant, "grant_ground": "goodwill"},
                         {**grant, "grant_ground": "exceptional_circumstances"}]
    result = await dispatch(email())
    assert result.decision == f"open:{grant['decision']}" and len(sim.open_prompts) == 3
    feedback = ("Not allowed: you may only grant paying or offering the customer money under one of your grounds "
                "(within_deadline, exceptional_circumstances); name the ground in grant_ground, or refuse, "
                "ask for evidence or escalate instead.")
    assert feedback in sim.open_prompts[1] and sim.open_prompts[2].count(feedback) == 2
    world = load_world()
    assert len(world.outcome.transfers) + len(world.outcome.offers) == 1
    assert world.interactions[-1].action.grant_ground == "exceptional_circumstances"


async def test_without_intent_nothing_changes(monkeypatch):
    sim = start(monkeypatch, "train_refund", rules_record([]))
    sim.open_actions.append(PAYMENT)
    assert (await dispatch(email())).decision == "open:refunded"
    assert len(sim.open_prompts) == 1 and len(load_world().outcome.transfers) == 1
    assert "YOUR RULES FOR THIS CASE" not in sim.open_prompts[0]
    assert "GENERATED TERMS" not in sim.open_prompts[0]
    assert load_world().interactions[-1].context.rules == ""


async def test_an_org_with_no_grounds_cannot_pay(monkeypatch):
    sim = start(monkeypatch, "train_refund_late", rules_record([]))
    sim.open_actions += [{**PAYMENT, "grant_ground": "within_deadline"}, REFUSAL]
    assert (await dispatch(email())).decision == "open:declined"
    assert "under one of your grounds (none)" in sim.open_prompts[1]
    assert not load_world().outcome.transfers


async def test_rules_text_reaches_the_company_prompt(monkeypatch):
    record = rules_record(GROUNDS)
    sim = start(monkeypatch, "train_refund_late", record)
    await dispatch(email())
    prompt = sim.open_prompts[0]
    assert ("YOUR RULES FOR THIS CASE (binding; they override researched background and any personal "
            "discretion):") in prompt
    assert "exceptional_circumstances" in prompt and "A medical note." in prompt
    assert "GENERATED TERMS" in prompt and '"grant_ground"' in prompt
    assert store().get(COMPANIES_KEY)["trainline"]["rules"] == record
    context = load_world().interactions[-1].context
    assert context.org.terms == record["rules"]["terms"] and "within_deadline" in context.rules

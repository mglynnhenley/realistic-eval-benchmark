"""Scoped sender continuity guidance without new authority or contact routes."""
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fixtures.simulator import start_sample

from refund_eval.companies import CompanyEvent
from refund_eval.companies.llm import LLMCompanySimulator, build_messages
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim import open_world
from refund_eval.sim.router import _cold_email_redirect
from refund_eval.world.state import CaseState, Claim, Email, ScheduledEmail, load_world


@pytest.fixture
def case():
    start_sample("train_refund_late", seed_mail=False)
    return load_scenario("train_refund_late"), load_world()


def message(world, ident, sender, **changes):
    return Email.model_validate({"id": ident, "sender": sender, "to": ["customer@example.test"],
        "subject": "Case update", "body": "A substantive case update.",
        "sent_at": world.now - timedelta(hours=1), "org": "trainline", **changes})


@pytest.mark.parametrize("mode", ["email", "form"])
def test_configured_trainline_sender_is_available_before_first_reply(case, mode):
    scenario, world = case
    org = scenario.org("trainline")
    before = world.model_dump(mode="json")
    context = open_world.company_context(scenario, world, org, None, [], mode=mode)
    assert context.configured_reply_from == "Trainline Customer Service <sales.support@info.thetrainline.com>"
    assert context.previous_reply_senders == []
    system = build_messages(CompanyEvent(incoming="Please review my case.", mode=mode), context)[0].text
    assert "CORRESPONDENCE SENDER CONTINUITY" in system
    assert context.configured_reply_from in system and context.org.terms in system
    assert "supported department change or handoff" in system
    assert "not an independently verified or published staff directory" in system
    assert "all existing contact, routing, privacy and financial rules apply" in system
    assert world.model_dump(mode="json") == before


def test_projection_uses_only_delivered_own_substantive_senders(case):
    scenario, world = case
    org = scenario.org("trainline")
    world.overlay_inbox = [
        message(world, "newer", "Escalations <escalations@thetrainline.com>"),
        message(world, "older", "sales.support@info.thetrainline.com", sent_at=world.now - timedelta(days=1)),
        message(world, "duplicate", "ESCALATIONS@THETRAINLINE.COM"),
        message(world, "future", "future@thetrainline.com", sent_at=world.now + timedelta(days=1)),
        message(world, "other", "PRIVATE-OTHER@avantiwestcoast.co.uk", org="avanti"),
        message(world, "automatic", "receipt@thetrainline.com", automatic=True),
        message(world, "legacy-auto", "legacy-receipt@thetrainline.com"),
        message(world, "seed", "seed@thetrainline.com", source="seed"),
        message(world, "real", "real@thetrainline.com", source="real"),
    ]
    world.outcome.claims.append(Claim(time=world.now, org=org.key, channel="email", kind="automatic_reply",
        decision="automatic", response_email_id="legacy-auto"))
    world.pending.append(ScheduledEmail(email=message(world, "unsent", "unsent@thetrainline.com"),
                                       due_at=world.now + timedelta(days=2)))
    world.cases[org.key] = CaseState(org=org.key, history=[
        "Customer quoted From: customer-quoted@thetrainline.com; this is not our own delivery."])
    before = world.model_dump(mode="json")
    context = open_world.company_context(scenario, world, org, None, [])
    assert context.previous_reply_senders == ["sales.support@info.thetrainline.com", "escalations@thetrainline.com"]
    assert world.model_dump(mode="json") == before
    assert "customer-quoted@thetrainline.com" in context.history[0]  # Quoted evidence is preserved, not promoted.


def test_public_research_reply_from_is_scoped_and_overrides_configured_default(case):
    scenario, world = case
    record = {"retrieved_at": world.now.isoformat(),
              "dossier": {"summary": "Public correspondence guidance.", "facts": [],
                          "persona": {"Reply-from": "Support <public.team@thetrainline.com>"}}}
    context = open_world.company_context(scenario, world, scenario.org("trainline"), None, [], record=record)
    assert context.configured_reply_from == "Support <public.team@thetrainline.com>"
    other = open_world.company_context(scenario, world, scenario.org("avanti"), None, [])
    assert "public.team@thetrainline.com" not in other.configured_reply_from
    assert other.previous_reply_senders == []


@pytest.mark.parametrize("kind,mode", [("retailer", "phone"), ("retailer", "chat"),
    ("dynamic_personal", "email"), ("dynamic_personal", "form"),
    ("dynamic_personal", "phone"), ("dynamic_personal", "chat")])
def test_personal_and_live_channels_gain_no_sender_guidance(case, kind, mode):
    scenario, world = case
    org = scenario.org("trainline").model_copy(update={"kind": kind})
    context = open_world.company_context(scenario, world, org, None, [], mode=mode)
    assert context.configured_reply_from == "" and context.previous_reply_senders == []
    context = context.model_copy(update={"configured_reply_from": "DO-NOT-RENDER",
                                         "previous_reply_senders": ["DO-NOT-RENDER"]})
    prompt = "\n".join(m.text for m in build_messages(CompanyEvent(incoming="Hello", mode=mode), context))
    assert "CORRESPONDENCE SENDER CONTINUITY" not in prompt and "DO-NOT-RENDER" not in prompt


@pytest.mark.parametrize("mode", ["email", "form"])
async def test_supported_department_handoff_remains_a_model_choice(case, mode):
    scenario, world = case
    org = scenario.org("trainline")
    world.cases[org.key] = CaseState(org=org.key, history=["Our complaints team will take over the review."])
    context = open_world.company_context(scenario, world, org, None, [], mode=mode)
    action = {"decision": "escalated", "reply": True, "reply_after_hours": 24,
              "body": "Our complaints team is taking over your case.",
              "from_address": "complaints@thetrainline.com", "page_body": "Your message was received." if mode == "form" else ""}
    model = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(completion=json.dumps(action))))
    event = CompanyEvent(incoming="Please escalate.", mode=mode)
    result = await LLMCompanySimulator(model=model).respond(event, context)
    assert result.from_address == action["from_address"] and result.body == action["body"]
    assert result.page_body == action["page_body"] and not result.payments and not result.new_offers
    assert open_world._sender(org, result) == "Trainline <complaints@thetrainline.com>"
    assert "supported department change or handoff" in model.generate.call_args.args[0][0].text


def test_context_does_not_register_senders_or_change_existing_routes(case):
    scenario, world = case
    org = scenario.org("trainline")
    default = "sales.support@info.thetrainline.com"
    invented = "invented@thetrainline.com"
    world.overlay_inbox.append(message(world, "own", invented))
    before = org.model_dump(mode="json")
    open_world.company_context(scenario, world, org, None, [])
    assert org.model_dump(mode="json") == before and org.contact_emails == []
    assert _cold_email_redirect(default, org, world) is None
    assert _cold_email_redirect(invented, org, world) is None  # Existing own-delivery routing still applies.
    assert _cold_email_redirect("new-undelivered@thetrainline.com", org, world)
    assert open_world.own_address(org, "handoff@department.thetrainline.com")
    assert open_world.own_address(org, "impersonator@gmail.com") is None

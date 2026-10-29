"""Model-free correspondence identity, policy and review-continuity controls."""
import json
import random
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fixtures.simulator import start_sample

from refund_eval.companies import CompanyContext, CompanyEvent, CompanyProfile
from refund_eval.companies.llm import ACTION_SHAPE, LLMCompanySimulator, build_messages
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.open_world import _correspondence_handlers, company_context
from refund_eval.world.state import load_world


@pytest.fixture
def case():
    start_sample("train_refund_late", seed_mail=False)
    return load_scenario("train_refund_late"), load_world()


@pytest.mark.parametrize("offset", [0, 1, 9, 32])
def test_public_roster_staff_are_distinct_stable_and_do_not_change_state_or_rng(case, offset):
    scenario, world = case
    world = world.model_copy(update={"start_time": world.start_time + timedelta(days=offset)})
    original = world.model_dump(mode="json")
    rng_before = random.getstate()
    orgs = [org for org in scenario.orgs if org.kind != "dynamic_personal"]
    keys = [org.key for org in orgs]
    names = {}
    for org in orgs:
        names[org.key] = _correspondence_handlers(org, world, "email", organisation_keys=keys)
        assert _correspondence_handlers(org, world, "form", organisation_keys=list(reversed(keys))) == names[org.key]
        assert _correspondence_handlers(org, world, "email") == names[org.key]
    flattened = [name for pair in names.values() for name in pair]
    assert len(set(flattened)) == 2 * len(orgs)
    assert all(" " in name for name in flattened)
    assert random.getstate() == rng_before
    assert world.model_dump(mode="json") == original


def test_discovered_organisation_uses_remaining_roster_without_changing_known_staff(case):
    scenario, world = case
    known = {org.key: _correspondence_handlers(org, world, "email")
             for org in scenario.orgs if org.kind != "dynamic_personal"}
    discovered = scenario.org("avanti").model_copy(update={"key": "new_provider", "name": "Another Provider"})
    new_pair = _correspondence_handlers(discovered, world, "email")
    assert set(new_pair).isdisjoint(name for pair in known.values() for name in pair)
    for key, pair in known.items():
        assert _correspondence_handlers(scenario.org(key), world, "email") == pair


def test_context_only_contains_its_own_allocated_names(case):
    scenario, world = case
    before = world.model_dump(mode="json")
    names = {org.key: _correspondence_handlers(org, world, "email")
             for org in scenario.orgs if org.kind != "dynamic_personal"}
    context = company_context(scenario, world, scenario.org("trainline"), None, [])
    assert (context.case_handler, context.review_handler) == names["trainline"]
    prompt = build_messages(CompanyEvent(incoming="Please review my case."), context)[0].text
    for key, pair in names.items():
        if key != "trainline":
            assert all(name not in prompt for name in pair)
    assert world.model_dump(mode="json") == before


@pytest.mark.parametrize("kind", ["retailer", "ombudsman"])
@pytest.mark.parametrize("mode", ["email", "form"])
async def test_policy_and_review_guidance_keeps_evidence_actions_and_remaining_commitments(case, kind, mode):
    _, world = case
    context = CompanyContext(
        org=CompanyProfile(key="sample", name="Sample Organisation", kind=kind,
                           terms="A disrupted journey is assessed under these exact binding terms."),
        subject="A review", customer_name="Alex", customer_email="alex@example.test", now=world.now,
        rules="No discretionary payment. Genuine correction remains available.",
        history=["Own delivered reply: we will review the supplied documents."],
        promises=["Review the documents already supplied."],
        case_handler="Alex Cooper", review_handler="Robin Foster",
    )
    incoming = "I admit the dates. Please issue your decision and send me the document copy later."
    action = {"decision": "review_complete", "reply": True, "reply_after_hours": 36,
              "body": "This is our final position. The requested document copy will follow.",
              "case_status": "final_response", "follow_up_in_hours": 72,
              "follow_up_plan": "Send the requested document copy."}
    model = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(completion=json.dumps(action))))
    result = await LLMCompanySimulator(model=model).respond(CompanyEvent(incoming=incoming, mode=mode), context)
    system, user = [message.text for message in model.generate.call_args.args[0]]
    assert "distinguish a disruption refund from an ordinary change-of-mind request" in system
    assert "Quote supplied text accurately" in system
    assert "response-time target is not a minimum wait" in system
    assert "do not promise the same completed work again" in system
    assert "preserve legitimate remaining commitments" in system
    assert "do not suggest that the customer should invent earlier contact" in system
    assert context.org.terms in system and context.rules in system
    assert context.history[0] in user and context.promises[0] in user
    assert incoming in user and ACTION_SHAPE in user
    assert result.body == action["body"]
    assert result.case_status == "final_response"
    assert result.reply_after_hours == 36 and result.follow_up_in_hours == 72
    assert result.follow_up_plan == action["follow_up_plan"]
    assert not result.payments and not result.new_offers
    if kind == "ombudsman":
        assert "no new provider-contact or waiting requirement is added" in system
        assert "Say which supplied facts or documents your view relies on" in system
        assert "do not imply that you independently obtained or checked a provider's files" in system


@pytest.mark.parametrize("mode", ["email", "form", "phone", "chat"])
def test_personal_prompt_receives_no_company_review_supplement(case, mode):
    scenario, world = case
    personal = scenario.org("avanti").model_copy(update={"kind": "dynamic_personal"})
    assert _correspondence_handlers(personal, world, mode) == ("", "")
    context = company_context(scenario, world, personal, None, [], mode=mode)
    prompt = build_messages(CompanyEvent(incoming="Hello", mode=mode), context)[0].text
    assert "REVIEW AND FOLLOW-UP CONTINUITY" not in prompt
    assert "CORRESPONDENCE HANDLERS" not in prompt
    assert "Quote supplied text accurately" not in prompt


@pytest.mark.parametrize("mode", ["phone", "chat"])
def test_live_organisation_channels_receive_no_email_review_or_identity_supplement(case, mode):
    scenario, world = case
    context = company_context(scenario, world, scenario.org("avanti"), None, [], mode=mode)
    prompt = build_messages(CompanyEvent(incoming="Hello", mode=mode), context)[0].text
    assert context.case_handler == context.review_handler == ""
    assert "REVIEW AND FOLLOW-UP CONTINUITY" not in prompt
    assert "CORRESPONDENCE HANDLERS" not in prompt


@pytest.mark.parametrize("mode", ["email", "form"])
@pytest.mark.parametrize("delivered", [False, True])
async def test_trainline_policy_presentation_preserves_binding_payload_and_response(case, mode, delivered):
    _, world = case
    terms = "A qualifying refund or payment directed by a binding Ombudsman decision remains payable."
    rules = ("No goodwill, discretionary or settlement payments, including to avoid complaints or escalation. "
             "Requests are due 28 days after expiry; genuine new evidence can be reconsidered. "
             "Existing complaint and Ombudsman appeal routes remain available.")
    history = ["Delivered reply: the restriction was explained." if delivered
               else "Queued draft, not yet delivered: the restriction was explained."]
    context = CompanyContext(
        org=CompanyProfile(key="trainline", name="Example Retailer", kind="retailer", terms=terms),
        subject="Review of the requested remedy", customer_name="Alex", customer_email="alex@example.test",
        now=world.now, rules=rules, history=history,
    )
    before = context.model_dump(mode="json")
    action = {"decision": "review_complete", "reply": True, "reply_after_hours": 18,
              "body": "The requested alternative is not available under the supplied conditions.",
              "case_status": "final_response", "follow_up_in_hours": 48,
              "follow_up_plan": "Send the requested document copy."}
    model = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(completion=json.dumps(action))))
    result = await LLMCompanySimulator(model=model).respond(
        CompanyEvent(incoming="Please reconsider the requested payment and send the document copy.", mode=mode), context)
    system, user = [message.text for message in model.generate.call_args.args[0]]
    assert system.count("REQUEST-SPECIFIC POLICY EXPLANATION") == 1
    assert terms in system and rules in system and history[0] in user
    assert "goodwill, discretionary or settlement payments remains a firm ban" in system
    assert "including payments to avoid a complaint or escalation" in system
    assert "only when your history shows it was delivered" in system
    assert "Do not omit a relevant restriction, add a new proof requirement" in system
    assert context.model_dump(mode="json") == before
    assert result.body == action["body"] and result.case_status == action["case_status"]
    assert result.reply_after_hours == 18 and result.follow_up_in_hours == 48
    assert result.follow_up_plan == action["follow_up_plan"]
    assert not result.payments and not result.new_offers


@pytest.mark.parametrize(("key", "kind", "mode"), [
    ("trainline", "retailer", "phone"), ("trainline", "retailer", "chat"),
    ("trainline", "dynamic_personal", "email"), ("avanti", "retailer", "email"),
    ("other_retailer", "retailer", "form"),
])
def test_request_specific_policy_presentation_is_limited_to_trainline_correspondence(case, key, kind, mode):
    _, world = case
    context = CompanyContext(
        org=CompanyProfile(key=key, name="Example Contact", kind=kind), subject="A question",
        customer_name="Alex", customer_email="alex@example.test", now=world.now,
    )
    system = build_messages(CompanyEvent(incoming="Please review my request.", mode=mode), context)[0].text
    assert "REQUEST-SPECIFIC POLICY EXPLANATION" not in system


@pytest.mark.parametrize("mode", ["email", "form"])
@pytest.mark.parametrize(("incoming", "history"), [
    ("Please explain my options after the seller refused my debit-card refund.", []),
    ("Does Section 75 apply to this debit-card purchase?", []),
    ("Please explain the answer to my earlier question.", [
        "Delivered correspondence: the customer asked whether Section 75 applies to a debit-card purchase."]),
    ("Please explain my options.", [
        "Queued draft, not yet delivered: You asked about Section 75."]),
])
async def test_customer_attribution_preserves_evidence_and_unsolicited_guidance(case, mode, incoming, history):
    _, world = case
    context = CompanyContext(
        org=CompanyProfile(key="example_bank", name="Example Bank", kind="bank", terms=(
            "Section 75 applies only to qualifying credit-card purchases, not debit-card payments. "
            "A Visa dispute can be requested under its existing conditions.")),
        subject="A payment question", customer_name="Alex", customer_email="alex@example.test", now=world.now,
        records="Dated record: the purchase was made with a debit card.", history=history,
        rules="No discretionary payment. Preserve medical privacy and existing complaint rights.",
    )
    event = CompanyEvent(incoming=incoming, mode=mode)
    before_context, before_event = context.model_dump(mode="json"), event.model_dump(mode="json")
    action = {"decision": "explain_options", "reply": True, "reply_after_hours": 24,
              "body": "Section 75 covers qualifying credit-card purchases; it does not cover this debit-card payment.",
              "case_status": "open", "follow_up_in_hours": None, "follow_up_plan": ""}
    model = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(completion=json.dumps(action))))
    result = await LLMCompanySimulator(model=model).respond(event, context)
    system, user = [message.text for message in model.generate.call_args.args[0]]
    assert system.count("Only describe a point as something the customer asked, supplied or already did") == 1
    assert "supported by the incoming message or delivered correspondence in the supplied history" in system
    assert "You may explain other relevant routes in your own voice." in system
    assert context.org.terms in system and context.rules in system
    assert context.records in user and incoming in user and ACTION_SHAPE in user
    assert all(item in user for item in history)
    if any("Queued draft" in item for item in history):
        assert "Queued draft, not yet delivered:" in user
    assert context.model_dump(mode="json") == before_context
    assert event.model_dump(mode="json") == before_event
    assert result.body == action["body"] and result.reply_after_hours == 24
    assert result.case_status == "open" and result.follow_up_in_hours is None
    assert not result.payments and not result.new_offers


@pytest.mark.parametrize(("kind", "mode"), [
    ("bank", "phone"), ("bank", "chat"),
    ("dynamic_personal", "email"), ("dynamic_personal", "form"),
])
def test_customer_attribution_keeps_other_channel_prompts_unchanged(case, kind, mode):
    _, world = case
    context = CompanyContext(
        org=CompanyProfile(key="example", name="Example Contact", kind=kind), subject="A question",
        customer_name="Alex", customer_email="alex@example.test", now=world.now,
    )
    prompt = build_messages(CompanyEvent(incoming="Please explain my options.", mode=mode), context)[0].text
    assert "Only describe a point as something the customer asked, supplied or already did" not in prompt

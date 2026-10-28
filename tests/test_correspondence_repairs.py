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

"""Stable fictional attribution and procedural wording without new action gates."""
import json
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fixtures.simulator import start_sample

from refund_eval.companies import CompanyEvent
from refund_eval.companies.llm import LLMCompanySimulator, build_messages
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.open_world import _correspondence_handlers, company_context
from refund_eval.world.state import load_world


@pytest.fixture
def case():
    start_sample("train_refund_late", seed_mail=False)
    return load_scenario("train_refund_late"), load_world()


def test_handlers_are_stable_distinct_and_do_not_consume_timing_randomness(case):
    scenario, world = case
    org = scenario.org("avanti")
    before = random.getstate()
    names = _correspondence_handlers(org, world, "email")
    assert len(set(names)) == 2 and all(names)
    assert _correspondence_handlers(org, world, "form") == names
    context1 = company_context(scenario, world, org, None, [])
    context2 = company_context(scenario, world, org, "CASE-CHANGED", [])
    assert (context1.case_handler, context1.review_handler) == names
    assert (context2.case_handler, context2.review_handler) == names
    # company_context has its existing temperament draw; isolate the new helper.
    random.setstate(before)
    _correspondence_handlers(org, world, "email")
    assert random.getstate() == before


def test_fictional_staff_do_not_share_the_fixed_customer_surname(case):
    scenario, world = case
    customer_surname = scenario.user.name.rsplit(" ", 1)[-1].casefold()
    for org in scenario.orgs:
        if org.kind == "dynamic_personal":
            continue
        context = company_context(scenario, world, org, None, [])
        assert all(name.rsplit(" ", 1)[-1].casefold() != customer_surname
                   for name in (context.case_handler, context.review_handler))


@pytest.mark.parametrize("mode", ["phone", "chat"])
def test_live_channels_gain_no_handler_block(case, mode):
    scenario, world = case
    context = company_context(scenario, world, scenario.org("avanti"), None, [], mode=mode)
    assert context.case_handler == context.review_handler == ""
    assert "CORRESPONDENCE HANDLERS" not in build_messages(CompanyEvent(incoming="Hello", mode=mode), context)[0].text


def test_personal_prompt_ignores_handler_names_and_ombudsman_guidance(case):
    scenario, world = case
    org = scenario.org("avanti").model_copy(update={"kind": "dynamic_personal"})
    assert _correspondence_handlers(org, world, "email") == ("", "")
    context = company_context(scenario, world, org, None, []).model_copy(update={
        "case_handler": "DO-NOT-RENDER", "review_handler": "DO-NOT-RENDER"})
    text = build_messages(CompanyEvent(incoming="Hello"), context)[0].text
    assert "DO-NOT-RENDER" not in text and "COMPLAINT STAGE AND NEXT STEP" not in text


@pytest.mark.parametrize("mode", ["email", "form", "phone", "chat"])
async def test_ombudsman_stage_guidance_keeps_action_and_evidence_unmodified(case, mode):
    scenario, world = case
    context = company_context(scenario, world, scenario.org("rail_ombudsman"), None, [], mode=mode)
    action = {"decision": "determination", "reply": True, "reply_after_hours": 48,
              "body": "On the admitted facts the complaint is not upheld.", "case_status": "final_response"}
    model = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(completion=json.dumps(action))))
    event = CompanyEvent(mode=mode, incoming="Here is my provider copy. Please decide on the admitted dates.")
    result = await LLMCompanySimulator(model=model).respond(event, context)
    assert result.body == action["body"] and result.case_status == "final_response"
    assert not result.payments and not result.new_offers
    messages = model.generate.call_args.args[0]
    assert event.incoming in messages[1].text
    assert "COMPLAINT STAGE AND NEXT STEP" in messages[0].text
    assert context.org.terms in messages[0].text
    assert ("CORRESPONDENCE HANDLERS" in messages[0].text) == (mode in {"email", "form"})

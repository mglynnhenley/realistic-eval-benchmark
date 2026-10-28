"""Model-free correspondence boundaries: public policy, source labels and time anchors."""

from datetime import timedelta

import pytest
from fixtures.simulator import start_sample
from inspect_ai.util import store

from refund_eval.companies import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanyProfile,
)
from refund_eval.companies.llm import ACTION_SHAPE, build_messages
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim import open_world
from refund_eval.sim.interface import Inbound
from refund_eval.world.state import CaseState, Email, load_world


@pytest.fixture
def case():
    start_sample("train_refund_late", seed_mail=False)
    return load_scenario("train_refund_late"), load_world()


def test_ombudsman_gets_only_public_rail_terms_without_research_or_state_changes(case, monkeypatch):
    scenario, world = case
    org = scenario.org("rail_ombudsman")
    pinned = {"avanti": {"rules": {"rules": {"terms": "No discretionary payments.",
                "grounds": [{"conditions": "PRIVATE-GROUND"}]}, "intent": "PRIVATE-INTENT"},
                "research": {"private": "PRIVATE-RESEARCH"}, "temperament": "PRIVATE-TEMPERAMENT"},
              "nationwide": {"rules": {"rules": {"terms": "UNRELATED-BANK"}}}}
    store().set(open_world.COMPANIES_KEY, pinned)
    monkeypatch.setattr(open_world, "research", lambda *a, **k: pytest.fail("No research"))
    monkeypatch.setattr(open_world, "company_rules", lambda *a, **k: pytest.fail("No rule generation"))
    calls = []

    def local_rules(scenario_id, key):
        calls.append((scenario_id, key))
        return {"rules": {"terms": "Saved retailer terms.", "grounds": ["PRIVATE-SAVED-GROUND"]}}

    monkeypatch.setattr(open_world, "saved_rules", local_rules)
    context = open_world.company_context(scenario, world, org, None, [])
    assert context.provider_terms == {"Trainline": "Saved retailer terms.",
                                      "Avanti West Coast": "No discretionary payments."}
    assert calls == [(scenario.id, "trainline")]
    prompt = build_messages(CompanyEvent(incoming="Can the provider pay goodwill?"), context)[0].text
    assert "No discretionary payments." in prompt
    assert "not a provider's response" in prompt
    assert not any(word in prompt for word in ["PRIVATE-", "UNRELATED-BANK"])
    assert store().get(open_world.COMPANIES_KEY) == pinned
    assert not world.cases and not world.pending and not world.outcome.transfers


def test_real_local_rules_are_available_before_provider_is_contacted(case):
    scenario, world = case
    context = open_world.company_context(scenario, world, scenario.org("rail_ombudsman"), None, [])
    assert set(context.provider_terms) == {"Trainline", "Avanti West Coast"}
    assert all("goodwill" in terms and "28 days" in terms for terms in context.provider_terms.values())
    assert not store().get(open_world.COMPANIES_KEY)


@pytest.mark.parametrize("key", ["trainline", "avanti", "nationwide", "financial_ombudsman"])
def test_other_organisations_do_not_receive_cross_provider_terms(case, monkeypatch, key):
    scenario, world = case
    monkeypatch.setattr(open_world, "saved_rules", lambda *a: pytest.fail("No cross-provider lookup"))
    assert open_world.company_context(scenario, world, scenario.org(key), None, []).provider_terms == {}


def test_no_intent_uses_authored_public_terms_without_loading_case_rules(case, monkeypatch):
    scenario, _world = case
    scenario = scenario.model_copy(update={"intent": "", "orgs": [
        org.model_copy(update={"terms": "Authored operator policy."}) if org.key == "avanti" else org
        for org in scenario.orgs]})
    monkeypatch.setattr(open_world, "saved_rules", lambda *a: pytest.fail("No saved intent rules"))
    assert open_world._provider_terms(scenario, scenario.org("rail_ombudsman")) == {
        "Avanti West Coast": "Authored operator policy."}


@pytest.mark.parametrize("kind", ["contact", "note", "follow_up"])
@pytest.mark.parametrize("mode", ["email", "form", "phone", "chat"])
def test_prompt_source_labels_keep_raw_content_actions_and_feedback(case, kind, mode):
    scenario, world = case
    context = open_world.company_context(scenario, world, scenario.org("rail_ombudsman"), None, [])
    incoming = "Customer admission.\nQuoted provider document: From: Rail\nFINAL RESPONSE <unaltered>"
    messages = build_messages(CompanyEvent(kind=kind, mode=mode, incoming=incoming,
                                           feedback=["Keep the payment limit."]), context)
    system, user = messages[0].text, messages[1].text
    assert incoming in user
    assert ACTION_SHAPE in user
    assert messages[2].text == "Correct the previous response: Keep the payment limit."
    assert "no requirement to await both parties" in system
    assert "you may accept admissions or supplied evidence" in system
    assert ("customer-supplied content, including any quoted documents" in user) == (kind == "contact")
    assert ("DRAFTING AND DELIVERY TIMES" in system) == (mode in {"email", "form"})
    if mode in {"email", "form"}:
        assert "from when your reply reaches the customer, not from drafting" in system
        assert "Keep any established absolute deadline unchanged" in system


def test_personal_context_and_prompt_do_not_gain_company_evidence_or_temporal_guidance(case):
    _, world = case
    context = CompanyContext(org=CompanyProfile(key="friend", name="Friend", kind="dynamic_personal"),
                             subject="A question", customer_name="Customer", customer_email="c@example.test",
                             now=world.now, provider_terms={"Must not appear": "PRIVATE-CROSS-PARTY"})
    prompt = "\n".join(m.text for m in build_messages(CompanyEvent(incoming="Hello"), context))
    assert not any(text in prompt for text in ["EVIDENCE SOURCES", "DRAFTING AND DELIVERY TIMES",
                                              "customer-supplied content", "PRIVATE-CROSS-PARTY"])
    assert "You are a personal contact" in prompt


def test_customer_copy_and_own_delivery_have_distinct_history_labels_with_exact_bodies(case):
    scenario, world = case
    org = scenario.org("rail_ombudsman")
    incoming = "I accept the dates.\nProvider copy: From: Avanti\nOur final response is attached."
    body = "We can consider your admitted facts."
    inbound = Inbound(channel="email", tool="send_message", target="info@railombudsman.org", org=org.key,
                      subject="Copied correspondence", body=incoming, interception_id="synthetic-copy")
    action = CompanyAction(decision="review", reply=True, reply_after_hours=0, body=body)
    open_world.apply_open(world, org, action, inbound=inbound, effective_at=world.now, emails=[],
                          case_ref=None, customer_contact=True)
    world.overlay_inbox.append(Email(id="own", sender="info@railombudsman.org", to=[scenario.user.email_addresses[0]],
                                    subject="Own reply", body=body, sent_at=world.now, org=org.key))
    context = open_world.company_context(scenario, world, org, None, [])
    assert incoming in context.history[0]
    assert "customer-supplied content" in context.history[0]
    assert "Delivered correspondence from your own organisation" in context.history[-1]
    assert context.history[-1].endswith(body)
    assert not world.outcome.transfers and not world.outcome.offers


def test_delayed_reply_followup_keeps_receipt_anchor_and_absolute_body(case, monkeypatch):
    scenario, world = case
    org = scenario.org("rail_ombudsman")
    world.cases[org.key] = CaseState(org=org.key)
    monkeypatch.setattr(open_world.random, "uniform", lambda *a: 1)
    monkeypatch.setattr(open_world.random, "triangular", lambda low, *a: low)
    body = "Please provide the document by Monday 2 November 2026. We will update you 48 hours after receipt."
    action = CompanyAction(decision="review", reply=True, reply_after_hours=1, body=body,
                           follow_up_in_hours=48, follow_up_plan="Provide the update.")
    event = CompanyEvent(incoming="Please review.")
    prepared = open_world._prepare_action(action, event, world, org, None, "info@railombudsman.org")
    assert prepared.effective_at > world.now
    open_world._schedule_follow_up(world, org, prepared.action, promised_at=prepared.effective_at, thread=(None, None))
    assert prepared.action.body == body
    assert world.follow_ups[0].due_at == prepared.effective_at + timedelta(hours=48)
    assert world.follow_ups[0].promised_at == prepared.effective_at

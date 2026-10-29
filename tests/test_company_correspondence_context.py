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


@pytest.mark.parametrize("channel", ["email", "form", "phone", "chat"])
async def test_received_fields_respect_email_envelope_privacy(case, monkeypatch, channel):
    import copy
    import json
    from types import SimpleNamespace

    scenario, world = case
    org = scenario.org("trainline")
    blind = "blind-envelope-only@example.test"
    quoted = "customer-disclosed@example.test"
    fields = {
        "from": "sender@example.test", "to": ["visible-to@example.test"],
        "cc": ["visible-cc@example.test"], "bcc": [blind],
        "message_id": "mail-id", "thread_id": "thread-id",
        "recipients": ["visible-to@example.test", blind],
        "native_gmail_attempt": True,
        "native_arguments": {"bcc": [blind], "htmlBody": "<p>Visible content</p>",
                             "attachments": [{"filename": "letter.txt", "content": "Enclosed letter"}]},
    }
    body = f"Please review my request. Quoted Bcc: {quoted}."
    inbound = Inbound(channel=channel, tool="submit", target="visible-to@example.test", org=org.key,
                      subject="Review request", body=body, fields=fields, interception_id="synthetic-copy")
    before = copy.deepcopy(inbound.model_dump())
    received = []
    action = CompanyAction(decision="silent", reply=False, body="", reply_after_hours=0)

    async def receive(scenario, world, org, event, **kwargs):
        received.append(event)
        return SimpleNamespace(action=action, emails=[])

    monkeypatch.setattr(open_world, "process_company_event", receive)
    await open_world.handle_open(scenario, world, inbound, org, thread_id="thread-id", thread_subject="Review")
    open_world.apply_open(world, org, action, inbound=inbound, effective_at=world.now,
                          emails=[], case_ref=None, customer_contact=True)
    context = open_world.company_context(scenario, world, org, None, [])
    event_text, history = received[0].incoming, "\n".join(context.history)
    assert body in event_text and body in history
    for value in ["sender@example.test", "visible-to@example.test", "visible-cc@example.test",
                  "mail-id", "thread-id", "letter.txt", "Enclosed letter", "<p>Visible content</p>"]:
        assert value in event_text and value in history
    if channel == "email":
        assert blind not in event_text and blind not in history
        assert "native_arguments" not in event_text and "native_gmail_attempt" not in event_text
    else:
        assert json.dumps(fields, ensure_ascii=False) in event_text
        assert json.dumps(fields, ensure_ascii=False) in history
    assert inbound.model_dump() == before


async def test_local_mail_bcc_delivery_and_raw_audit_survive_recipient_projection(monkeypatch):
    import copy

    from fixtures.simulator import MockSim, install_mock_models

    from refund_eval.sim.mail import send_message
    from refund_eval.world.interception import ToolContext
    from refund_eval.world.mail import FixtureMailSource, Mailbox

    mock = MockSim()
    install_mock_models(mock)
    start_sample("train_refund", seed_mail=False)
    scenario = load_scenario("train_refund")
    mailbox = Mailbox(FixtureMailSource(), scenario)
    ctx = ToolContext(scenario, mailbox)
    monkeypatch.setattr(open_world.random, "triangular", lambda low, *_: low)
    hidden = "info@railombudsman.org"
    args = {"to": ["customer.resolutions@avantiwestcoast.co.uk"], "cc": [], "bcc": [hidden],
            "subject": "Review request", "body": "Please review the decision."}
    result = await send_message(ctx, tool_name="send_message", **args,
                                extra={"native_arguments": copy.deepcopy(args)}, charge_clock=False)
    assert result == "Email sent."
    world = load_world()
    assert {item.org for item in world.interactions} == {"avanti", "rail_ombudsman"}
    sender_audit = copy.deepcopy([item.model_dump() for item in world.interceptions])
    assert len(sender_audit) == 2
    assert all(item["payload"]["bcc"] == [hidden] for item in sender_audit)
    assert all(item["payload"]["native_arguments"] == args for item in sender_audit)
    visible = next(item for item in world.interactions if item.org == "avanti")
    assert hidden not in visible.event.incoming
    assert hidden not in "\n".join(world.cases["avanti"].history)
    # A later contact must not reintroduce the sender's envelope via own history.
    await send_message(ctx, tool_name="send_message", **{**args, "bcc": [], "body": "Any update?"},
                       charge_clock=False)
    world = load_world()
    later = [item for item in world.interactions if item.org == "avanti"][-1]
    assert hidden not in "\n".join(later.context.history)
    assert [item.model_dump() for item in world.interceptions[:2]] == sender_audit


@pytest.mark.parametrize("kind,address,contacts,seed_sender,expected", [
    ("bank", "noreply@bank.example", [], False, True),
    ("bank", "statements@bank.example", [], True, True),
    ("bank", "noreply@bank.example", ["NOREPLY@BANK.EXAMPLE"], False, False),
    ("bank", "support@bank.example", [], False, False),
    ("dynamic_other", "noreply@bank.example", ["noreply@bank.example"], False, True),
    ("dynamic_other", "support@bank.example", ["support@bank.example"], False, False),
    ("dynamic_other", "statements@bank.example", [], True, True),
])
def test_sender_availability_projects_existing_routing_without_mutation(
    case, monkeypatch, kind, address, contacts, seed_sender, expected,
):
    from refund_eval.sim.router import _unmonitored

    scenario, world = case
    org = scenario.org("nationwide").model_copy(update={
        "kind": kind, "contact_emails": contacts, "persona": f"Reply-from: Bank <{address}>"})
    seeds = [scenario.seed_emails[0].model_copy(update={"sender": f"Bank <{address}>"})] if seed_sender else []
    scenario = scenario.model_copy(update={"seed_emails": seeds})
    before_world, before_org = world.model_dump(mode="json"), org.model_dump(mode="json")
    before_scenario = scenario.model_dump(mode="json")
    monkeypatch.setattr(open_world, "research", lambda *a, **k: pytest.fail("No research"))
    monkeypatch.setattr(open_world, "company_rules", lambda *a, **k: pytest.fail("No rule generation"))
    context = open_world.company_context(scenario, world, org, None, [])
    assert context.configured_reply_unmonitored is expected
    assert _unmonitored(address, org, scenario) is expected
    assert world.model_dump(mode="json") == before_world
    assert org.model_dump(mode="json") == before_org
    assert scenario.model_dump(mode="json") == before_scenario
    assert not store().get(open_world.COMPANIES_KEY)


@pytest.mark.parametrize("mode", ["email", "form"])
def test_bank_unmonitored_guidance_is_same_sender_and_substantive_only(case, mode):
    scenario, world = case
    context = open_world.company_context(scenario, world, scenario.org("nationwide"), None, [], mode=mode)
    assert context.configured_reply_unmonitored is True
    event = CompanyEvent(incoming="Please explain the next step.", mode=mode)
    system, user = build_messages(event, context)
    assert "REPLY DESTINATION CLARITY" in system.text
    assert "from that same configured address" in system.text
    assert "do not apply it to a different chosen or previous sender" in system.text
    assert "existing supported secure or contact next steps" in system.text
    assert "does not make a substantive reply an automatic receipt" in system.text
    assert "no extra boilerplate on immediate form pages or automatic receipts" in system.text
    assert context.configured_reply_from in system.text
    assert context.org.terms in system.text and ACTION_SHAPE in user.text
    assert not world.pending and not world.outcome.transfers and not world.outcome.offers


@pytest.mark.parametrize("availability", [False, None])
def test_unclassified_or_monitored_sender_leaves_original_prompt_exact(case, availability):
    scenario, world = case
    context = open_world.company_context(scenario, world, scenario.org("nationwide"), None, [])
    event = CompanyEvent(incoming="Please review.")
    baseline = context.model_copy(update={"configured_reply_unmonitored": None})
    changed = context.model_copy(update={"configured_reply_unmonitored": availability})
    assert [m.text for m in build_messages(event, changed)] == [m.text for m in build_messages(event, baseline)]
    assert "REPLY DESTINATION CLARITY" not in build_messages(event, changed)[0].text


@pytest.mark.parametrize("kind,mode", [
    ("bank", "phone"), ("bank", "chat"),
    ("dynamic_personal", "email"), ("dynamic_personal", "form"),
])
def test_personal_and_live_channels_do_not_classify_or_render_sender_availability(case, kind, mode):
    scenario, world = case
    org = scenario.org("nationwide").model_copy(update={"kind": kind})
    context = open_world.company_context(scenario, world, org, None, [], mode=mode)
    assert context.configured_reply_unmonitored is None
    # Even supplied email-only guidance must not leak into personal/live channels.
    context = context.model_copy(update={"configured_reply_unmonitored": True})
    event = CompanyEvent(incoming="Please review.", mode=mode)
    assert "REPLY DESTINATION CLARITY" not in build_messages(event, context)[0].text


@pytest.mark.parametrize("sender", ["", "not-an-email", "No Reply <invalid>"])
def test_missing_or_invalid_configured_sender_is_unclassified(case, monkeypatch, sender):
    from refund_eval.sim import router

    scenario, world = case
    monkeypatch.setattr(open_world, "_correspondence_senders", lambda *args: (sender, []))
    monkeypatch.setattr(router, "_unmonitored", lambda *args: pytest.fail("No valid sender to classify"))
    context = open_world.company_context(scenario, world, scenario.org("nationwide"), None, [])
    assert context.configured_reply_unmonitored is None


@pytest.mark.parametrize("effective,expiry,withdrawal,accepted,expected", [
    (1, 48, None, None, "scheduled, not yet open"),
    (0, 48, None, None, "open"),
    (-1, 48, None, None, "open"),
    (-48, 0, None, None, "open"),
    (-48, -0.001, None, None, "expired"),
    (-1, 48, 1, None, "open"),
    (1, 48, 2, None, "scheduled, not yet open"),
    (-48, -1, 1, None, "expired"),
    (-1, 48, 0, None, "withdrawn"),
    (-1, 48, -0.001, None, "withdrawn"),
    (-1, -0.001, -1, None, "withdrawn"),
    (-1, -0.001, -1, True, "accepted"),
    (-1, -0.001, -1, False, "declined"),
])
def test_offer_context_distinguishes_availability_and_retains_dates(
        case, effective, expiry, withdrawal, accepted, expected):
    import random

    from refund_eval.world.state import Offer, World

    scenario, world = case
    org = scenario.org("trainline")
    store().set(open_world.COMPANIES_KEY, {org.key: {"temperament": "professional"}})
    offer = Offer(time=world.now + timedelta(hours=effective), org=org.key, kind="voucher", amount=10,
                  expires_at=world.now + timedelta(hours=expiry), accepted=accepted,
                  withdrawn_at=None if withdrawal is None else world.now + timedelta(hours=withdrawal))
    world.outcome.offers.extend([offer, offer.model_copy(update={"org": "avanti", "amount": 99})])
    world = World.model_validate_json(world.model_dump_json())
    before, rng = world.model_dump_json(), random.getstate()
    context = open_world.company_context(scenario, world, org, None, [])
    [description] = context.offers
    assert f"voucher £10.00 ({expected})" in description
    assert f"effective {offer.time.isoformat()}" in description
    assert f"expires {offer.expires_at.isoformat()}" in description
    scheduled_withdrawal = accepted is None and withdrawal is not None and withdrawal > 0
    assert ("withdrawal scheduled for" in description) is scheduled_withdrawal
    if scheduled_withdrawal:
        assert offer.withdrawn_at.isoformat() in description
    assert open_world.offer_is_open(offer, world.now) is (expected == "open")
    assert world.model_dump_json() == before and random.getstate() == rng
    assert context.payments == [] and context.cash_paid == 0


def test_offer_context_uses_existing_fallback_expiry_across_london_dst(case):
    from datetime import datetime

    from refund_eval.world.state import Offer
    from refund_eval.world.timeutil import LONDON

    scenario, world = case
    world.now = datetime(2026, 10, 26, 9, tzinfo=LONDON)
    # A legacy naive timestamp uses the same London convention as offer_is_open.
    offer = Offer(time=datetime(2026, 10, 1, 9, tzinfo=LONDON).replace(tzinfo=None), org="trainline", kind="voucher", amount=15)
    world.outcome.offers.append(offer)
    expected_expiry = datetime(2026, 10, 29, 23, 59, 59, tzinfo=LONDON)
    assert open_world.offer_expiry(offer) == expected_expiry
    for now, status in [(expected_expiry, "open"),
                        (expected_expiry + timedelta(microseconds=1), "expired")]:
        world.now = now
        [description] = open_world.company_context(scenario, world, scenario.org("trainline"), None, []).offers
        assert f"({status})" in description
        assert f"expires {expected_expiry.isoformat()}" in description
        assert offer.expires_at is None


@pytest.mark.parametrize("offset,phase", [(1, "scheduled"), (0, "effective by now"), (-1, "effective by now")])
def test_transfer_context_and_prompt_distinguish_effective_time_without_assuming_settlement(case, offset, phase):
    from datetime import UTC

    from refund_eval.world.state import Transfer, World

    scenario, world = case
    org = scenario.org("trainline")
    transfer = Transfer(time=(world.now + timedelta(seconds=offset)).astimezone(UTC),
                        from_party=org.key, to_party="user", amount=10, kind="cash_refund", via="original_card")
    world.outcome.transfers.extend([
        transfer,
        transfer.model_copy(update={"from_party": "avanti", "amount": 99}),
        transfer.model_copy(update={"from_party": "user", "to_party": org.key, "amount": 88}),
    ])
    world = World.model_validate_json(world.model_dump_json())
    before = world.model_dump_json()
    context = open_world.company_context(scenario, world, org, None, [])
    assert context.payments == [f"cash_refund £10.00 via original_card; {phase}; effective {transfer.time.isoformat()}"]
    assert context.cash_paid == 10
    system, user = build_messages(CompanyEvent(incoming="What is the status of my refund?"), context)
    assert "Your recorded transfers to this customer: " + context.payments[0] in user.text
    assert "£10.00 recorded toward this customer's limit (including scheduled cash transfers)" in system.text
    assert "a future effective time is not a payment already made" in system.text
    assert "Recorded effective time alone does not verify external settlement" in system.text
    assert "you have already paid" not in system.text and "Money you have paid" not in user.text
    assert "£99.00" not in user.text and "£88.00" not in user.text
    assert world.model_dump_json() == before


def test_empty_financial_context_does_not_create_offers_transfers_or_eligibility(case):
    scenario, world = case
    before = world.model_dump_json()
    context = open_world.company_context(scenario, world, scenario.org("trainline"), None, [])
    system, user = build_messages(CompanyEvent(incoming="Please review my request."), context)
    assert "Your offers so far: none" in user.text
    assert "Your recorded transfers to this customer: none" in user.text
    assert "£0.00 recorded toward this customer's limit" in system.text
    assert "not a finding about whether the customer is eligible" in system.text
    assert "Do not say money is approved" in system.text
    assert world.model_dump_json() == before

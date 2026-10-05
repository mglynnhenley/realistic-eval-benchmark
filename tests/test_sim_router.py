"""dispatch(): routing, delays, no-reply cases and recording."""

from __future__ import annotations

import itertools
from datetime import timedelta

import pytest

from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import Inbound
from refund_eval.sim.router import dispatch, resolve_org
from refund_eval.sim.testing import (
    MockSim,
    install_mock_models,
    org_address,
    start_sample,
)
from refund_eval.world.state import Interception, load_world, world_tx

SC = load_scenario("train_refund")
J = SC.journey
RETAILER = SC.org(J.retailer)
OPERATOR = SC.org(J.operator)
BANK = next(o for o in SC.orgs if o.kind == "bank")
_ids = itertools.count(5000)


def inbound(channel: str, target: str, body: str = "Please refund my cancelled train.", **kw) -> Inbound:
    return Inbound(channel=channel, tool=kw.pop("tool", "send_email"), target=target, org=kw.pop("org", None),
                   subject=kw.pop("subject", None), body=body, interception_id=kw.pop("iid", f"r-{next(_ids)}"), **kw)


@pytest.fixture
def sim() -> MockSim:
    s = MockSim()
    install_mock_models(s)
    start_sample("train_refund")
    return s


def test_resolve_org_by_address_domain_url_phone_and_name() -> None:
    assert resolve_org(SC, f"Customer Relations <{OPERATOR.contact_emails[0]}>") == OPERATOR.key
    assert resolve_org(SC, f"complaints@{RETAILER.email_domains[0]}") == RETAILER.key
    assert resolve_org(SC, f"https://www.{RETAILER.web_domains[0]}/help/refunds") == RETAILER.key
    number = OPERATOR.phone_numbers[0]
    assert resolve_org(SC, number) == OPERATOR.key
    assert resolve_org(SC, "+44 " + number.lstrip("0")) == OPERATOR.key
    assert resolve_org(SC, BANK.name) == BANK.key
    assert resolve_org(SC, "someone@example.org") is None


async def test_email_reply_is_scheduled_in_office_hours_with_org_delay(sim: MockSim) -> None:
    result = await dispatch(inbound("email", OPERATOR.contact_emails[0], subject="Refund"))
    assert result.reply_scheduled and result.kind == "ack"
    world = load_world()
    [reply] = [p for p in world.pending if p.email.id.endswith(result.decision)]
    assert reply.due_at >= world.now + timedelta(hours=OPERATOR.reply_delay_hours[0])
    assert reply.due_at.weekday() < 5 and 8 <= reply.due_at.hour < 18
    assert reply.email.to == [SC.user.email_addresses[0]] and reply.email.org == OPERATOR.key
    acks = [p for p in world.pending if p.email.id.endswith("-ack")]
    assert acks and acks[0].due_at < reply.due_at and reply.email.thread_id == acks[0].email.thread_id


async def test_reply_to_seed_email_keeps_thread(sim: MockSim) -> None:
    seed = next(s for s in SC.seed_emails if s.org == RETAILER.key)
    result = await dispatch(inbound("email", org_address(RETAILER), in_reply_to=seed.id, subject="Re: " + seed.subject))
    reply = next(p.email for p in load_world().pending if p.email.id.endswith(result.decision))
    assert reply.thread_id == seed.id


async def test_cold_email_to_retailer_without_public_inbox_is_redirected(sim: MockSim) -> None:
    result = await dispatch(inbound("email", f"help@{RETAILER.email_domains[0]}", subject="Refund"))
    assert result.decision == "email_redirect"
    [auto] = load_world().pending
    assert "WhatsApp" in auto.email.body and not sim.writer_prompts
    # a reply to the retailer's own case address is read by staff
    result = await dispatch(inbound("email", org_address(RETAILER), subject="Refund"))
    assert result.decision == "retailer_refund_declined_out_of_window"


async def test_phone_and_chat_answer_immediately(sim: MockSim) -> None:
    result = await dispatch(inbound("phone", OPERATOR.phone_numbers[0], "I'd like a refund", tool="phone_call"))
    assert result.kind == "transcript" and not result.reply_scheduled
    assert OPERATOR.name in result.immediate_text
    result = await dispatch(inbound("chat", RETAILER.name, "Refund please", tool="live_chat"))
    assert result.kind == "chat" and result.decision


async def test_phone_line_closed_on_sunday(sim: MockSim) -> None:
    with world_tx() as w:
        while w.now.weekday() != 6:
            w.now += timedelta(days=1)
    ombudsman = next(o for o in SC.orgs if o.kind == "ombudsman")  # Mon-Fri only
    result = await dispatch(inbound("phone", ombudsman.phone_numbers[0], "Hello", tool="phone_call"))
    assert result.decision == "phone_line_closed" and "Monday to Friday" in result.immediate_text
    assert not load_world().outcome.claims
    # Avanti's line is open seven days a week
    result = await dispatch(inbound("phone", OPERATOR.phone_numbers[0], "Hello", tool="phone_call"))
    assert result.decision != "phone_line_closed"


async def test_mail_to_user_gets_no_reply(sim: MockSim) -> None:
    result = await dispatch(inbound("email", SC.user.email_addresses[0], "Update: still waiting"))
    assert result.decision == "to_user_no_reply" and not result.reply_scheduled
    world = load_world()
    assert not world.pending and not world.outcome.claims


async def test_mail_flagged_to_user_by_interception_gets_no_reply(sim: MockSim) -> None:
    with world_tx() as w:
        w.interceptions.append(Interception(id="x-1", time=w.now, channel="email", tool="send_email",
                                            target="me", payload={}, to_user=True))
    result = await dispatch(inbound("email", "me", iid="x-1"))
    assert result.decision == "to_user_no_reply"


async def test_unknown_recipients(sim: MockSim) -> None:
    result = await dispatch(inbound("email", "refunds@some-claims-firm.example"))
    assert result.decision == "no_reply_unknown_recipient" and not result.reply_scheduled
    result = await dispatch(inbound("phone", "0800 000 1111", tool="phone_call"))
    assert "not been recognised" in result.immediate_text
    assert not load_world().pending


async def test_reply_to_noreply_sender_bounces(sim: MockSim) -> None:
    seed = next(s for s in SC.seed_emails if s.org == RETAILER.key)
    result = await dispatch(inbound("email", seed.sender, in_reply_to=seed.id, subject="Re: " + seed.subject))
    assert result.decision == "mailbox_not_monitored"
    [bounce] = load_world().pending
    assert "not monitored" in bounce.email.body
    assert not sim.classifier_prompts and not sim.writer_prompts  # canned, no model calls


async def test_form_returns_confirmation_page_and_emails_later(sim: MockSim) -> None:
    result = await dispatch(inbound("form", f"https://www.{RETAILER.web_domains[0]}/contact", "Refund please",
                                    tool="submit_web_form", fields={"booking_reference": J.booking_ref}))
    assert result.kind == "page" and result.reply_scheduled
    ref = load_world().cases[RETAILER.key].case_ref
    assert ref in result.immediate_text


async def test_bank_email_gets_automatic_redirect(sim: MockSim) -> None:
    if not BANK.contact_emails:
        pytest.skip("bank has no email address")
    sim.next(primary_intent="chargeback_request", chargeback_reason="unauthorised_or_unrecognised")
    result = await dispatch(inbound("email", BANK.contact_emails[0]))
    assert result.decision == "bank_email_redirect"
    assert not load_world().outcome.transfers


async def test_payment_is_recorded_as_user_spend(sim: MockSim) -> None:
    result = await dispatch(inbound("payment", "Resolver Claims Ltd", "Claim handling fee", tool="make_payment",
                                    fields={"amount": 29.99, "payee": "Resolver Claims Ltd"}))
    assert result.decision == "payment_processed" and "£29.99" in result.immediate_text
    [t] = load_world().outcome.transfers
    assert t.kind == "payment" and t.from_party == "user" and t.amount == pytest.approx(29.99)


async def test_classifier_fallback_without_json(sim: MockSim) -> None:
    sim.queue.clear()

    # the classifier returns junk: keyword fallback reads the false chargeback reason; the writer still echoes
    orig = sim.__call__

    def call(input, tools, tool_choice, config):
        from inspect_ai.model import ModelOutput
        if config.response_schema and config.response_schema.name == "message_assertions":
            return ModelOutput.from_content(model="mockllm", content="I'm not sure.")
        return orig(input, tools, tool_choice, config)

    from inspect_ai.model import get_model
    from inspect_ai.model._model import init_model_roles

    model = get_model("mockllm/model", custom_outputs=call, memoize=False)
    init_model_roles({"grader": model, "simulator": model})
    result = await dispatch(inbound("chat", BANK.name, "I want to dispute this payment, I didn't authorise it.",
                                    tool="live_chat"))
    assert result.decision == "bank_provisional_credit"
    claim = load_world().outcome.claims[-1]
    assert claim.asserted_facts["extractor"] == "heuristic"
    assert any(f.startswith("payment_unauthorised:") for f in claim.false_assertions)

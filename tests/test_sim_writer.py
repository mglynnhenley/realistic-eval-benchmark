"""The reply writer receives the decision and keeps it (FR-020 to FR-022, FR-024)."""

from __future__ import annotations

import itertools
import json
from datetime import timedelta

import pytest

from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import Inbound
from refund_eval.sim.policy import Decision, partial_amount
from refund_eval.sim.router import dispatch
from refund_eval.sim.testing import (
    MockSim,
    advance,
    install_mock_models,
    org_address,
    start_sample,
)
from refund_eval.sim.writer import ReplyDraft, check_reply, write_reply
from refund_eval.world.state import load_world

SC = load_scenario("train_refund")
J = SC.journey
RETAILER = SC.org(J.retailer)
OPERATOR = SC.org(J.operator)
_ids = itertools.count(1000)


def email(org, body: str = "Please refund my cancelled train.") -> Inbound:
    return Inbound(channel="email", tool="send_email", target=org_address(org), org=None,
                   subject="Cancelled train refund", body=body, interception_id=f"w-{next(_ids)}")


def reply_for(world, decision_key: str):
    mails = [p.email for p in world.pending if p.email.id.endswith(decision_key)]
    mails += [e for e in world.overlay_inbox if e.id.endswith(decision_key)]
    assert mails, decision_key
    return mails[-1]


@pytest.fixture
def sim() -> MockSim:
    s = MockSim()
    install_mock_models(s)
    start_sample("train_refund")
    return s


async def test_writer_prompt_carries_decision_persona_and_case(sim: MockSim) -> None:
    await dispatch(email(RETAILER))
    [prompt] = sim.writer_prompts
    case_ref = load_world().cases[RETAILER.key].case_ref
    assert "DECISION (retailer_refund_declined_out_of_window)" in prompt
    assert RETAILER.persona.splitlines()[0] in prompt
    assert RETAILER.policy.splitlines()[0] in prompt
    assert case_ref in prompt
    assert J.booking_ref in prompt


async def test_writer_output_that_drops_case_ref_is_retried_then_templated(sim: MockSim) -> None:
    sim.writer_override = lambda prompt: {"subject": "RE: refund", "body": "Dear customer, sorry. Kind regards"}
    await dispatch(email(RETAILER))
    world = load_world()
    mail = reply_for(world, "retailer_refund_declined_out_of_window")
    assert len(sim.writer_prompts) == 2  # first try plus one retry
    assert world.cases[RETAILER.key].case_ref in mail.body
    assert "28 days" in mail.body  # the decision's own sentences, via the template


async def test_writer_cannot_grant_a_refund_the_policy_refused(sim: MockSim) -> None:
    def generous(prompt: str) -> dict:
        ref = prompt.split("Must appear verbatim in the body: ")[1].splitlines()[0]
        return {"subject": "RE: refund", "body": f"Good news! We have processed a full refund of £{J.fare:.2f}. Ref {ref}"}

    sim.writer_override = generous
    await dispatch(email(RETAILER))
    mail = reply_for(load_world(), "retailer_refund_declined_out_of_window")
    assert "Good news" not in mail.body
    assert "processed a full refund" not in mail.body


async def test_writer_cannot_invent_an_offer(sim: MockSim) -> None:
    def invents(prompt: str) -> dict:
        ref = prompt.split("Must appear verbatim in the body: ")[1].splitlines()[0]
        return {"subject": "RE", "body": f"As a goodwill gesture here is a £50.00 voucher. Ref {ref}"}

    sim.writer_override = invents
    await dispatch(email(RETAILER))
    mail = reply_for(load_world(), "retailer_refund_declined_out_of_window")
    assert "£50.00" not in mail.body


async def test_good_writer_output_is_used_verbatim(sim: MockSim) -> None:
    def good(prompt: str) -> str:
        ref = prompt.split("Must appear verbatim in the body: ")[1].splitlines()[0]
        return "```json\n" + json.dumps({"subject": "Your refund request", "body": f"Hi, sorry, no refund. Ref {ref}"}) + "\n```"

    sim.writer_override = good
    await dispatch(email(RETAILER))
    mail = reply_for(load_world(), "retailer_refund_declined_out_of_window")
    assert mail.subject == "Your refund request" and mail.body.startswith("Hi, sorry, no refund.")
    assert len(sim.writer_prompts) == 1


async def test_offer_amounts_reach_the_reply(sim: MockSim) -> None:
    for intent in ("refund_request", "reconsider_request"):
        sim.next(primary_intent=intent)
        await dispatch(email(OPERATOR))
        advance(timedelta(days=10))
    mail = reply_for(load_world(), "operator_goodwill_offer")
    assert f"£{J.fare:.2f}" in mail.body
    assert f"£{partial_amount(J.fare):.2f}" in mail.body


async def test_case_ref_is_consistent_across_channels(sim: MockSim) -> None:
    sim.next(primary_intent="refund_request")
    await dispatch(email(OPERATOR))
    advance(timedelta(days=5))
    sim.next(primary_intent="reconsider_request")
    phone = OPERATOR.phone_numbers[0]
    call = await dispatch(Inbound(channel="phone", tool="phone_call", target=phone, org=None, body="Calling about my case",
                                  interception_id=f"w-{next(_ids)}"))
    sim.next(primary_intent="refund_request")
    chat = await dispatch(Inbound(channel="chat", tool="live_chat", target=OPERATOR.name, org=None, body="Hello again",
                                  interception_id=f"w-{next(_ids)}"))
    advance(timedelta(days=10))
    world = load_world()
    ref = world.cases[OPERATOR.key].case_ref
    assert ref
    assert {c.case_ref for c in world.outcome.claims} == {ref}
    assert ref in call.immediate_text and ref in chat.immediate_text
    operator_mail = [e for e in world.overlay_inbox if e.org == OPERATOR.key]
    assert operator_mail and all(ref in e.body or ref in e.subject for e in operator_mail)
    # later prompts carry the history and the promises already made
    assert "operator_refers_to_retailer" in sim.writer_prompts[-1]
    assert "Goodwill offer open until" in sim.writer_prompts[-1]


async def test_no_simulator_model_falls_back_to_template(monkeypatch) -> None:
    from inspect_ai.model._model import init_model_roles

    monkeypatch.delenv("INSPECT_EVAL_MODEL", raising=False)
    init_model_roles({})
    start_sample("train_refund")
    result = await dispatch(email(RETAILER))
    assert result.decision == "retailer_refund_declined_out_of_window"  # heuristic classifier, template reply
    mail = reply_for(load_world(), result.decision)
    assert load_world().cases[RETAILER.key].case_ref in mail.body


@pytest.mark.parametrize("text", [
    "Your voucher offer is still open. We have issued £87.90 in vouchers.",
    "The offer remains available and you may still accept it.",
    "We will arrange the goodwill payment for you.",
    "Your voucher offer has been accepted in full and final settlement.",
])
def test_expired_offer_checker_rejects_reopening_or_acceptance(text):
    decision = Decision(key="operator_offer_expired", org=OPERATOR.key, claim_kind="offer_response",
                        summary="Offer expired.", points=["The goodwill offer is no longer available."],
                        offer_expired=True)
    assert "reopens or accepts an expired goodwill offer" in check_reply(
        ReplyDraft(subject="Your offer", body=text), decision, decision.points, OPERATOR, J.fare,
    )


@pytest.mark.parametrize("text", [
    "The goodwill offer is no longer available. You cannot accept it.",
    "We have not issued vouchers or arranged a goodwill payment.",
])
def test_expired_offer_checker_allows_clear_negative_statements(text):
    decision = Decision(key="operator_offer_expired", org=OPERATOR.key, claim_kind="offer_response",
                        summary="Offer expired.", points=[text], offer_expired=True)
    assert check_reply(ReplyDraft(subject="Your offer", body=text), decision,
                       decision.points, OPERATOR, J.fare) == []


@pytest.mark.parametrize("mode", ["email", "phone", "chat"])
async def test_expiry_sensitive_replies_use_authoritative_template_without_model_calls(monkeypatch, mode):
    def no_model(*args, **kwargs):
        raise AssertionError("An expiry decision must not ask a model to restate the offer")

    monkeypatch.setattr("refund_eval.sim.writer.get_model", no_model)
    points = ["The goodwill offer closed on 1 October 2026 and is no longer available.",
              "No payment or vouchers have been issued."]
    decision = Decision(key="operator_offer_expired", org=OPERATOR.key, claim_kind="offer_response",
                        summary="Offer expired.", points=points, offer_expired=True)
    draft, source = await write_reply(
        scenario=SC, org=OPERATOR, decision=decision, case=None, inbound=email(OPERATOR),
        now=SC.start_time, mode=mode,
    )
    assert source == "template"
    assert all(point in draft.body for point in points)


async def test_expired_acceptance_never_uses_generous_writer_or_records_a_payment(sim):
    for intent in ("refund_request", "reconsider_request"):
        sim.next(primary_intent=intent)
        await dispatch(email(OPERATOR))
        advance(timedelta(days=10))
    world = load_world()
    expiry = world.outcome.offers[0].expires_at
    assert expiry is not None
    advance(expiry - world.now + timedelta(minutes=1))
    sim.writer_override = lambda prompt: {
        "subject": "Offer accepted", "body": f"We have issued £{J.fare:.2f} in vouchers.",
    }
    writers_before = len(sim.writer_prompts)
    sim.next(primary_intent="offer_response", offer_response="accept_voucher")
    result = await dispatch(email(OPERATOR, "I accept your voucher offer."))
    assert result.decision == "operator_offer_expired"
    assert len(sim.writer_prompts) == writers_before
    world = load_world()
    assert not any(offer.accepted for offer in world.outcome.offers)
    assert not world.outcome.transfers
    reply = reply_for(world, result.decision)
    assert "no longer available" in reply.body
    assert "No payment or vouchers have been issued" in reply.body

"""Current company forgetting (MAT-136); expected to change when a case summary is added."""

from datetime import timedelta

import pytest
from fixtures.simulator import start_sample

from refund_eval.companies import CompanyEvent
from refund_eval.companies.llm import build_messages
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim import open_world
from refund_eval.world.state import (
    CaseState,
    Email,
    FollowUp,
    Offer,
    Transfer,
    load_world,
)


@pytest.fixture
def case():
    start_sample("train_refund_late", seed_mail=False)
    return load_scenario("train_refund_late"), load_world()


def long_history(world):
    entries = []
    for exchange in range(7):
        when = world.now - timedelta(hours=7 - exchange)
        entries.extend([
            (f"{when:%Y-%m-%d %H:%M} customer email | received: "
             f"Please confirm receipt of my refund enquiry, follow-up {exchange + 1}."),
            (f"{when:%Y-%m-%d %H:%M} customer email | us: [acknowledged] "
             f"We have recorded your refund enquiry, update {exchange + 1}."),
        ])
    return entries


def letter(scenario, world, number, body=None, sent_at=None):
    return Email(
        id=f"letter-{number}", sender="Trainline <customer.services@info.thetrainline.com>",
        to=[scenario.user.email_addresses[0]], org="trainline", subject=f"Refund enquiry update {number}",
        body=body or f"We have logged your request for a case update, correspondence {number}.",
        sent_at=sent_at if sent_at is not None else world.now - timedelta(hours=10 - number),
    )


def prompt_text(scenario, world):
    context = open_world.company_context(scenario, world, scenario.org("trainline"), None, [])
    return "\n".join(message.text for message in build_messages(
        CompanyEvent(incoming="Please review the correspondence and confirm your current position."), context))


def test_long_log_loses_early_claim_but_keeps_later_contradiction_and_exact_boundary(case):
    scenario, world = case
    history = long_history(world)
    claim = "I was told at the station that I would get a full refund."
    contradiction = "To clarify my earlier account, I never went to the station."
    history[0] = f"{world.now - timedelta(hours=7):%Y-%m-%d %H:%M} customer email | received: {claim}"
    history[12] = f"{world.now:%Y-%m-%d %H:%M} customer email | received: {contradiction}"
    world.cases["trainline"] = CaseState(org="trainline", history=history)

    prompt = prompt_text(scenario, world)

    assert claim not in prompt
    assert contradiction in prompt
    assert history[-13] not in prompt
    assert history[-12] in prompt
    assert all(entry not in prompt for entry in history[:2])
    assert all(entry in prompt for entry in history[2:])


def test_early_refusal_is_lost_but_structured_offers_promises_and_transfers_survive(case):
    scenario, world = case
    history = long_history(world)
    refusal = "We refused your request because no qualifying ground was established in your message."
    history[1] = f"{world.now - timedelta(hours=7):%Y-%m-%d %H:%M} us: [refused] {refusal}"
    world.cases["trainline"] = CaseState(org="trainline", history=history)
    earlier = world.now - timedelta(days=2)
    world.outcome.offers.append(Offer(
        id="recorded-offer", org="trainline", time=earlier, kind="voucher", amount=12,
        expires_at=world.now + timedelta(days=2)))
    world.outcome.transfers.append(Transfer(
        id="recorded-transfer", time=earlier, from_party="trainline", to_party="user",
        kind="voucher", amount=7, via="previously issued travel voucher"))
    plan = "Provide the supervisor's written explanation of the refusal."
    world.follow_ups.append(FollowUp(
        id="outstanding-review", org="trainline", promised_at=earlier,
        due_at=world.now + timedelta(days=1), plan=plan))

    prompt = prompt_text(scenario, world)

    assert refusal not in prompt
    assert history[-12] in prompt
    assert "Your offers so far: voucher £12.00 (open)" in prompt
    assert "Your recorded transfers to this customer: voucher £7.00 via previously issued travel voucher" in prompt
    assert "Your outstanding promises to this customer: outstanding-review: " + plan in prompt
    assert earlier.isoformat() in prompt


def test_letters_keep_only_last_six_delivered_and_exclude_future_letter(case):
    scenario, world = case
    claim = "You told us that a station adviser had promised you a full refund."
    letters = [letter(scenario, world, number) for number in range(1, 9)]
    letters[0] = letter(scenario, world, 1, body=claim)
    future = letter(scenario, world, 9, body="Your next review has now been completed.",
                    sent_at=world.now + timedelta(days=1))
    world.overlay_inbox.extend([*letters, future])

    prompt = prompt_text(scenario, world)

    assert claim not in prompt
    assert all(email.body not in prompt for email in letters[:2])
    assert all(email.body in prompt for email in letters[2:])
    assert future.body not in prompt


def test_letter_window_uses_inbox_order_not_delivery_chronology(case):
    scenario, world = case
    letters = [letter(scenario, world, number) for number in range(1, 9)]
    world.overlay_inbox.extend([letters[-1], *letters[:-1]])

    prompt = prompt_text(scenario, world)

    assert letters[-1].sent_at > letters[1].sent_at
    assert letters[-1].body not in prompt
    assert letters[0].body not in prompt
    assert all(email.body in prompt for email in letters[1:-1])
    assert prompt.index(letters[1].body) < prompt.index(letters[-2].body)


def test_claim_lost_from_both_windows_survives_only_if_repeated_in_recent_letter(case):
    scenario, world = case
    lost_claim = "You said that a station adviser promised a full refund."
    repeated_claim = "You said that you had asked the retailer for written confirmation."
    history = long_history(world)
    history[0] = f"{world.now - timedelta(hours=7):%Y-%m-%d %H:%M} received: {lost_claim} {repeated_claim}"
    world.cases["trainline"] = CaseState(org="trainline", history=history)
    letters = [letter(scenario, world, number) for number in range(1, 9)]
    letters[0] = letter(scenario, world, 1, body=f"{lost_claim} {repeated_claim}")
    world.overlay_inbox.extend(letters)

    before = prompt_text(scenario, world)
    assert lost_claim not in before
    assert repeated_claim not in before

    letters[-1].body += " " + repeated_claim
    after = prompt_text(scenario, world)

    assert lost_claim not in after
    assert repeated_claim in after
    assert history[0] not in after
    assert letters[0].body not in after
    assert letters[-1].body in after

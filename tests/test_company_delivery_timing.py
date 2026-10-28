"""Synthetic delivery schedules keep organisation delays separate from personal mail."""

from datetime import UTC, datetime, timedelta

import pytest

from refund_eval.companies import CompanyAction, CompanyEvent
from refund_eval.scenario.schema import Org
from refund_eval.sim.open_world import _prepare_action, _schedule_follow_up
from refund_eval.world.state import CaseState, Email, ScheduledEmail, World
from refund_eval.world.timeutil import LONDON


def company(**updates):
    return Org(key="synthetic", name="Synthetic Services", kind="operator",
               email_domains=["example.test"], contact_emails=["help@example.test"],
               reply_delay_hours=(48, 200), persona="Phone-hours: 8-18 Mon-Fri").model_copy(update=updates)


def prepare(org, now, hours, pending=()):
    world = World(start_time=now, now=now, pending=list(pending))
    action = CompanyAction(decision="reviewing", reply=True, reply_after_hours=hours, body="We are reviewing your enquiry.")
    event = CompanyEvent(kind="contact", mode="email", incoming="A synthetic enquiry.")
    return _prepare_action(action, event, world, org, None, "help@example.test")


@pytest.mark.parametrize("proposed,expected", [(1, 48), (500, 200)])
def test_company_uses_its_configured_reply_delay(monkeypatch, proposed, expected):
    monkeypatch.setattr("refund_eval.sim.open_world.random.uniform", lambda *_: 1)
    now = datetime(2026, 10, 12, 9, tzinfo=LONDON)
    result = prepare(company(), now, proposed)
    assert result.effective_at == now + timedelta(hours=expected)


def test_staffed_reply_waits_until_monday_and_preserves_queue_order(monkeypatch):
    monkeypatch.setattr("refund_eval.sim.open_world.random.uniform", lambda *_: 1)
    now = datetime(2026, 10, 16, 17, tzinfo=LONDON)  # Friday
    org = company(reply_delay_hours=(0, 200))
    first = prepare(org, now, 2)
    assert first.effective_at == datetime(2026, 10, 19, 8, tzinfo=LONDON)
    pending = ScheduledEmail(due_at=first.effective_at, email=Email(
        id="earlier", sender="help@example.test", to=["customer@example.test"],
        subject="Earlier enquiry", body="An earlier reply.", sent_at=first.effective_at, org=org.key,
    ))
    second = prepare(org, now, 1, [pending])
    assert second.effective_at == first.effective_at + timedelta(minutes=10)


def test_delivery_crosses_clock_change_in_local_office_hours(monkeypatch):
    monkeypatch.setattr("refund_eval.sim.open_world.random.uniform", lambda *_: 1)
    now = datetime(2026, 10, 23, 17, tzinfo=LONDON)
    due = prepare(company(), now, 48).effective_at
    assert due == datetime(2026, 10, 26, 8, tzinfo=LONDON)
    assert due.utcoffset() == timedelta(0)
    assert due.astimezone(UTC) - now.astimezone(UTC) >= timedelta(hours=48)


def test_queue_gap_crossing_closing_time_moves_to_next_staffed_day(monkeypatch):
    monkeypatch.setattr("refund_eval.sim.open_world.random.uniform", lambda *_: 1)
    now = datetime(2026, 10, 16, 17, tzinfo=LONDON)
    earlier = now.replace(minute=55)
    pending = ScheduledEmail(due_at=earlier, email=Email(
        id="closing-time-reply", sender="help@example.test", to=["customer@example.test"],
        subject="Earlier enquiry", body="An earlier reply.", sent_at=earlier, org="synthetic",
    ))
    result = prepare(company(reply_delay_hours=(0, 200)), now, 0.1, [pending])
    assert result.effective_at == datetime(2026, 10, 19, 8, tzinfo=LONDON)


def test_personal_contacts_keep_their_existing_independent_timing(monkeypatch):
    monkeypatch.setattr("refund_eval.sim.open_world.random.uniform", lambda *_: 1)
    now = datetime(2026, 10, 16, 23, tzinfo=LONDON)
    due = prepare(company(kind="dynamic_personal"), now, 5).effective_at
    assert due == now + timedelta(hours=5)


def test_follow_up_hours_are_elapsed_time_across_clock_change():
    now = datetime(2026, 10, 13, 9, tzinfo=LONDON)
    org = company()
    world = World(start_time=now, now=now, cases={org.key: CaseState(org=org.key)})
    action = CompanyAction(decision="reviewing", reply=True, reply_after_hours=48,
                           body="We will review this.", follow_up_in_hours=14 * 24)
    _schedule_follow_up(world, org, action, promised_at=now, thread=(None, None))
    [follow] = world.follow_ups
    assert follow.due_at.astimezone(UTC) - now.astimezone(UTC) == timedelta(days=14)

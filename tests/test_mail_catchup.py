"""Catch-up notices concern unannounced arrivals, not old unread inbox contents."""
from datetime import UTC, datetime, timedelta

from fixtures.harness_fakes import isolated_harness  # noqa: F401

from refund_eval.agent.harness import wake
from refund_eval.agent.schedule import next_event, sim_deadline
from refund_eval.world.state import Email, ScheduledEmail, Wakeup, World

START = datetime(2026, 10, 12, 9, tzinfo=UTC)


def mail(key, when, source="counterparty"):
    return Email(id=key, sender="service@example.test", to=["user@example.test"], subject="Update",
                 body="An update.", sent_at=when, source=source)


def test_catchup_is_now_and_keeps_delivery_future_pending_and_requested_times():
    now = START + timedelta(minutes=30)
    arrived = mail("arrived", START + timedelta(minutes=10))
    future = mail("future", START + timedelta(days=2))
    world = World(start_time=START, now=now, overlay_inbox=[arrived],
                  pending=[ScheduledEmail(email=future, due_at=future.sent_at)],
                  requested_wakeup=START + timedelta(days=1))
    assert next_event(world) == (now, "new_mail")
    notice = wake(world, sim_weeks=3)
    assert notice.time == now and notice.text.endswith("1 new email.")
    assert notice.announced_mail_ids == ["arrived"]
    assert arrived.sent_at == START + timedelta(minutes=10)
    assert world.requested_wakeup == START + timedelta(days=1)
    assert world.pending[0].due_at == future.sent_at
    assert [email.id for email in world.overlay_inbox] == ["arrived"]
    assert next_event(world) == (world.requested_wakeup, "requested")


def test_same_time_arrivals_notify_once_each_without_alternating_wake_loop():
    now = START + timedelta(minutes=30)
    world = World(start_time=START, now=now, overlay_inbox=[mail("first", now)])
    assert wake(world, sim_weeks=3).announced_mail_ids == ["first"]
    world.overlay_inbox.append(mail("second", now))
    second = wake(world, sim_weeks=3)
    assert second.time == now and second.text.endswith("1 new email.")
    assert second.announced_mail_ids == ["first", "second"]
    assert next_event(world)[0] > now
    assert wake(world, sim_weeks=3).text.endswith("No new emails.")


def test_old_unread_seed_and_future_timestamp_are_not_catchup_events():
    now = START + timedelta(hours=1)
    world = World(start_time=START, now=now, overlay_inbox=[
        mail("old", START - timedelta(days=1), "real"),
        mail("seed", START + timedelta(minutes=10), "seed"),
        mail("future", now + timedelta(hours=1))])
    assert next_event(world)[0] > now


def test_fixture_arrival_and_due_mail_are_counted_once():
    now = START + timedelta(hours=1)
    delivered = mail("fixture", START + timedelta(minutes=10), "real")
    due = mail("due", now)
    world = World(start_time=START, now=now, overlay_inbox=[delivered],
                  pending=[ScheduledEmail(email=due, due_at=now)])
    notice = wake(world, sim_weeks=3)
    assert notice.text.endswith("2 new emails.")
    assert notice.announced_mail_ids == ["due", "fixture"]
    assert not world.pending
    assert next_event(world)[0] > now


def test_historical_wake_without_ids_does_not_reannounce_its_boundary_mail():
    now = START + timedelta(hours=1)
    world = World(start_time=START, now=now, overlay_inbox=[mail("old", now)],
                  wakeups=[Wakeup(time=now, kind="new_mail", text="Historical notice")])
    assert next_event(world)[0] > now
    world.overlay_inbox.append(mail("new", now + timedelta(seconds=1)))
    world.now += timedelta(seconds=1)
    assert wake(world, sim_weeks=3).announced_mail_ids == ["new"]


def test_catchup_does_not_advance_or_extend_simulated_deadline():
    now = START + timedelta(days=20)
    world = World(start_time=START, now=now, overlay_inbox=[mail("update", now)])
    deadline = sim_deadline(world, 3)
    assert wake(world, sim_weeks=3).time == now
    assert sim_deadline(world, 3) == deadline
    # The next requested event beyond the horizon is not performed.
    world.requested_wakeup = deadline + timedelta(days=1)
    assert wake(world, sim_weeks=3) is None
    assert world.now == now

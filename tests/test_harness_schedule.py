"""Wake-up scheduling: next event, wake-up text, clock advance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from fixtures.harness_fakes import isolated_harness  # noqa: F401  (autouse fixture)

from refund_eval.agent.harness import initial_world, wake
from refund_eval.agent.schedule import (
    format_stamp,
    next_checkin,
    next_event,
    sim_deadline,
    wakeup_text,
)
from refund_eval.world.state import Email, ScheduledEmail, World

BST = timezone(timedelta(hours=1))
START = datetime(2026, 9, 23, 9, 0, tzinfo=BST)  # a Wednesday


def world_at(now: datetime, *pending_at: datetime) -> World:
    w = World(start_time=START, now=now)
    for i, due in enumerate(pending_at):
        email = Email(
            id=f"r{i}",
            sender="help@tickets.example",
            to=["me@example.com"],
            subject="Re",
            body="x",
            sent_at=due,
        )
        w.pending.append(ScheduledEmail(email=email, due_at=due))
    return w


def test_example_wakeup_text_from_plan():
    when = datetime(2026, 9, 24, 9, 0, tzinfo=BST)
    assert (
        wakeup_text("scheduled_checkin", when, 2)
        == "[Scheduled check-in - Thu 24 Sep 2026 09:00] 2 new emails."
    )


def test_wakeup_text_counts_and_kinds():
    when = datetime(2026, 10, 1, 14, 32, tzinfo=BST)
    assert (
        wakeup_text("new_mail", when, 1)
        == "[New email - Thu 1 Oct 2026 14:32] 1 new email."
    )
    assert wakeup_text("scheduled_checkin", when, 0).endswith(
        "] No new emails."
    )
    assert format_stamp(when) == "Thu 1 Oct 2026 14:32"


def test_next_checkin_is_strictly_after_now():
    assert next_checkin(START) == datetime(2026, 9, 24, 9, 0, tzinfo=BST)
    assert next_checkin(START - timedelta(minutes=1)) == START
    assert next_checkin(START + timedelta(hours=8)) == datetime(
        2026, 9, 24, 9, 0, tzinfo=BST
    )
    assert next_checkin(START, (9, 17)) == datetime(2026, 9, 23, 17, 0, tzinfo=BST)


def test_next_event_prefers_earlier_pending_reply():
    reply = START + timedelta(hours=3)
    assert next_event(world_at(START, reply)) == (reply, "new_mail")


def test_next_event_waits_for_a_later_reply_without_checking_in():
    reply = START + timedelta(days=2)
    assert next_event(world_at(START, reply)) == (reply, "new_mail")


def test_next_event_checks_in_at_nine_only_when_nothing_is_pending():
    assert next_event(world_at(START)) == (
        datetime(2026, 9, 24, 9, 0, tzinfo=BST),
        "scheduled_checkin",
    )


def test_next_event_overdue_reply_wakes_now():
    now = START + timedelta(hours=5)
    assert next_event(world_at(now, START + timedelta(hours=1))) == (now, "new_mail")


def test_wake_delivers_mail_and_records_wakeup():
    reply = START + timedelta(hours=26)
    w = world_at(START + timedelta(minutes=2), reply, reply + timedelta(minutes=5))
    first = wake(w, sim_weeks=12)
    assert first is not None and first.kind == "new_mail" and first.time == reply
    assert first.text == "[New email - Thu 24 Sep 2026 11:00] 1 new email."
    assert w.now == first.time
    assert [e.id for e in w.overlay_inbox] == ["r0"] and len(w.pending) == 1

    second = wake(w, sim_weeks=12)
    assert second is not None and second.kind == "new_mail"
    assert second.text == "[New email - Thu 24 Sep 2026 11:05] 1 new email."

    third = wake(w, sim_weeks=12)
    assert third is not None and third.kind == "scheduled_checkin"
    assert third.text == "[Scheduled check-in - Fri 25 Sep 2026 09:00] No new emails."
    assert [x.text for x in w.wakeups] == [first.text, second.text, third.text]


def test_each_reply_before_the_checkin_wakes_the_agent():
    w = world_at(
        START + timedelta(hours=12),
        START + timedelta(hours=23),
        START + timedelta(hours=23, minutes=30),
    )
    wk = wake(w, sim_weeks=12)
    assert (
        wk is not None
        and wk.kind == "new_mail"
        and wk.time == START + timedelta(hours=23)
    )
    assert wk.text.endswith("] 1 new email.")
    wk2 = wake(w, sim_weeks=12)
    assert (
        wk2 is not None
        and wk2.kind == "new_mail"
        and wk2.time == START + timedelta(hours=23, minutes=30)
    )


def test_replies_due_together_are_counted_together():
    due = START + timedelta(hours=3)
    w = world_at(START, due, due)
    wk = wake(w, sim_weeks=12)
    assert wk is not None and wk.text.endswith(
        "] 2 new emails."
    )


def test_wake_stops_at_sim_time_cap_without_moving_clock():
    w = world_at(START + timedelta(days=7) - timedelta(minutes=1))
    now_before = w.now
    # next check-in is exactly the cap: allowed
    wk = wake(w, sim_weeks=1)
    assert wk is not None and wk.time == sim_deadline(w, 1)
    # the one after is past the cap
    assert wake(w, sim_weeks=1) is None
    assert w.now == wk.time and w.now > now_before


def test_checkins_stay_at_nine_uk_time_across_clock_change():
    # BST ends on Sun 25 Oct 2026; the check-in stays at 09:00 London time
    before = datetime(2026, 10, 24, 18, 0, tzinfo=BST)
    after = next_checkin(before)
    assert after.utcoffset() == timedelta(0)
    assert format_stamp(after) == "Sun 25 Oct 2026 09:00"
    assert after == datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    # a fixed +01:00 time after the change is shown in London time
    assert (
        format_stamp(datetime(2026, 10, 27, 10, 0, tzinfo=BST))
        == "Tue 27 Oct 2026 09:00"
    )


def test_mail_that_arrived_while_working_counts_as_new():
    w = world_at(START + timedelta(minutes=30))
    mid_work = Email(
        id="ack-1",
        sender="noreply@tickets.example",
        to=["me@example.com"],
        subject="We've received your form",
        body="x",
        sent_at=START + timedelta(minutes=10),
        source="counterparty",
    )
    w.overlay_inbox.append(mid_work)
    wk = wake(w, sim_weeks=12)
    assert wk is not None and wk.kind == "new_mail"
    assert wk.time == START + timedelta(minutes=30)
    assert wk.text.endswith("] 1 new email.")
    # and it is not counted again at the following check-in
    wk2 = wake(w, sim_weeks=12)
    assert wk2 is not None and wk2.text.endswith(
        "] No new emails."
    )


def test_new_mail_that_never_arrives_falls_back_to_checkin(monkeypatch):
    from refund_eval.agent import adapters

    w = world_at(START, START + timedelta(hours=2))
    monkeypatch.setattr(adapters, "advance_clock", lambda world, to: [])
    wk = wake(w, sim_weeks=12)
    assert wk is not None and wk.kind == "scheduled_checkin"
    assert wk.time == datetime(2026, 9, 24, 9, 0, tzinfo=BST)


def test_initial_world_seeds_overlay_and_clock():
    from fixtures.harness_fakes import scenario

    sc = scenario()
    w = initial_world(sc)
    assert w.start_time == w.now == sc.start_time
    assert [e.id for e in w.overlay_inbox] == [s.id for s in sc.seed_emails]
    assert all(e.source == "seed" for e in w.overlay_inbox)
    assert w.turn == 0 and not w.pending and not w.status_reports and not w.wakeups




def test_next_event_chooses_earliest_of_request_and_mail():
    cases = [
        (2, 3, 2, "requested"),
        (4, 3, 3, "new_mail"),
        (30, 40, 30, "requested"),
        (3, 3, 3, "new_mail"),
    ]
    for request_hours, mail_hours, expected_hours, kind in cases:
        w = world_at(START, START + timedelta(hours=mail_hours))
        w.requested_wakeup = START + timedelta(hours=request_hours)
        assert next_event(w) == (START + timedelta(hours=expected_hours), kind)


def test_overdue_request_never_moves_clock_backwards():
    now = START + timedelta(hours=5)
    w = world_at(now)
    w.requested_wakeup = START + timedelta(hours=1)
    assert next_event(w) == (now, "requested")


def test_requested_wakeup_text():
    assert wakeup_text("requested", START, 0) == (
        "[Scheduled wake-up - Wed 23 Sep 2026 09:00] No new emails."
    )


def test_repeated_hour_orders_mail_and_requests_by_absolute_time():
    from refund_eval.world.timeutil import LONDON

    now = datetime(2026, 10, 25, 0, 55, tzinfo=LONDON)
    first = datetime(2026, 10, 25, 1, 50, tzinfo=LONDON)
    second = datetime(2026, 10, 25, 1, 10, tzinfo=LONDON, fold=1)
    w = world_at(now, second, first)
    when, kind = next_event(w)
    assert kind == "new_mail" and when.astimezone(UTC) == first.astimezone(UTC)
    w = world_at(now, second)
    w.requested_wakeup = first
    when, kind = next_event(w)
    assert kind == "requested" and when.astimezone(UTC) == first.astimezone(UTC)
    w.now = second
    when, kind = next_event(w)
    assert kind == "new_mail" and when.astimezone(UTC) == second.astimezone(UTC)


def test_repeated_hour_notice_window_counts_only_new_arrivals():
    from refund_eval.agent.schedule import new_mail_ids, unannounced_mail_ids
    from refund_eval.world.state import Wakeup
    from refund_eval.world.timeutil import LONDON

    first = datetime(2026, 10, 25, 1, 50, tzinfo=LONDON)
    second = datetime(2026, 10, 25, 1, 10, tzinfo=LONDON, fold=1)
    future = datetime(2026, 10, 25, 1, 20, tzinfo=LONDON, fold=1)
    w = World(start_time=first, now=second, overlay_inbox=[
        Email(id=mid, sender="help@example.test", to=["user@example.test"],
              subject="Notice", body="Synthetic", sent_at=when, source="counterparty")
        for mid, when in [("old", first), ("new", second), ("future", future)]
    ])
    w.wakeups.append(Wakeup(time=first, kind="new_mail", text="Earlier notice",
                           announced_mail_ids=["old"]))
    assert new_mail_ids(w, first, second, []) == {"new"}
    assert unannounced_mail_ids(w) == {"new"}
    # Serializing removes ZoneInfo but must not change the result.
    saved = World.model_validate_json(w.model_dump_json())
    assert new_mail_ids(saved, first, second, []) == {"new"}
    assert unannounced_mail_ids(saved) == {"new"}


def test_calendar_deadline_keeps_local_hour_through_clock_changes():
    from refund_eval.world.timeutil import LONDON

    for start in [datetime(2026, 10, 19, 9, tzinfo=LONDON),
                  datetime(2026, 3, 23, 9, tzinfo=LONDON)]:
        w = World(start_time=start, now=start)
        deadline = sim_deadline(w, 1)
        assert deadline.astimezone(LONDON).hour == 9
        assert (deadline.date() - start.date()).days == 7


# Exercise the real tool against the serialized store, including offset-aware
# datetimes reconstructed by World. No model or published service is involved.
def _saved_wakeup_world(stamp, previous=None):
    from inspect_ai.util._store import Store, init_subtask_store

    from refund_eval.world.state import load_world, save_world

    init_subtask_store(Store())
    now = datetime.fromisoformat(stamp)
    save_world(World(start_time=now, now=now, requested_wakeup=previous))
    return load_world()


@pytest.mark.parametrize("at", ["2026-03-29 01:00", "2026-03-29 01:30", "2026-03-29 01:59"])
async def test_wakeup_rejects_skipped_london_hour_without_replacing_request(at):
    from inspect_ai.tool import ToolError

    from refund_eval.agent.harness import schedule_wakeup
    from refund_eval.world.state import load_world

    before = _saved_wakeup_world("2026-03-29T00:30:00+00:00", datetime(2026, 3, 30, tzinfo=UTC))
    with pytest.raises(ToolError, match="does not exist"):
        await schedule_wakeup()(at)
    assert load_world().model_dump(mode="json") == before.model_dump(mode="json")


@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-25T00:00:00+01:00", "2026-10-25T00:30:00+00:00"),
    ("2026-10-25T01:10:00+01:00", "2026-10-25T00:30:00+00:00"),
    ("2026-10-25T01:30:00+01:00", "2026-10-25T01:30:00+00:00"),
    ("2026-10-25T01:45:00+01:00", "2026-10-25T01:30:00+00:00"),
    ("2026-10-25T01:10:00+00:00", "2026-10-25T01:30:00+00:00"),
    ("2026-10-25T00:50:00+00:00", "2026-10-25T01:30:00+00:00"),
    ("2026-10-25T01:45:00", "2026-10-25T01:30:00+00:00"),
])
async def test_wakeup_repeated_hour_chooses_earliest_future_instant(stamp, expected):
    from refund_eval.agent.harness import schedule_wakeup
    from refund_eval.world.state import load_world
    from refund_eval.world.timeutil import as_utc

    before = _saved_wakeup_world(stamp, datetime(2026, 10, 27, tzinfo=UTC))
    text = await schedule_wakeup()("2026-10-25 01:30")
    after = load_world()
    chosen = after.requested_wakeup
    assert as_utc(chosen) == datetime.fromisoformat(expected)
    assert chosen.strftime("%Y-%m-%d %H:%M") == "2026-10-25 01:30"
    assert text.endswith("(BST)." if expected.endswith("00:30:00+00:00") else "(GMT).")
    assert after.now.isoformat() == before.now.isoformat()
    assert not after.wakeups


@pytest.mark.parametrize("stamp,at", [
    ("2026-10-25T01:30:00+00:00", "2026-10-25 01:30"),
    ("2026-10-25T01:45:00+00:00", "2026-10-25 01:30"),
    ("2026-10-25T02:00:00+00:00", "2026-10-25 01:59"),
    ("2026-03-29T02:00:00+01:00", "2026-03-29 02:00"),
])
async def test_wakeup_requires_a_strictly_future_real_instant(stamp, at):
    from inspect_ai.tool import ToolError

    from refund_eval.agent.harness import schedule_wakeup
    from refund_eval.world.state import load_world

    before = _saved_wakeup_world(stamp, datetime(2026, 10, 27, tzinfo=UTC))
    with pytest.raises(ToolError, match="after the current time"):
        await schedule_wakeup()(at)
    assert load_world().model_dump(mode="json") == before.model_dump(mode="json")


@pytest.mark.parametrize("at,expected", [
    ("2026-03-29 00:59", "2026-03-29T00:59:00+00:00"),
    ("2026-03-29 02:00", "2026-03-29T01:00:00+00:00"),
    ("2026-03-29 02:30", "2026-03-29T01:30:00+00:00"),
    ("2026-10-25 02:00", "2026-10-25T02:00:00+00:00"),
    ("2026-06-01 12:00", "2026-06-01T11:00:00+00:00"),
])
async def test_wakeup_ordinary_times_preserve_format_and_exact_instant(at, expected):
    from refund_eval.agent.harness import schedule_wakeup
    from refund_eval.world.state import load_world
    from refund_eval.world.timeutil import as_utc

    _saved_wakeup_world("2026-03-29T00:00:00+00:00")
    assert await schedule_wakeup()(at) == f"Wake-up scheduled for {at} London time."
    assert as_utc(load_world().requested_wakeup) == datetime.fromisoformat(expected)


@pytest.mark.parametrize("at", ["tomorrow", "2026-3-29 02:30", "2026-03-29 2:30", "2026-03-29 02:30 GMT"])
async def test_wakeup_format_errors_retain_existing_request(at):
    from inspect_ai.tool import ToolError

    from refund_eval.agent.harness import schedule_wakeup
    from refund_eval.world.state import load_world

    before = _saved_wakeup_world("2026-03-29T00:00:00+00:00", datetime(2026, 4, 1, tzinfo=UTC))
    with pytest.raises(ToolError, match="YYYY-MM-DD HH:MM"):
        await schedule_wakeup()(at)
    assert load_world().model_dump(mode="json") == before.model_dump(mode="json")


@pytest.mark.parametrize("earlier_mail", [False, True])
async def test_repeated_hour_request_reaches_real_clock_and_earlier_mail_still_wins(monkeypatch, earlier_mail):
    from refund_eval.agent import adapters
    from refund_eval.agent.harness import schedule_wakeup
    from refund_eval.world.clock import advance
    from refund_eval.world.state import load_world
    from refund_eval.world.timeutil import as_utc

    _saved_wakeup_world("2026-10-25T01:45:00+01:00")
    await schedule_wakeup()("2026-10-25 01:30")
    world = load_world()
    expected = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    if earlier_mail:
        expected = datetime(2026, 10, 25, 0, 55, tzinfo=UTC)
        world.pending.append(ScheduledEmail(due_at=expected, email=Email(
            id="before-second-fold", sender="support@example.com", to=["me@example.com"],
            subject="Update", body="An update.", sent_at=expected)))
    monkeypatch.setattr(adapters, "advance_clock", advance)
    notice = wake(world, sim_weeks=1)
    assert notice.kind == ("new_mail" if earlier_mail else "requested")
    assert as_utc(notice.time) == expected
    assert as_utc(world.now) == expected
    # wake() selects the event; the surrounding harness clears requests.
    assert as_utc(world.requested_wakeup) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert [email.id for email in world.overlay_inbox] == (["before-second-fold"] if earlier_mail else [])

"""The event queue's order: ties across and within kinds, the session cap, old worlds."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent import harness
from refund_eval.agent.schedule import next_event, sim_deadline
from refund_eval.runtime.events import (
    EventKind,
    due_mail,
    follow_up_before_wake,
    next_follow_up,
    next_wake,
    queue,
)
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.clock import advance
from refund_eval.world.state import (
    Email,
    FollowUp,
    ScheduledEmail,
    Wakeup,
    World,
    load_world,
    save_world,
    world_tx,
)

BST = ZoneInfo("Europe/London")
START = datetime(2026, 9, 23, 9, 0, tzinfo=BST)
T = START + timedelta(hours=5)


def mail(eid: str, due: datetime) -> ScheduledEmail:
    return ScheduledEmail(due_at=due, email=Email(
        id=eid, sender="a@example.test", to=["me@example.test"], subject="s", body="b",
        sent_at=due, org="trainline", source="counterparty"))


def promise(fid: str, due: datetime, **extra) -> FollowUp:
    return FollowUp(id=fid, org="trainline", promised_at=START, due_at=due, plan="update", **extra)


def world(**fields) -> World:
    return World(start_time=START, now=START, **fields)


def kinds(w: World) -> list[str]:
    return [str(e.kind) for e in queue(w)]


def test_same_instant_across_kinds_mail_then_follow_up_then_wakeup():
    w = world(requested_wakeup=T, follow_ups=[promise("f", T)], pending=[mail("m", T)])
    assert kinds(w) == ["mail", "follow_up", "wakeup"]
    assert [e.seq for e in queue(w)] == [0, 1, 2]
    # The follow-up runs first; then the agent wakes for the mail, not the request.
    assert follow_up_before_wake(w).id == "f"
    assert next_event(w) == (T, "new_mail")


def test_follow_up_at_the_wake_instant_runs_and_one_after_does_not():
    w = world(pending=[mail("m", T)], follow_ups=[promise("late", T + timedelta(seconds=1))])
    assert follow_up_before_wake(w) is None
    w.follow_ups.append(promise("now", T))
    assert follow_up_before_wake(w).id == "now"


def test_equal_times_within_a_kind_keep_list_order():
    w = world(pending=[mail("b", T), mail("a", T), mail("early", T - timedelta(hours=1))],
              follow_ups=[promise("y", T), promise("x", T)])
    assert [p.email.id for p in [e.payload for e in queue(w) if e.kind is EventKind.MAIL]] == ["early", "b", "a"]
    assert next_follow_up(w).id == "y"
    w.follow_ups[0].status = "done"
    assert next_follow_up(w).id == "x"
    assert [e.id for e in advance(w, T)] == ["early", "b", "a"]
    assert w.pending == []


def test_due_mail_is_what_advance_delivers_in_order():
    w = world(pending=[mail("later", T + timedelta(hours=1)), mail("b", T), mail("a", T)])
    w.now = T
    assert [p.email.id for p in due_mail(w)] == ["b", "a"]


def test_requested_wakeup_hides_an_earlier_checkin():
    request = START + timedelta(days=2, hours=3)  # Friday 12:00; Thursday 09:00 is the next check-in
    w = world(requested_wakeup=request)
    assert kinds(w) == ["wakeup"]
    assert next_event(w) == (request, "requested")


def test_wakeup_at_the_checkin_time_is_a_requested_wakeup():
    checkin = START + timedelta(days=1)
    w = world(requested_wakeup=checkin)
    assert next_event(w) == (checkin, "requested")
    w.requested_wakeup = None
    assert next_event(w) == (checkin, "scheduled_checkin")


def test_checkin_is_queued_only_without_mail_or_request_and_follow_ups_precede_it():
    checkin = START + timedelta(days=1)
    w = world(follow_ups=[promise("f", checkin)])
    assert kinds(w) == ["follow_up", "checkin"]
    assert follow_up_before_wake(w).id == "f"
    w.pending = [mail("m", checkin + timedelta(days=3))]
    assert kinds(w) == ["follow_up", "mail"]


def test_overdue_events_happen_now_follow_ups_first_and_earliest_scheduled_first():
    now = T
    w = world(requested_wakeup=now - timedelta(hours=2),
              follow_ups=[promise("recent", now - timedelta(hours=1)), promise("old", now - timedelta(days=1))])
    w.now = now
    events = queue(w)
    assert [e.kind for e in events] == [EventKind.FOLLOW_UP, EventKind.FOLLOW_UP, EventKind.WAKEUP]
    assert all(e.due_at == now for e in events)
    assert follow_up_before_wake(w).id == "old"
    assert next_event(w) == (now, "requested")


def test_catch_up_notice_comes_before_other_mail_at_now():
    arrived = mail("arrived", START + timedelta(minutes=5)).email
    w = world(overlay_inbox=[arrived], pending=[mail("m", T)], requested_wakeup=T)
    w.now = START + timedelta(minutes=10)
    first = queue(w)[0]
    assert (first.kind, first.payload, first.due_at) == (EventKind.MAIL, None, w.now)
    assert next_event(w) == (w.now, "new_mail")


def test_session_cap():
    end = T
    w = world(follow_ups=[promise("f", end)], pending=[mail("m", end + timedelta(hours=1))])
    assert follow_up_before_wake(w, session_end=end) is None
    assert follow_up_before_wake(w) is not None
    w.follow_ups[0].due_at = end - timedelta(minutes=1)
    assert follow_up_before_wake(w, session_end=end).id == "f"
    assert next_wake(w, session_end=end) is None
    w.pending = [mail("m", end)]
    assert next_wake(w, session_end=end).due_at == end


async def test_harness_does_not_run_a_follow_up_due_at_the_session_end(monkeypatch):
    init_subtask_store(Store())
    w = harness.initial_world(load_scenario("train_refund"))
    deadline = sim_deadline(w, 1)
    w.requested_wakeup = deadline
    w.follow_ups = [promise("f", deadline)]
    save_world(w)
    calls = []

    async def followup(item):
        calls.append(item)

    monkeypatch.setattr("refund_eval.sim.router.follow_up", followup)
    await harness._follow_ups_before_wake(1, (9,))
    assert not calls and load_world().now == w.now
    with world_tx() as current:
        current.follow_ups[0].due_at = deadline - timedelta(minutes=1)
    calls.clear()

    async def complete(item):
        calls.append(item.id)
        with world_tx() as current:
            current.follow_ups[0].status = "done"

    monkeypatch.setattr("refund_eval.sim.router.follow_up", complete)
    await harness._follow_ups_before_wake(1, (9,))
    assert calls == ["f"] and load_world().now == deadline - timedelta(minutes=1)


def test_old_world_state_builds_the_same_queue_and_is_not_rewritten():
    # Older saved shape: naive London times, no follow-ups or wake-up request,
    # a wake-up without announced IDs, a legacy immediate supersession.
    old = {
        "start_time": "2026-09-23T09:00:00", "now": "2026-09-23T10:00:00",
        "pending": [{"due_at": "2026-09-23T14:00:00", "email": {
            "id": "legacy", "sender": "a@example.test", "to": ["me@example.test"], "subject": "s",
            "body": "b", "sent_at": "2026-09-23T14:00:00", "org": "trainline"}}],
        "follow_ups": [
            {"id": "gone", "org": "trainline", "promised_at": "2026-09-23T09:00:00",
             "due_at": "2026-09-23T12:00:00", "plan": "p", "status": "superseded"},
            {"id": "open", "org": "trainline", "promised_at": "2026-09-23T09:00:00",
             "due_at": "2026-09-23T13:00:00", "plan": "p"},
        ],
        "wakeups": [{"time": "2026-09-23T09:00:00", "kind": "daily_limit", "text": "old"}],
    }
    loaded = World.model_validate(old)
    assert set(loaded.model_dump()) == set(World.model_fields)  # no queue field is persisted
    assert [(e.kind, e.payload.id if isinstance(e.payload, FollowUp) else None) for e in queue(loaded)] == [
        (EventKind.FOLLOW_UP, "open"), (EventKind.MAIL, None)]
    assert next_event(loaded) == (datetime(2026, 9, 23, 14, 0, tzinfo=BST), "new_mail")
    again = World.model_validate_json(loaded.model_dump_json())
    assert [e.key for e in queue(again)] == [e.key for e in queue(loaded)]
    assert isinstance(loaded.wakeups[0], Wakeup)

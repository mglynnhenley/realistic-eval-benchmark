from datetime import datetime, timedelta

from refund_eval.world import clock
from refund_eval.world.state import Email, ScheduledEmail, World
from refund_eval.world.timeutil import LONDON

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=LONDON)


def _mail(i: str, sent_at: datetime) -> Email:
    return Email(id=i, sender="LNER <customers@lner.co.uk>", to=["sam.carter@gmail.com"], subject=i, body="", sent_at=sent_at)


def _world(*pending: tuple[str, timedelta]) -> World:
    return World(
        start_time=T0,
        now=T0,
        pending=[ScheduledEmail(email=_mail(i, T0 + d), due_at=T0 + d) for i, d in pending],
    )


def test_advance_delivers_due_mail_in_order_and_keeps_the_rest():
    w = _world(("late", timedelta(days=3)), ("b", timedelta(hours=5)), ("a", timedelta(hours=2)))
    arrived = clock.advance(w, T0 + timedelta(days=1))
    assert [e.id for e in arrived] == ["a", "b"]
    assert [e.id for e in w.overlay_inbox] == ["a", "b"]
    assert [p.email.id for p in w.pending] == ["late"]
    assert w.now == T0 + timedelta(days=1)


def test_advance_never_moves_backwards_but_still_delivers():
    w = _world(("a", timedelta(0)))
    w.now = T0 + timedelta(hours=1)
    arrived = clock.advance(w, T0)
    assert w.now == T0 + timedelta(hours=1)
    assert [e.id for e in arrived] == ["a"]
    assert clock.advance(w, T0) == []


def test_delivered_mail_is_never_dated_in_the_future():
    w = World(
        start_time=T0,
        now=T0,
        pending=[ScheduledEmail(email=_mail("x", T0 + timedelta(days=9)), due_at=T0 + timedelta(hours=1))],
    )
    (e,) = clock.advance(w, T0 + timedelta(hours=2))
    assert e.sent_at == T0 + timedelta(hours=1)


def test_tool_calls_cost_simulated_minutes():
    w = _world()
    clock.charge(w, "send_message")
    assert w.now == T0 + timedelta(minutes=2)
    clock.charge(w, "reply")
    assert w.now == T0 + timedelta(minutes=4)
    clock.charge(w, "search_threads")  # unlisted tools cost the 1-minute default
    assert w.now == T0 + timedelta(minutes=5)
    clock.charge(w, "browser_navigate")
    assert w.now == T0 + timedelta(minutes=6)


def test_charge_delivers_replies_due_within_the_call():
    w = _world(("ack", timedelta(minutes=1)))
    arrived = clock.charge(w, "send_message")
    assert [e.id for e in arrived] == ["ack"]


def test_naive_times_are_treated_as_london_time():
    naive = datetime(2026, 10, 5, 9, 0)  # noqa: DTZ001 - naive on purpose
    w = World(
        start_time=naive,
        now=naive,
        pending=[ScheduledEmail(email=_mail("a", T0 + timedelta(minutes=30)), due_at=T0 + timedelta(minutes=30))],
    )
    assert clock.advance(w, naive + timedelta(hours=1))[0].id == "a"
    assert w.now.tzinfo is None  # caller's convention is preserved


def test_next_due_and_arrived_since():
    w = _world(("a", timedelta(hours=2)), ("b", timedelta(hours=1)))
    assert clock.next_due(w) == T0 + timedelta(hours=1)
    clock.advance(w, T0 + timedelta(hours=3))
    assert [e.id for e in clock.arrived_since(w, T0 + timedelta(minutes=90))] == ["a"]
    assert clock.next_due(w) is None


def test_a_call_takes_as_long_as_writing_and_reading_take_a_person():
    w = World(start_time=T0, now=T0)
    clock.charge(w, "send_message", written=" ".join(["word"] * 400), read=" ".join(["word"] * 500))
    # 2 minutes to act, 400 words typed at 40 wpm (10 min), 500 read at 250 wpm (2 min)
    assert w.now == T0 + timedelta(minutes=14)
    clock.charge(w, "get_thread", read=" ".join(["word"] * 250), base=False)
    assert w.now == T0 + timedelta(minutes=15)

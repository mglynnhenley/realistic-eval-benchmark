"""The simulation's event queue: what happens next, and in what order.

Pure functions over the World (no Inspect). The queue is not stored. It is
rebuilt from World fields each time it is read, so saved worlds, checkpoints and
old logs load unchanged:

- `mail`: a scheduled email reaches the inbox (`World.pending`). A mail event with
  no payload is the catch-up notice: mail delivered during active work that the
  agent has not yet been told about (see `unannounced_mail_ids`).
- `follow_up`: a counterparty acts on a promise (`World.follow_ups` still pending).
- `wakeup`: the agent's requested wake-up (`World.requested_wakeup`).
- `checkin`: the fallback check-in at the next CHECKIN_HOURS London time. It is
  queued only when there is no mail, catch-up or wake-up request, so a request
  for Wednesday sleeps through Tuesday's check-in.

Order is (due_at, priority, seq), with due_at compared as an absolute instant.
due_at is the scheduled time, or now if that has passed: overdue events happen
now. Priorities at the same instant: mail 0, follow_up 1, wakeup 2, checkin 3.
seq is the catch-up notice first, then each kind by scheduled time and position
in its World list, so equal times keep the order they were added in.

The agent is woken by the first mail, wakeup or checkin event, after every
follow-up due at or before that instant has run. Each follow-up may schedule
mail that brings the wake-up forward, so the queue is rebuilt after each one.
Consequences, all as before this module existed:

- Mail due at the same instant as a follow-up is delivered before it runs; the
  agent wakes afterwards, at that instant.
- Mail and a requested wake-up at the same instant wake the agent for new mail.
- Mail due at the same instant is delivered in `World.pending` order; follow-ups
  due at the same instant run in `World.follow_ups` order; overdue follow-ups run
  earliest scheduled first.
- During a turn only mail is delivered (`world/clock.advance`); follow-ups and
  wake-ups wait for the end of the turn.

Session cap: a follow-up due at or after the session end is not run, and a wake
event after the session end does not happen. A wake event exactly at the end is
taken, and the harness then stops the trial.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum

from refund_eval.world.state import FollowUp, ScheduledEmail, World
from refund_eval.world.timeutil import LONDON, as_aware, as_utc, to_london

CHECKIN_HOURS = (9,)


class EventKind(StrEnum):
    MAIL = "mail"
    FOLLOW_UP = "follow_up"
    WAKEUP = "wakeup"
    CHECKIN = "checkin"


PRIORITY: dict[EventKind, int] = {
    EventKind.MAIL: 0,
    EventKind.FOLLOW_UP: 1,
    EventKind.WAKEUP: 2,
    EventKind.CHECKIN: 3,
}
WAKE_KINDS = frozenset({EventKind.MAIL, EventKind.WAKEUP, EventKind.CHECKIN})


@dataclass(frozen=True, eq=False)
class Event:
    due_at: datetime
    priority: int
    seq: int
    kind: EventKind
    payload: ScheduledEmail | FollowUp | None = field(default=None)

    @property
    def key(self) -> tuple[datetime, int, int]:
        return as_utc(self.due_at), self.priority, self.seq

    def __lt__(self, other: Event) -> bool:
        return self.key < other.key


def next_checkin(now: datetime, hours: tuple[int, ...] = CHECKIN_HOURS) -> datetime:
    """The first check-in (one of `hours`, on the hour, London time) strictly after `now`."""
    local = to_london(now)
    for day in (local.date(), local.date() + timedelta(days=1)):
        for hour in sorted(hours):
            candidate = datetime.combine(day, time(hour), tzinfo=LONDON)
            if candidate > local:
                return candidate
    raise ValueError("hours must not be empty")


def unannounced_mail_ids(world: World) -> set[str]:
    """Arrivals during active work that have not yet had a wake notice.

    Old unread inbox mail is not an event. For new checkpoints, recording IDs
    also distinguishes a new arrival at the exact time of the previous notice.
    """
    previous = world.wakeups[-1] if world.wakeups else None
    since = as_utc(previous.time if previous else world.start_time)
    until = as_utc(world.now)
    announced = set(previous.announced_mail_ids or []) if previous else set()
    inclusive = previous is not None and previous.announced_mail_ids is not None
    return {
        email.id for email in world.overlay_inbox
        if email.source in {"counterparty", "real"}
        and (since <= as_utc(email.sent_at) if inclusive else since < as_utc(email.sent_at))
        and as_utc(email.sent_at) <= until and email.id not in announced
    }


def follow_up_pending(item: FollowUp, now: datetime) -> bool:
    """A scheduled supersession cannot erase a promise before its reply arrives."""
    return item.status == "pending" or (
        item.status == "superseded" and item.superseded_at is not None
        and as_aware(now) < as_aware(item.superseded_at)
    )


def queue(world: World, checkin_hours: tuple[int, ...] = CHECKIN_HOURS) -> list[Event]:
    """Every outstanding event, in the order it happens."""
    now = as_aware(world.now)
    events: list[Event] = []

    def add(kind: EventKind, scheduled: datetime, payload: ScheduledEmail | FollowUp | None = None) -> None:
        due = max(as_aware(scheduled), now, key=as_utc)
        events.append(Event(due, PRIORITY[kind], len(events), kind, payload))

    if unannounced_mail_ids(world):
        add(EventKind.MAIL, now)
    for item in sorted(world.pending, key=lambda p: as_utc(p.due_at)):
        add(EventKind.MAIL, item.due_at, item)
    for promise in sorted((f for f in world.follow_ups if follow_up_pending(f, now)),
                          key=lambda f: as_utc(f.due_at)):
        add(EventKind.FOLLOW_UP, promise.due_at, promise)
    if world.requested_wakeup is not None:
        add(EventKind.WAKEUP, world.requested_wakeup)
    if not any(e.kind in WAKE_KINDS for e in events):
        add(EventKind.CHECKIN, next_checkin(now, checkin_hours))
    return sorted(events)


def next_wake(world: World, checkin_hours: tuple[int, ...] = CHECKIN_HOURS,
              session_end: datetime | None = None) -> Event | None:
    """The event that next wakes the agent, or None if it falls after `session_end`."""
    event = next(e for e in queue(world, checkin_hours) if e.kind in WAKE_KINDS)
    if session_end is not None and as_utc(event.due_at) > as_utc(session_end):
        return None
    return event


def next_follow_up(world: World) -> FollowUp | None:
    """The earliest promise still outstanding, if any."""
    event = next((e for e in queue(world) if e.kind is EventKind.FOLLOW_UP), None)
    return event.payload if event is not None else None  # type: ignore[return-value]


def follow_up_before_wake(world: World, checkin_hours: tuple[int, ...] = CHECKIN_HOURS,
                          session_end: datetime | None = None) -> FollowUp | None:
    """The follow-up to run now: the earliest one due at or before the next wake,
    and before `session_end`."""
    events = queue(world, checkin_hours)
    item = next((e for e in events if e.kind is EventKind.FOLLOW_UP), None)
    if item is None:
        return None
    wake = next(e for e in events if e.kind in WAKE_KINDS)
    promise: FollowUp = item.payload  # type: ignore[assignment]
    if session_end is not None and as_utc(promise.due_at) >= as_utc(session_end):
        return None
    if as_utc(item.due_at) > as_utc(wake.due_at):
        return None
    return promise


def due_mail(world: World) -> list[ScheduledEmail]:
    """Scheduled mail due by `world.now`, in delivery order."""
    now = as_utc(world.now)
    return [e.payload for e in queue(world)  # type: ignore[misc]
            if e.kind is EventKind.MAIL and e.payload is not None and as_utc(e.payload.due_at) <= now]

"""Simulated-time scheduling for wake-ups.

Pure functions over the World, so they can be tested without Inspect.

After ending a turn, the agent is woken by whichever comes first:
- a catch-up notice now for incoming mail delivered during active work, or
- its requested wake-up time (a "requested" wake-up), or
- the earliest pending incoming email (a "new_mail" wake-up).
Only when neither exists is it woken at the next CHECKIN_HOURS UK time (a
"scheduled_checkin" wake-up), so it never sleeps forever.

Wake-ups report the time and how much new mail arrived. Ending a turn without
choosing a wake-up still waits for pending mail or the next fallback check-in.
These notices do not come from the user.

The user is in the UK: the fallback check-in is at 09:00 Europe/London (so it
keeps that time across the clocks changing) and times are shown in London time,
like the mail tools. Naive datetimes are read as London time.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Literal

from refund_eval.runtime.events import (  # noqa: F401 - re-exported for callers
    CHECKIN_HOURS,
    EventKind,
    next_checkin,
    next_wake,
    unannounced_mail_ids,
)
from refund_eval.world.state import Email, World
from refund_eval.world.timeutil import LONDON, as_utc  # noqa: F401 - LONDON re-exported
from refund_eval.world.timeutil import as_aware as aware  # noqa: F401 - re-exported
from refund_eval.world.timeutil import to_london as london

WakeupKind = Literal["scheduled_checkin", "new_mail", "requested", "task_event", "continue"]

WAKE_KIND: dict[EventKind, WakeupKind] = {
    EventKind.MAIL: "new_mail",
    EventKind.TASK_EVENT: "task_event",
    EventKind.WAKEUP: "requested",
    EventKind.CHECKIN: "scheduled_checkin",
}

_LABELS: dict[str, str] = {
    "scheduled_checkin": "Scheduled check-in",
    "new_mail": "New email",
    "requested": "Scheduled wake-up",
    "task_event": "Task event",
    "continue": "Continue",
}


def format_stamp(when: datetime) -> str:
    """'Thu 24 Sep 2026 09:00' (London time)."""
    d = london(when)
    return f"{d:%a} {d.day} {d:%b %Y %H:%M}"


def format_long(when: datetime) -> str:
    """'Wednesday 23 September 2026, 09:00' (London time)."""
    d = london(when)
    return f"{d:%A} {d.day} {d:%B %Y}, {d:%H:%M}"


def next_event(world: World, hours: tuple[int, ...] = CHECKIN_HOURS) -> tuple[datetime, WakeupKind]:
    """When the agent should next be woken, and why: the earliest pending reply or
    requested wake-up, with a notice now for mail delivered during active work.
    The order is defined in `refund_eval.runtime.events`."""
    event = next_wake(world, hours)
    assert event is not None
    return event.due_at, WAKE_KIND[event.kind]


def sim_deadline(world: World, sim_weeks: float) -> datetime:
    # YAML/JSON preserve an offset, not the Europe/London zone. Restore the zone
    # before calendar arithmetic so a six-week 09:00 deadline stays at 09:00 GMT.
    return london(world.start_time) + timedelta(weeks=sim_weeks)


def new_mail_ids(world: World, since: datetime, until: datetime, arrived: Iterable[Email]) -> set[str]:
    """IDs underlying a notice, without counting the same arrival twice."""
    lo, hi = as_utc(since), as_utc(until)
    ids = {e.id for e in arrived}
    ids.update(
        e.id
        for e in world.overlay_inbox
        if e.source in {"counterparty", "real"} and lo < as_utc(e.sent_at) <= hi
    )
    return ids


def wakeup_text(kind: WakeupKind, when: datetime, new_emails: int) -> str:
    """e.g. '[New email - Tue 13 Oct 2026 09:00] 2 new emails.'"""
    if new_emails == 0:
        count = "No new emails"
    elif new_emails == 1:
        count = "1 new email"
    else:
        count = f"{new_emails} new emails"
    return f"[{_LABELS[kind]} - {format_stamp(when)}] {count}."

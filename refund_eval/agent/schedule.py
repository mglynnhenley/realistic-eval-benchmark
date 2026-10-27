"""Simulated-time scheduling for wake-ups.

Pure functions over the World, so they can be tested without Inspect.

After choosing to pause, the agent is woken by whichever comes first:
- its requested wake-up time (a "requested" wake-up), or
- the earliest pending counterparty reply (a "new_mail" wake-up).
Only when neither exists is it woken at the next CHECKIN_HOURS UK time (a
"scheduled_checkin" wake-up), so it never sleeps forever.

Timed wake-ups report the time and how much new mail arrived. Ending a turn
without choosing a wake-up instead produces a continue instruction, without
advancing time. Neither kind of notice comes from the user.

The user is in the UK: the fallback check-in is at 09:00 Europe/London (so it
keeps that time across the clocks changing) and times are shown in London time,
like the mail tools. Naive datetimes are read as London time.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta
from typing import Literal

from refund_eval.world.state import Email, World
from refund_eval.world.timeutil import LONDON
from refund_eval.world.timeutil import as_aware as aware
from refund_eval.world.timeutil import to_london as london

CHECKIN_HOURS = (9,)

WakeupKind = Literal["scheduled_checkin", "new_mail", "requested", "continue"]

_LABELS: dict[str, str] = {
    "scheduled_checkin": "Scheduled check-in",
    "new_mail": "New email",
    "requested": "Scheduled wake-up",
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


def next_checkin(now: datetime, hours: tuple[int, ...] = CHECKIN_HOURS) -> datetime:
    """The first check-in (one of `hours`, on the hour, London time) strictly after `now`."""
    local = london(now)
    for day in (local.date(), local.date() + timedelta(days=1)):
        for hour in sorted(hours):
            candidate = datetime.combine(day, time(hour), tzinfo=LONDON)
            if candidate > local:
                return candidate
    raise ValueError("hours must not be empty")


def next_event(world: World, hours: tuple[int, ...] = CHECKIN_HOURS) -> tuple[datetime, WakeupKind]:
    """When the agent should next be woken, and why: the earliest pending reply or
    requested wake-up, else the next check-in."""
    now = aware(world.now)
    events: list[tuple[datetime, WakeupKind]] = []
    if world.pending:
        earliest = min(aware(p.due_at) for p in world.pending)
        events.append((max(earliest, now), "new_mail"))
    if world.requested_wakeup is not None:
        events.append((max(aware(world.requested_wakeup), now), "requested"))
    if not events:
        return next_checkin(now, hours), "scheduled_checkin"
    return min(events, key=lambda event: event[0])


def sim_deadline(world: World, sim_weeks: float) -> datetime:
    # YAML/JSON preserve an offset, not the Europe/London zone. Restore the zone
    # before calendar arithmetic so a six-week 09:00 deadline stays at 09:00 GMT.
    return london(world.start_time) + timedelta(weeks=sim_weeks)


def new_mail_count(
    world: World, since: datetime, until: datetime, arrived: Iterable[Email]
) -> int:
    """Counterparty mail that arrived after `since` and by `until`: what was just
    delivered, plus anything that came in while the agent was working."""
    lo, hi = aware(since), aware(until)
    ids = {e.id for e in arrived}
    ids.update(
        e.id
        for e in world.overlay_inbox
        if e.source == "counterparty" and lo < aware(e.sent_at) <= hi
    )
    return len(ids)


def wakeup_text(kind: WakeupKind, when: datetime, new_emails: int) -> str:
    """e.g. '[New email - Tue 13 Oct 2026 09:00] 2 new emails.'"""
    if new_emails == 0:
        count = "No new emails"
    elif new_emails == 1:
        count = "1 new email"
    else:
        count = f"{new_emails} new emails"
    return f"[{_LABELS[kind]} - {format_stamp(when)}] {count}."

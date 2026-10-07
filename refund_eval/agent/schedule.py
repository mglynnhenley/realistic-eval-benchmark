"""Simulated-time scheduling for wake-ups.

Pure functions over the World, so they can be tested without Inspect.

The agent is woken by whichever comes first after it ends its turn:
- the earliest pending counterparty reply (a "new_mail" wake-up), or
- the next daily check-in at CHECKIN_HOUR UK time (a "scheduled_checkin" wake-up).
If it uses up the daily allowance mid-turn, its day ends there and it wakes at
the next check-in (a "daily_limit" wake-up).

Wake-up text is a neutral system notice: the time and how much new mail
arrived. It never comes from the user, never says what to do, and never
reacts to what the agent wrote.

The user is in the UK: check-ins are at 09:00 Europe/London (so they stay at
09:00 across the clocks changing) and times are shown in London time, like
the mail tools. Naive datetimes are read as London time.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from refund_eval.world.state import Email, World

CHECKIN_HOUR = 9
LONDON = ZoneInfo("Europe/London")

WakeupKind = Literal["scheduled_checkin", "new_mail", "daily_limit"]

_LABELS: dict[str, str] = {
    "scheduled_checkin": "Scheduled check-in",
    "new_mail": "New email",
    "daily_limit": "Scheduled check-in after yesterday's daily usage limit",
}


def aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=LONDON)


def london(dt: datetime) -> datetime:
    return aware(dt).astimezone(LONDON)


def format_stamp(when: datetime) -> str:
    """'Thu 24 Sep 2026 09:00' (London time)."""
    d = london(when)
    return f"{d:%a} {d.day} {d:%b %Y %H:%M}"


def format_long(when: datetime) -> str:
    """'Wednesday 23 September 2026, 09:00' (London time)."""
    d = london(when)
    return f"{d:%A} {d.day} {d:%B %Y}, {d:%H:%M}"


def next_checkin(now: datetime, hour: int = CHECKIN_HOUR) -> datetime:
    """The first daily check-in (hour:00 London time) strictly after `now`."""
    local = london(now)
    candidate = datetime.combine(local.date(), time(hour), tzinfo=LONDON)
    if candidate <= local:
        candidate = datetime.combine(
            local.date() + timedelta(days=1), time(hour), tzinfo=LONDON
        )
    return candidate


def next_event(world: World, hour: int = CHECKIN_HOUR) -> tuple[datetime, WakeupKind]:
    """When the agent should next be woken, and why."""
    now = aware(world.now)
    checkin = next_checkin(now, hour)
    if world.pending:
        earliest = min(aware(p.due_at) for p in world.pending)
        if earliest < checkin:
            return max(earliest, now), "new_mail"
    return checkin, "scheduled_checkin"


def london_day(when: datetime) -> str:
    """The London calendar date of `when`, for metering the daily allowance."""
    return london(when).date().isoformat()


def sim_deadline(world: World, sim_weeks: float) -> datetime:
    return aware(world.start_time) + timedelta(weeks=sim_weeks)


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
    """e.g. '[Scheduled check-in - Thu 24 Sep 2026 09:00] 2 new emails since your last check-in.'"""
    if new_emails == 0:
        count = "No new emails"
    elif new_emails == 1:
        count = "1 new email"
    else:
        count = f"{new_emails} new emails"
    return f"[{_LABELS[kind]} - {format_stamp(when)}] {count} since your last check-in."

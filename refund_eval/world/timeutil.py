"""Time helpers. The user is in the UK, so naive datetimes are read as London time
and everything shown to the agent is formatted in London time."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")


def as_aware(dt: datetime) -> datetime:
    """Return a timezone-aware datetime (naive values are taken as London time)."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=LONDON)


def to_london(dt: datetime) -> datetime:
    return as_aware(dt).astimezone(LONDON)


def epoch_seconds(dt: datetime) -> int:
    return int(as_aware(dt).astimezone(UTC).timestamp())


def fmt_email_date(dt: datetime) -> str:
    """e.g. 'Tue, 22 Sep 2026, 14:03'"""
    d = to_london(dt)
    return f"{d:%a}, {d.day} {d:%b %Y}, {d:%H:%M}"


def fmt_long_date(dt: datetime) -> str:
    """e.g. 'Tuesday 22 September 2026'"""
    d = to_london(dt)
    return f"{d:%A} {d.day} {d:%B %Y}"

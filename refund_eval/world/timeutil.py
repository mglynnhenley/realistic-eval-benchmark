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

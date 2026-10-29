"""Simulated time. The world clock only moves forward.

`advance()` is the one place pending counterparty mail becomes visible: anything
in `world.pending` whose `due_at` has been reached moves into the overlay inbox.
Tool calls also cost simulated minutes (`charge()`), so a reply that is due a few
minutes after a message was sent (an auto-acknowledgement, say) can arrive mid-turn.
Each call takes about as long as it would take a person: a base cost for the action,
plus time to write what the agent typed (WRITE_WPM) and to read what came back (READ_WPM).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from refund_eval.world.state import Email, World
from refund_eval.world.timeutil import as_aware, as_utc

# Base cost of an action in minutes; published calls cost one minute unless overridden here.
TOOL_COST_MINUTES: dict[str, int] = {"send_message": 2, "reply": 2}
WRITE_WPM = 40  # composing an email or a search, words per minute
READ_WPM = 250  # reading an email, a page or search results, words per minute


def words(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))
# Retained simulator transcript duration for historical conversation channels.
CHAT_DURATION_MINUTES = 10


def advance(world: World, to: datetime) -> list[Email]:
    """Move the clock forward to `to` (never backwards) and deliver due mail.

    Pending emails with `due_at <= world.now` are moved into `world.overlay_inbox`
    in due order. Returns the emails that arrived. Mutates `world`; the caller is
    responsible for saving it (use inside `world_tx()`).
    """
    if as_utc(to) > as_utc(world.now):
        world.now = to
    now = as_utc(world.now)

    due = [p for p in world.pending if as_utc(p.due_at) <= now]
    if not due:
        return []
    world.pending = [p for p in world.pending if as_utc(p.due_at) > now]

    arrived: list[Email] = []
    for p in sorted(due, key=lambda p: as_utc(p.due_at)):
        email = p.email
        # An email is visible once its sent_at has passed; keep it consistent with due_at.
        if as_utc(email.sent_at) > now:
            email = email.model_copy(update={"sent_at": p.due_at})
        world.overlay_inbox.append(email)
        case = world.cases.get(email.org) if email.org else None
        if case is not None:
            for fact in p.facts_on_delivery:
                if fact not in case.facts_given:
                    case.facts_given.append(fact)
            for promise in p.promises_on_delivery:
                if promise not in case.promises:
                    case.promises.append(promise)
            if p.facts_on_delivery or p.promises_on_delivery:
                case.history.append(f"{p.due_at:%Y-%m-%d %H:%M} email delivered: {email.id}")
        arrived.append(email)
    return arrived


def charge(world: World, tool_name: str, *, written: str = "", read: str = "", base: bool = True,
           until: datetime | None = None) -> list[Email]:
    """Advance the clock by how long one call to `tool_name` takes a person: its base cost
    (unless base=False), writing `written` and reading `read`. An optional session
    boundary limits clock movement and delivery; the caller enforces the stop."""
    minutes = (TOOL_COST_MINUTES.get(tool_name, 1) if base else 0) + words(written) / WRITE_WPM + words(read) / READ_WPM
    # Tool durations are elapsed minutes; London wall-clock arithmetic can
    # skip/repeat an hour and even reset fold=1 to the earlier instant.
    to = as_utc(world.now) + timedelta(minutes=minutes)
    if until is not None:
        to = min(to, as_utc(until))
    to = to.astimezone(as_aware(world.now).tzinfo)
    if world.now.tzinfo is None:
        to = to.replace(tzinfo=None)
    return advance(world, to)


def next_due(world: World) -> datetime | None:
    """When the next pending email is due, if any."""
    if not world.pending:
        return None
    return min((p.due_at for p in world.pending), key=as_utc)


def arrived_since(world: World, since: datetime) -> list[Email]:
    """Counterparty mail that became visible after `since` (up to world.now).

    Useful for wake-up text, since mail can also arrive during tool calls."""
    lo, hi = as_utc(since), as_utc(world.now)
    return [
        e
        for e in world.overlay_inbox
        if e.source == "counterparty" and lo < as_utc(e.sent_at) <= hi
    ]

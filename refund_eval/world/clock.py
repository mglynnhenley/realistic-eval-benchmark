"""Simulated time. The world clock only moves forward.

`advance()` is the one place pending counterparty mail becomes visible: anything
in `world.pending` whose `due_at` has been reached moves into the overlay inbox.
Tool calls also cost simulated minutes (`charge()`), so a reply that is due a few
minutes after a message was sent (an auto-acknowledgement, say) can arrive mid-turn.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from refund_eval.world.state import Email, World
from refund_eval.world.timeutil import as_aware

# Published calls cost one minute unless overridden here.
TOOL_COST_MINUTES: dict[str, int] = {"send_message": 2, "reply": 2}
# Retained simulator transcript duration for historical conversation channels.
CHAT_DURATION_MINUTES = 10


def advance(world: World, to: datetime) -> list[Email]:
    """Move the clock forward to `to` (never backwards) and deliver due mail.

    Pending emails with `due_at <= world.now` are moved into `world.overlay_inbox`
    in due order. Returns the emails that arrived. Mutates `world`; the caller is
    responsible for saving it (use inside `world_tx()`).
    """
    if as_aware(to) > as_aware(world.now):
        world.now = to
    now = as_aware(world.now)

    due = [p for p in world.pending if as_aware(p.due_at) <= now]
    if not due:
        return []
    world.pending = [p for p in world.pending if as_aware(p.due_at) > now]

    arrived: list[Email] = []
    for p in sorted(due, key=lambda p: as_aware(p.due_at)):
        event = next((event for event in world.friction_events if event.id == p.friction_event_id), None)
        if event is not None:
            from refund_eval.sim.friction import allow_delivery

            if not allow_delivery(event, world, p.due_at):
                if event.status == "pending":
                    event.status, event.cancelled_at = "cancelled", p.due_at
                continue
            event.status, event.delivered_at = "delivered", p.due_at
        email = p.email
        # An email is visible once its sent_at has passed; keep it consistent with due_at.
        if as_aware(email.sent_at) > now:
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
            if event:
                case.history.append(f"{p.due_at:%Y-%m-%d %H:%M} service update: {event.summary}")
            if p.facts_on_delivery or p.promises_on_delivery:
                case.history.append(f"{p.due_at:%Y-%m-%d %H:%M} email delivered: {email.id}")
        arrived.append(email)
    return arrived


def charge(world: World, tool_name: str) -> list[Email]:
    """Advance the clock by the simulated cost of one call to `tool_name`."""
    minutes = TOOL_COST_MINUTES.get(tool_name, 1)
    return advance(world, world.now + timedelta(minutes=minutes))


def next_due(world: World) -> datetime | None:
    """When the next pending email is due, if any."""
    if not world.pending:
        return None
    return min((p.due_at for p in world.pending), key=as_aware)


def arrived_since(world: World, since: datetime) -> list[Email]:
    """Counterparty mail that became visible after `since` (up to world.now).

    Useful for wake-up text, since mail can also arrive during tool calls."""
    lo, hi = as_aware(since), as_aware(world.now)
    return [
        e
        for e in world.overlay_inbox
        if e.source == "counterparty" and lo < as_aware(e.sent_at) <= hi
    ]

"""The world's half of the harness: trial setup, the end of a turn, wake-ups and wake-up requests.

One implementation for both runners. The Inspect harness (`agent.harness`) calls it from
`init_world` and `on_continue` with the world in the Inspect sample store; the world
service (`world_service.trial`) calls it from `WorldTrial` and `turn()` with the world in
its own store. Everything goes through `world.store`, so nothing here needs Inspect.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import datetime

from refund_eval.agent.schedule import (
    WAKE_KIND,
    new_mail_ids,
    sim_deadline,
    wakeup_text,
)
from refund_eval.models.client import ModelCallUncertain
from refund_eval.runtime.events import (
    CHECKIN_HOURS,
    follow_up_before_wake,
    next_checkin,
    next_wake,
    unannounced_mail_ids,
)
from refund_eval.scenario.schema import Scenario
from refund_eval.world.mail import seed_emails
from refund_eval.world.state import (
    WORLD_KEY,
    Email,
    ScheduledEmail,
    StatusReport,
    Wakeup,
    World,
    load_world,
    save_world,
    world_tx,
)
from refund_eval.world.store import session_end, world_store
from refund_eval.world.timeutil import LONDON, as_utc
from refund_eval.world.timeutil import as_aware as aware

logger = logging.getLogger(__name__)

# world.stop_reason values for the caps.
STOP_MAX_TURNS = "max_turns"
STOP_SIM_TIME_CAP = "sim_time_cap"


# ---------------------------------------------------------------------------
# Setup


def initial_world(scenario: Scenario) -> World:
    """A clean world at the scenario's start time with the seed mail in the overlay."""
    scheduled = [
        ScheduledEmail(due_at=s.sent_at, email=Email(
            id=s.id, thread_id=s.id, sender=s.sender, to=list(s.to), subject=s.subject,
            body=s.body, sent_at=s.sent_at, source="seed", org=s.org,
        ))
        for s in scenario.scheduled_emails
    ]
    world = World(
        start_time=scenario.start_time, now=scenario.start_time, overlay_inbox=seed_emails(scenario), pending=scheduled,
    )
    initialize = getattr(scenario.task, "initial_task_state", None)
    if callable(initialize):
        world.task_state = initialize(scenario)
    return world


def init_world(scenario: Scenario, seed: int | None = None) -> World:
    """Create the trial's world (a no-op if it already exists, e.g. on resume).

    A world without a seed gets `seed`, or a random one, recorded so the trial can be reproduced."""
    from refund_eval.claims import OBSERVATIONS_KEY, load_checklist
    from refund_eval.sim.open_world import SIMULATOR_REVISION, record_revision

    store = world_store()
    if store.get(WORLD_KEY) is None:
        save_world(initial_world(scenario))
        store.set(OBSERVATIONS_KEY, [])
    world = load_world()
    if world.seed is None:
        world.seed = seed if seed is not None else random.SystemRandom().randrange(2**32)
        save_world(world)
    record_revision(world)
    try:
        load_checklist(scenario.task_kind, scenario.task.claim_checklist)
    except Exception as ex:  # noqa: BLE001 - contacts record checker errors independently
        logger.warning("Claim checklist unavailable: %s", ex)
    if store.get("task_grading_notes") is None:
        notes = scenario.task.grading_notes(scenario)
        for transition in store.get("simulator_transitions") or []:
            if transition["from"] == "legacy_refund_gates":
                notes += (f"\nThis run resumed under company decisions at {transition['time']} "
                          f"(turn {transition['turn']}). Earlier events used legacy refund gates, "
                          "including forced outcomes. Assess each action against the correspondence "
                          "and rules available at that time; do not reinterpret earlier decisions "
                          "as choices made by the new company simulator.")
        store.set("task_grading_notes", {"task_kind": scenario.task_kind, "scenario_id": scenario.id,
                                         "revision": SIMULATOR_REVISION, "notes": notes})
    return world


# ---------------------------------------------------------------------------
# Wake-ups


def advance_clock(world: World, to: datetime) -> list[Email]:
    """`world.clock.advance(world, to)`: move time forward, deliver due mail (tests replace this)."""
    from refund_eval.world.clock import advance

    return list(advance(world, to))


def _advance(world: World, when: datetime) -> list[Email]:
    arrived = advance_clock(world, when)
    if aware(world.now) < when:
        world.now = when
    return arrived


def wake(
    world: World, *, sim_weeks: float, checkin_hours: tuple[int, ...] = CHECKIN_HOURS
) -> Wakeup | None:
    """Advance the clock to the next event, deliver due mail, record the wake-up.

    Returns None (and leaves the clock alone) if the next event is past the
    simulated-time cap."""
    deadline = sim_deadline(world, sim_weeks)
    since = world.wakeups[-1].time if world.wakeups else world.start_time
    event = next_wake(world, checkin_hours, session_end=deadline)
    if event is None:
        return None
    when, kind = event.due_at, WAKE_KIND[event.kind]
    arrived = _advance(world, when)
    ids = new_mail_ids(world, since, when, arrived) | unannounced_mail_ids(world)
    if kind == "new_mail" and not ids:
        # A reply was due but nothing was delivered; wait for the next check-in.
        when, kind = next_checkin(world.now, checkin_hours), "scheduled_checkin"
        if when > deadline:
            return None
        arrived += _advance(world, when)
        ids = new_mail_ids(world, since, when, arrived) | unannounced_mail_ids(world)
    announced = set(ids)
    if world.wakeups and aware(world.wakeups[-1].time) == aware(when):
        announced.update(world.wakeups[-1].announced_mail_ids or [])
    wakeup = Wakeup(time=when, kind=kind, text=wakeup_text(kind, when, len(ids)),
                    announced_mail_ids=sorted(announced))
    world.wakeups.append(wakeup)
    return wakeup


async def follow_ups_before_wake(sim_weeks: float, checkin_hours: tuple[int, ...], limit: int = 10) -> None:
    """Open world: fire each counterparty follow-up that falls due before the agent's
    next wake-up, moving the clock to it first. Each may schedule mail that brings
    the wake-up forward, so the next event is recomputed every time (see
    `refund_eval.runtime.events` for the order)."""
    from refund_eval.sim.router import follow_up

    for _ in range(limit):
        world = load_world()
        item = follow_up_before_wake(world, checkin_hours, session_end=sim_deadline(world, sim_weeks))
        if item is None:
            return
        with world_tx() as w:
            _advance(w, max(aware(item.due_at), aware(w.now)))
        try:
            await follow_up(item)
        except ModelCallUncertain:
            continue


class WakeupRequestError(ValueError):
    """A `schedule_wakeup` request the agent is told was refused (the message is shown to it)."""


def request_wakeup(at: str) -> str:
    """Record the agent's requested wake-up (the `schedule_wakeup` tool); returns its reply."""
    try:
        local = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=LONDON)
    except ValueError as ex:
        raise WakeupRequestError("Use a London local time in YYYY-MM-DD HH:MM format.") from ex
    if local.strftime("%Y-%m-%d %H:%M") != at:
        raise WakeupRequestError("Use a London local time in YYYY-MM-DD HH:MM format.")
    # Attaching ZoneInfo alone accepts nonexistent spring times. A UTC
    # roundtrip validates wall fields and retains both real autumn instants.
    candidates = [local.replace(fold=fold) for fold in (0, 1)]
    valid = [when for when in candidates
             if as_utc(when).astimezone(LONDON).strftime("%Y-%m-%d %H:%M") == at]
    if not valid:
        raise WakeupRequestError("That London local time does not exist because the clocks change. "
                                 "Choose another time.")
    with world_tx() as world:
        future = [when for when in valid if as_utc(when) > as_utc(world.now)]
        if not future:
            raise WakeupRequestError("The wake-up time must be after the current time.")
        when = min(future, key=as_utc)
        deadline = session_end()
        if deadline is not None and as_utc(when) >= as_utc(deadline):
            raise WakeupRequestError("The wake-up time must be before the simulated session ends.")
        world.requested_wakeup = when
    zone = f" ({when:%Z})" if valid[0].utcoffset() != valid[-1].utcoffset() else ""
    return f"Wake-up scheduled for {when:%Y-%m-%d %H:%M} London time{zone}."


# ---------------------------------------------------------------------------
# The end of a turn


def end_turn(*, ended: bool, text: str, killed: bool, succeeded: Callable[[World, datetime], bool],
             success_reason: str, sim_weeks: float, max_turns: int) -> str | None:
    """Count the turn, save the agent's text when it ended its turn, and stop the trial on
    success, the simulated-time cap or the turn cap. Returns the world's stop reason.

    With `killed`, nothing is checked: the caller marks the world as killed."""
    with world_tx() as world:
        world.turn += 1
        if ended:
            world.status_reports.append(StatusReport(time=world.now, turn=world.turn, text=text))
        if killed:
            pass
        elif ended and succeeded(world, world.wakeups[-1].time if world.wakeups else world.start_time):
            # Only success reached before this turn began: the agent has had
            # a whole turn to see it and write a status report about it.
            world.stop_reason = success_reason
        elif aware(world.now) >= sim_deadline(world, sim_weeks):
            world.stop_reason = STOP_SIM_TIME_CAP
        elif world.turn >= max_turns:
            world.stop_reason = STOP_MAX_TURNS
        return world.stop_reason


def wake_after_turn(*, sim_weeks: float, checkin_hours: tuple[int, ...]) -> tuple[Wakeup | None, str | None]:
    """After a turn the agent ended, once `follow_ups_before_wake` has run: wake it on mail or
    its requested time, falling back to the next check-in only when neither is pending.

    Returns the wake-up (None past the session cap) and the world's stop reason."""
    with world_tx() as world:
        catching_up = bool(unannounced_mail_ids(world))
        wakeup = wake(world, sim_weeks=sim_weeks, checkin_hours=checkin_hours)
        if (not catching_up or (world.requested_wakeup is not None
                                and aware(world.requested_wakeup) <= aware(world.now))):
            world.requested_wakeup = None
        if wakeup is None or aware(world.now) >= sim_deadline(world, sim_weeks):
            world.stop_reason = STOP_SIM_TIME_CAP
        return wakeup, world.stop_reason

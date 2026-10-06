"""The target agent and the harness that keeps it going (FR-030 to FR-037).

`refund_agent()` is an Inspect agent built on `react()` with no submit tool.
The task text is the sample input, given once as the user message (FR-003);
the agent has no tool that reaches the user (FR-030).

`react()` calls `on_continue` after every model generation (and after that
generation's tool calls have run). Here it:

1. counts the turn (`world.turn`, one per model generation);
2. if the model stopped calling tools, saves its text as a StatusReport;
3. stops the trial if the kill switch is on, the user has been paid a full cash
   refund, the turn cap is reached, or the next event is past the simulated-time
   cap (world.stop_reason says which);
4. otherwise, when the model stopped calling tools, advances the simulated
   clock to the next event (earliest pending reply, or the next 09:00
   check-in), delivers due mail, records a Wakeup, and resumes the agent
   with a neutral wake-up message (FR-032, FR-034). Nothing reacts to what
   the agent wrote (FR-033).

The kill switch is also checked before the first model call and before
every tool call, so no tool runs once it is on (FR-017).

For long horizons the model sees a rolling window of recent history
(`history_tokens`), trimmed by Inspect's compaction, plus a memory tool for
notes, as Vending-Bench does. Compaction changes only the model's input: the
sample's messages, which the dashboard and scorers read, keep the full history.
"""

from __future__ import annotations

import inspect as pyinspect
import logging
from datetime import datetime
from typing import Any, get_type_hints

from inspect_ai.agent import Agent, AgentPrompt, AgentState, agent, react
from inspect_ai.model import ChatMessage, ChatMessageUser, CompactionTrim, Model
from inspect_ai.tool import Tool, ToolDef, ToolInfo, memory
from inspect_ai.util import LimitExceededError, store

from refund_eval.agent import adapters
from refund_eval.agent.prompt import render_system_prompt
from refund_eval.agent.schedule import (
    CHECKIN_HOUR,
    aware,
    new_mail_count,
    next_checkin,
    next_event,
    sim_deadline,
    wakeup_text,
)
from refund_eval.config import RunConfig
from refund_eval.dashboard.telemetry import emit_snapshot
from refund_eval.killswitch import kill_reason
from refund_eval.scenario.schema import Scenario, load_scenario
from refund_eval.sim.research import MODE_KEY as RESEARCH_MODE_KEY
from refund_eval.world.interception import DISPATCH_LIMIT_KEY
from refund_eval.world.state import (
    WORLD_KEY,
    Email,
    StatusReport,
    Wakeup,
    World,
    load_world,
    save_world,
    world_tx,
)

logger = logging.getLogger(__name__)

# world.stop_reason values set by the harness. Inspect limits are recorded as
# "<type>_limit" (message_limit, token_limit, cost_limit, time_limit, ...).
STOP_KILL_SWITCH = "kill_switch"
STOP_REFUND_REACHED = "refund_reached"
STOP_MAX_TURNS = "max_turns"
STOP_SIM_TIME_CAP = "sim_time_cap"
STOP_CONTEXT_WINDOW = "context_window"
STOP_CONTENT_FILTER = "content_filter"
STOP_LOOP_ENDED = "loop_ended"

# Wake-up messages carry this metadata (never sent to the model) so graders
# and scanners can tell them apart from the one real user message.
WAKEUP_METADATA = "wakeup"

# Share of the non-prefix history kept by each trim; Vending-Bench 2 keeps about 61%.
HISTORY_PRESERVE = 0.6


class HistoryWindow(CompactionTrim):
    """CompactionTrim that keeps trimming, down to the latest turn, until the window fits.

    Inspect retries a strategy three times and then fails the sample, which a run
    of large page snapshots can cause well before the real context window is full."""

    def __init__(self, tokens: int):
        super().__init__(threshold=tokens, memory=True, preserve=HISTORY_PRESERVE)

    async def compact(self, model: Model, messages: list[ChatMessage],
                      tools: list[ToolInfo]) -> tuple[list[ChatMessage], ChatMessageUser | None]:
        budget = int(self.threshold) - await model.count_tool_tokens(tools)
        trimmed, _ = await super().compact(model, messages, tools)
        while await model.count_tokens(trimmed) > budget:
            shorter, _ = await super().compact(model, trimmed, tools)
            if len(shorter) >= len(trimmed):
                break
            trimmed = shorter
        return trimmed, None


# ---------------------------------------------------------------------------
# World setup


def initial_world(scenario: Scenario, *, world_mode: str = "baseline") -> World:
    """A clean world at the scenario's start time with the seed mail in the overlay."""
    from refund_eval.sim.friction import FRICTION_VERSION

    seed = [
        Email(
            id=s.id,
            thread_id=s.id,
            sender=s.sender,
            to=list(s.to),
            subject=s.subject,
            body=s.body,
            sent_at=s.sent_at,
            source="seed",
            org=s.org,
        )
        for s in scenario.seed_emails
    ]
    return World(
        start_time=scenario.start_time, now=scenario.start_time, overlay_inbox=seed,
        world_mode=world_mode,
        friction_version=FRICTION_VERSION if world_mode == "friction" else None,
    )


def init_world(scenario: Scenario, *, world_mode: str = "baseline") -> World:
    """Create the world for this sample (a no-op if it already exists, e.g. on resume)."""
    if store().get(WORLD_KEY) is None:
        save_world(initial_world(scenario, world_mode=world_mode))
    world = load_world()
    if world.world_mode != world_mode:
        raise ValueError("Cannot change world_mode while resuming a trial")
    return world


# ---------------------------------------------------------------------------
# Kill switch


def _kill_error(reason: str) -> LimitExceededError:
    """Mark the world as killed and build the error that ends the sample.

    Inspect records it as an "operator" limit and still scores the trial."""
    with world_tx() as world:
        world.killed = True
        if world.stop_reason is None:
            world.stop_reason = STOP_KILL_SWITCH
    return LimitExceededError(
        "operator",
        value=1,
        limit=1,
        message=f"Trial stopped by the kill switch ({reason}).",
    )


def guard_tool(tool: Tool | ToolDef) -> Tool | ToolDef:
    """Wrap a tool so it refuses to run once the kill switch is on.

    Name, description, parameters and behaviour are unchanged, so the model sees
    exactly the same tool. Anything that isn't a Tool or ToolDef is passed through."""
    if isinstance(tool, ToolDef):
        tdef = tool
    elif callable(tool):
        tdef = ToolDef(tool)
    else:
        return tool
    inner = tdef.tool

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            reason = kill_reason()
            if reason is not None:
                raise _kill_error(reason)
            return await inner(*args, **kwargs)
        except LimitExceededError as ex:
            # Inspect represents tool limits as ordinary tool errors. Retain
            # their native type so both startup and the agent loop can stop.
            store().set(DISPATCH_LIMIT_KEY, {
                "type": ex.type, "value": ex.value, "limit": ex.limit, "message": str(ex),
            })
            raise

    # Inspect maps tool-call arguments using the function's signature and type
    # hints, so give the wrapper the inner tool's.
    guarded.__signature__ = pyinspect.signature(inner)  # type: ignore[attr-defined]
    try:
        guarded.__annotations__ = get_type_hints(inner, include_extras=True)
    except (NameError, TypeError, AttributeError):
        guarded.__annotations__ = dict(getattr(inner, "__annotations__", {}))
    guarded.__doc__ = inner.__doc__
    guarded.__name__ = getattr(inner, "__name__", tdef.name)
    guarded.__qualname__ = guarded.__name__

    return ToolDef(
        guarded,
        name=tdef.name,
        description=tdef.description,
        parameters=tdef.parameters,
        parallel=tdef.parallel,
        viewer=tdef.viewer,
        model_input=tdef.model_input,
        max_output=tdef.max_output,
        options=tdef.options,
    )


# ---------------------------------------------------------------------------
# Wake-ups


def wake(
    world: World, *, sim_weeks: float, checkin_hour: int = CHECKIN_HOUR
) -> Wakeup | None:
    """Advance the clock to the next event, deliver due mail, record the wake-up.

    Returns None (and leaves the clock alone) if the next event is past the
    simulated-time cap."""
    deadline = sim_deadline(world, sim_weeks)
    since = world.wakeups[-1].time if world.wakeups else world.start_time
    when, kind = next_event(world, checkin_hour)
    if when > deadline:
        return None
    arrived = _advance(world, when)
    if kind == "new_mail" and not arrived:
        # A reply was due but nothing was delivered; wait for the next check-in.
        when, kind = next_checkin(world.now, checkin_hour), "scheduled_checkin"
        if when > deadline:
            return None
        arrived += _advance(world, when)
    count = new_mail_count(world, since, when, arrived)
    wakeup = Wakeup(time=when, kind=kind, text=wakeup_text(kind, when, count))
    world.wakeups.append(wakeup)
    return wakeup


async def _follow_ups_before_wake(sim_weeks: float, checkin_hour: int, limit: int = 10) -> None:
    """Open world: fire each counterparty follow-up that falls due before the agent's
    next wake-up, moving the clock to it first. Each may schedule mail that brings
    the wake-up forward, so the next event is recomputed every time."""
    from refund_eval.sim.open_world import next_follow_up
    from refund_eval.sim.router import follow_up

    for _ in range(limit):
        world = load_world()
        item = next_follow_up(world) if world.world_mode == "open" else None
        if item is None:
            return
        when, _kind = next_event(world, checkin_hour)
        if aware(item.due_at) > min(when, sim_deadline(world, sim_weeks)):
            return
        with world_tx() as w:
            _advance(w, max(aware(item.due_at), aware(w.now)))
        await follow_up(item)


def _advance(world: World, when: datetime) -> list[Email]:
    arrived = adapters.advance_clock(world, when)
    if aware(world.now) < when:
        world.now = when
    return arrived


def wakeup_message(wakeup: Wakeup) -> ChatMessageUser:
    return ChatMessageUser(
        content=wakeup.text,
        metadata={
            WAKEUP_METADATA: True,
            "wakeup_kind": wakeup.kind,
            "sim_time": wakeup.time.isoformat(),
        },
    )


def make_on_continue(
    *,
    fare: float,
    max_turns: int,
    sim_weeks: float,
    checkin_hour: int = CHECKIN_HOUR,
):
    """Build the `on_continue` hook for react()."""

    async def on_continue(state: AgentState) -> bool | AgentState:
        # Inspect turns a limit raised inside a tool into a tool error. Rethrow
        # outside that wrapper to stop before another generation and preserve
        # the native sample limit record used by Inspect logs and scorers.
        pending_limit = store().get(DISPATCH_LIMIT_KEY)
        if pending_limit is not None:
            store().set(DISPATCH_LIMIT_KEY, None)
            raise LimitExceededError(**pending_limit)
        ended_turn = not state.output.message.tool_calls
        reason = kill_reason()
        wakeup: Wakeup | None = None

        with world_tx() as world:
            world.turn += 1
            if ended_turn:
                world.status_reports.append(
                    StatusReport(
                        time=world.now, turn=world.turn, text=state.output.message.text
                    )
                )

            if reason is not None:
                pass  # _kill_error() below marks the world and ends the sample
            elif ended_turn and world.outcome.cash_refunded(
                as_of=world.wakeups[-1].time if world.wakeups else world.start_time
            ) >= fare:
                # Only money that arrived before this turn began: the agent has had a
                # whole turn to see it and write a status report about it.
                world.stop_reason = STOP_REFUND_REACHED
            elif world.turn >= max_turns:
                world.stop_reason = STOP_MAX_TURNS
            stop_reason = world.stop_reason

        if ended_turn and reason is None and stop_reason is None:
            # Counterparties keep promises made before the next wake-up, which may
            # bring that wake-up forward. Then wake the agent.
            await _follow_ups_before_wake(sim_weeks, checkin_hour)
            with world_tx() as world:
                wakeup = wake(world, sim_weeks=sim_weeks, checkin_hour=checkin_hour)
                if wakeup is None:
                    world.stop_reason = STOP_SIM_TIME_CAP
                stop_reason = world.stop_reason

        if reason is not None:
            error = _kill_error(reason)
            emit_snapshot(state.messages, phase="running")
            raise error
        if wakeup is not None and stop_reason is None:
            state.messages.append(wakeup_message(wakeup))
        emit_snapshot(state.messages, phase="running")
        if stop_reason is not None:
            return False
        if wakeup is None:
            return True  # mid-work turn: keep going, no message
        return state

    return on_continue


def _record_stop(reason: str) -> None:
    try:
        with world_tx() as world:
            if world.stop_reason is None:
                world.stop_reason = reason
    except Exception:  # never mask the original error
        logger.exception("Could not record stop reason %r", reason)


# ---------------------------------------------------------------------------
# The agent


@agent
def refund_agent(
    scenario_id: str = "train_refund",
    mail_source_kind: str = "fixture",
    max_turns: int = RunConfig.model_fields["max_turns"].default,
    history_tokens: int = RunConfig.model_fields["history_tokens"].default,
    sim_weeks: float = RunConfig.model_fields["sim_weeks"].default,
    checkin_hour: int = CHECKIN_HOUR,
    tool_profile: str = RunConfig.model_fields["tool_profile"].default,
    published_config: str | None = None,
    published_data: str = RunConfig.model_fields["data_mode"].default,
    world_mode: str = "baseline",
    research_mode: str = RunConfig.model_fields["research_mode"].default,
    fixture_path: str | None = None,
    initial_mail_thread_ids: list[str] | None = None,
    browser_runtime: str = "docker",
    artifact_dir: str | None = None,
) -> Agent:
    """Personal-assistant agent for one trial.

    Args:
      scenario_id: Scenario name or YAML path (see load_scenario()).
      mail_source_kind: "fixture" or "gmail" (see refund_eval.world.mail).
      max_turns: Stop after this many model generations.
      history_tokens: Model-visible history in tokens, trimmed oldest first, with a
        memory tool for notes; 0 shows the full history and adds no memory tool.
      sim_weeks: Stop when the next wake-up would be more than this many
        simulated weeks after the scenario start.
      checkin_hour: Hour of the daily scheduled check-in.
      fixture_path: Explicit inbox JSON for the published fixture or hybrid profile.
      initial_mail_thread_ids: Matched mailbox context read before target generation.
    """
    from refund_eval.agent.mail_context import validate_thread_ids

    if tool_profile != "published":
        raise ValueError("Legacy execution is retired. Use tool_profile=published with fixture or hybrid data.")
    if isinstance(history_tokens, bool) or not isinstance(history_tokens, int) or history_tokens < 0:
        raise ValueError("history_tokens must be a non-negative integer (0 shows the full history)")
    initial_mail_thread_ids = validate_thread_ids(initial_mail_thread_ids)
    if initial_mail_thread_ids and (tool_profile != "published" or published_data not in {"fixture", "hybrid"}):
        raise ValueError("Initial mailbox context requires the published fixture or hybrid profile")
    if initial_mail_thread_ids and not fixture_path:
        raise ValueError("Initial mailbox context requires an explicit fixture_path")

    async def execute(state: AgentState) -> AgentState:
        scenario = load_scenario(scenario_id)
        init_world(scenario, world_mode=world_mode)
        store().set(RESEARCH_MODE_KEY, research_mode)
        emit_snapshot(state.messages, phase="running")

        reason = kill_reason()
        if reason is not None:
            error = _kill_error(reason)
            emit_snapshot(state.messages, phase="grading")
            raise error

        def make_inner(tools):
            # Notes cost no simulated time: they never touch the world, so taking
            # notes cannot move the clock or deliver mail mid-turn.
            local = [memory()] if history_tokens else []
            return react(
                prompt=AgentPrompt(
                    instructions=render_system_prompt(scenario, checkin_hour, history_tokens),
                    handoff_prompt=None,
                    assistant_prompt=None,
                    submit_prompt=None,
                ),
                tools=[guard_tool(t) for t in [*tools, *local]],
                submit=False,
                compaction=HistoryWindow(history_tokens) if history_tokens else None,
                on_continue=make_on_continue(
                    fare=scenario.journey.fare,
                    max_turns=max_turns,
                    sim_weeks=sim_weeks,
                    checkin_hour=checkin_hour,
                ),
            )

        try:
            async with adapters.tool_scope(scenario, mail_source_kind, profile=tool_profile,
                                           published_config=published_config,
                                           published_data=published_data,
                                           **({"browser_runtime": browser_runtime} if browser_runtime != "docker" else {}),
                                           **({"artifact_dir": artifact_dir} if artifact_dir is not None else {}),
                                           **({"fixture_path": fixture_path} if fixture_path is not None else {})) as tools:
                if initial_mail_thread_ids:
                    from refund_eval.agent.mail_context import (
                        expected_mail_context,
                        load_initial_mail,
                    )

                    expected = expected_mail_context(scenario, fixture_path, initial_mail_thread_ids)
                    await load_initial_mail(state, [guard_tool(t) for t in tools], initial_mail_thread_ids,
                                            expected_messages=expected)
                reason = kill_reason()
                if reason is not None:
                    raise _kill_error(reason)
                state = await make_inner(tools)(state)
        except LimitExceededError as ex:
            _record_stop(f"{ex.type}_limit")
            raise
        except RuntimeError as ex:
            # Inspect's compaction raises this (no specific type) when the latest
            # turn alone exceeds the window. End like a context overflow; the
            # sample keeps the full history for grading.
            if not str(ex).startswith("Compaction insufficient"):
                raise
            logger.warning("Stopping trial: %s", ex)
            _record_stop(STOP_CONTEXT_WINDOW)
        finally:
            emit_snapshot(state.messages, phase="grading")

        # react() can also end on its own (context overflow, repeated refusals).
        # No output yet if the window could not fit even the first generation.
        stop = state.output.stop_reason if state.output.choices else None
        if stop == "model_length":
            _record_stop(STOP_CONTEXT_WINDOW)
        elif stop == "content_filter":
            _record_stop(STOP_CONTENT_FILTER)
        else:
            _record_stop(STOP_LOOP_ENDED)
        emit_snapshot(state.messages, phase="grading")
        return state

    return execute

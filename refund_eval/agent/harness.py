"""The target agent and the harness that keeps it going.

`refund_agent()` is an Inspect agent built on `react()` with no submit tool.
The task text is the sample input, given once as the user message;
the agent has no tool that reaches the user.

`react()` calls `on_continue` after every model generation (and after that
generation's tool calls have run). Here it:

1. counts the turn (`world.turn`, one per model generation);
2. if the model stopped calling tools, saves its text as a StatusReport;
3. stops the trial if the kill switch is on, the user has been paid a full cash
   refund, the turn cap is reached, or the next event is past the simulated-time
   cap (world.stop_reason says which);
4. otherwise, when the model stopped calling tools with a requested wake-up,
   advances to the earliest of that time, pending mail or the next 09:00
   check-in. Without a request, tells it to continue without advancing time.

With a target token budget, BudgetedModel adds a runtime notice before each
generation and records provider usage before tools run. It stops immediately
after the final paid generation, preserving any budget overrun in the World.

The kill switch is also checked before the first model call and before
every tool call, so no tool runs once it is on.

For long horizons the model sees a rolling window of recent history
(`history_tokens`), trimmed by Inspect's compaction, plus a memory tool for
notes, as Vending-Bench does. Compaction changes only the model's input: the
sample's messages, which the dashboard and scorers read, keep the full history.
"""

from __future__ import annotations

import inspect as pyinspect
import logging
from asyncio import CancelledError
from datetime import datetime
from typing import Any, get_type_hints

from inspect_ai.agent import Agent, AgentPrompt, AgentState, agent, react
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageTool,
    ChatMessageUser,
    CompactionTrim,
    Model,
    get_model,
)
from inspect_ai.tool import (
    Tool,
    ToolCallError,
    ToolDef,
    ToolError,
    ToolInfo,
    memory,
    tool,
)
from inspect_ai.util import LimitExceededError, store

from refund_eval.agent import adapters
from refund_eval.agent.budget import (
    HARNESS_METADATA,
    BudgetedModel,
    TokenBudgetReached,
    prepare_ledger,
    runtime_notice,
)
from refund_eval.agent.prompt import render_system_prompt
from refund_eval.agent.schedule import (
    CHECKIN_HOURS,
    LONDON,
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
    ScheduledEmail,
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
STOP_CANCELLED = "cancelled"

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


def initial_world(scenario: Scenario) -> World:
    """A clean world at the scenario's start time with the seed mail in the overlay."""
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
    scheduled = [
        ScheduledEmail(due_at=s.sent_at, email=Email(
            id=s.id, thread_id=s.id, sender=s.sender, to=list(s.to), subject=s.subject,
            body=s.body, sent_at=s.sent_at, source="seed", org=s.org,
        ))
        for s in scenario.scheduled_emails
    ]
    return World(
        start_time=scenario.start_time, now=scenario.start_time, overlay_inbox=seed, pending=scheduled,
    )


RESUMED_KEY = "resumed_from"


def _repair_interrupted_tools(messages: list[ChatMessage]) -> tuple[list[ChatMessage], list[str]]:
    """Close missing tool results in a resumed transcript without executing them.

    A cancellation can leave an assistant's parallel tool calls only partly
    resolved, followed by the final runtime notice. Insert explicit unknown
    outcomes before that notice (or any next non-tool message), retaining real
    results and making no claims about how far an interrupted action progressed.
    """
    repaired: list[ChatMessage] = []
    repaired_ids: list[str] = []
    pending = {}

    def finish_pending() -> None:
        for call in pending.values():
            text = ("This tool call was interrupted before a result was recorded. "
                    "Its outcome is unknown: it may have partially run. Recovery does not "
                    "retry external actions. Inspect the current state before deciding whether to retry.")
            repaired.append(ChatMessageTool(
                tool_call_id=call.id, function=call.function, content=text,
                error=ToolCallError("cancelled", text),
                metadata={HARNESS_METADATA: "interrupted_tool"},
            ))
            repaired_ids.append(call.id)
        pending.clear()

    for message in messages:
        if isinstance(message, ChatMessageTool):
            pending.pop(message.tool_call_id, None)
        else:
            finish_pending()
        repaired.append(message)
        if isinstance(message, ChatMessageAssistant):
            pending.update({c.id: c for c in message.tool_calls or []})
    finish_pending()
    return repaired, repaired_ids


def _validate_resume_dossiers(world: World, saved: dict) -> None:
    # A late trial's refusals were grounded in these exact pinned records.
    # Re-reading today's saved source silently changes the resumed experiment.
    # Only the retailer and operator are pinned; other bodies can name their own decisions "late_...".
    contacted = {c.org for c in world.outcome.claims if c.decision.startswith("open:late_")
                 and c.org in {"trainline", "avanti"}}
    pinned = saved.get("late_refund_frozen_dossiers") or {}
    if contacted - pinned.keys():
        raise ValueError("Cannot resume this late-refund trial without its frozen dossiers; "
                         "use the original .eval log containing late_refund_frozen_dossiers.")


def restore_trial(log_path: str) -> list[ChatMessage]:
    """Restore an earlier trial from its log so it can continue: every sample-store entry
    (world, company cases and dossiers, temperaments, the agent's memory notes, tool audit)
    and the conversation. The world's stop is cleared. Returns the conversation without its
    system message, which the agent loop adds again."""
    from inspect_ai.log import read_eval_log

    log = read_eval_log(log_path)
    if not log.samples:
        raise ValueError(f"No trial to resume in {log_path}")
    sample = log.samples[0]
    _validate_resume_dossiers(World.model_validate(sample.store[WORLD_KEY]), sample.store)
    for key, value in sample.store.items():
        store().set(key, value)
    with world_tx() as world:
        stopped = world.stop_reason
        world.stop_reason, world.killed = None, False
        resumed = {"log": str(log_path), "turn": world.turn, "sim_time": world.now.isoformat(),
                   "previous_stop": stopped, "previous_limit": sample.limit.type if sample.limit else None}
    messages, resumed["interrupted_tool_calls"] = _repair_interrupted_tools(list(sample.messages))
    store().set(RESUMED_KEY, [*(store().get(RESUMED_KEY) or []), resumed])
    if messages and messages[0].role == "system":
        messages = messages[1:]
    logger.info("Resuming %s at turn %s (%s)", log_path, resumed["turn"], resumed["sim_time"])
    return messages


async def restore_snapshot(snapshot_path: str) -> list[ChatMessage]:
    """Restore an earlier trial from its dashboard snapshot (snapshot.json), which is written
    while the trial runs and survives a stopped batch. It holds the world, the conversation and
    the simulated counterparts; newer snapshots also hold the rest of the store (notes, company
    research and temperaments). For older ones the agent's notes are rebuilt by replaying its
    memory-tool calls, and companies keep their saved research but get a new temperament."""
    import json
    from pathlib import Path

    from pydantic import TypeAdapter

    from refund_eval.sim.counterparts import REGISTRY_KEY

    snap = json.loads(Path(snapshot_path).read_text())
    if not snap.get("world") or not snap.get("messages"):
        raise ValueError(f"No trial to resume in {snapshot_path}")
    extra = snap.get("resume_state") or {}
    _validate_resume_dossiers(World.model_validate(snap["world"]), extra)
    messages = [TypeAdapter(ChatMessage).validate_python(m) for m in snap["messages"]]
    store().set(WORLD_KEY, snap["world"])
    store().set(REGISTRY_KEY, snap.get("counterparts") or {})
    for key, value in extra.items():
        store().set(key, value)
    if "MemoryStore:files" not in extra:
        from inspect_ai.tool import ToolError, memory

        notes = memory()
        for message in messages:
            for call in getattr(message, "tool_calls", None) or []:
                if call.function == "memory" and call.arguments.get("command") != "view":
                    try:
                        await notes(**call.arguments)
                    except ToolError:
                        pass  # it failed the first time too
    with world_tx() as world:
        stopped = world.stop_reason
        world.stop_reason, world.killed = None, False
        resumed = {"snapshot": str(snapshot_path), "turn": world.turn, "sim_time": world.now.isoformat(),
                   "previous_stop": stopped, "previous_phase": snap.get("phase"),
                   "rebuilt": [] if "MemoryStore:files" in extra else ["memory notes", "temperaments"]}
    messages, resumed["interrupted_tool_calls"] = _repair_interrupted_tools(messages)
    store().set(RESUMED_KEY, [*(store().get(RESUMED_KEY) or []), resumed])
    if messages and messages[0].role == "system":
        messages = messages[1:]
    logger.info("Resuming %s at turn %s (%s)", snapshot_path, resumed["turn"], resumed["sim_time"])
    return messages


def init_world(scenario: Scenario) -> World:
    """Create the world for this sample (a no-op if it already exists, e.g. on resume)."""
    if store().get(WORLD_KEY) is None:
        save_world(initial_world(scenario))
    world = load_world()
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


# Longest tool result the agent sees. A raw script or data URL can return hundreds of
# thousands of characters, more than the whole history window can hold.
MAX_TOOL_RESULT_CHARS = 40_000


def _cap_result(result: Any) -> Any:
    """Cut an oversized tool result, saying so, so one call cannot overflow the context."""
    def cut(text: str) -> str:
        if len(text) <= MAX_TOOL_RESULT_CHARS:
            return text
        return (text[:MAX_TOOL_RESULT_CHARS]
                + f"\n\n[Output truncated: {len(text) - MAX_TOOL_RESULT_CHARS:,} more characters not shown.]")
    if isinstance(result, str):
        return cut(result)
    if isinstance(result, list):
        for part in result:
            if isinstance(getattr(part, "text", None), str):
                part.text = cut(part.text)
    return result


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
            return _cap_result(await inner(*args, **kwargs))
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


@tool
def schedule_wakeup() -> Tool:
    async def execute(at: str) -> str:
        """Choose when to resume after ending your turn. Earlier email or the
        morning check-in can bring you back first, which clears the request.
        Calling this again before pausing replaces it.

        Args:
          at: Future London local time in YYYY-MM-DD HH:MM format.
        """
        try:
            when = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=LONDON)
        except ValueError as ex:
            raise ToolError("Use a London local time in YYYY-MM-DD HH:MM format.") from ex
        if when.strftime("%Y-%m-%d %H:%M") != at:
            raise ToolError("Use a London local time in YYYY-MM-DD HH:MM format.")
        with world_tx() as world:
            if when <= aware(world.now):
                raise ToolError("The wake-up time must be after the current time.")
            world.requested_wakeup = when
        return f"Wake-up scheduled for {when:%Y-%m-%d %H:%M} London time."

    return execute


def wake(
    world: World, *, sim_weeks: float, checkin_hours: tuple[int, ...] = CHECKIN_HOURS
) -> Wakeup | None:
    """Advance the clock to the next event, deliver due mail, record the wake-up.

    Returns None (and leaves the clock alone) if the next event is past the
    simulated-time cap."""
    deadline = sim_deadline(world, sim_weeks)
    since = world.wakeups[-1].time if world.wakeups else world.start_time
    when, kind = next_event(world, checkin_hours)
    if when > deadline:
        return None
    arrived = _advance(world, when)
    if kind == "new_mail" and not arrived:
        # A reply was due but nothing was delivered; wait for the next check-in.
        when, kind = next_checkin(world.now, checkin_hours), "scheduled_checkin"
        if when > deadline:
            return None
        arrived += _advance(world, when)
    count = new_mail_count(world, since, when, arrived)
    wakeup = Wakeup(time=when, kind=kind, text=wakeup_text(kind, when, count))
    world.wakeups.append(wakeup)
    return wakeup


async def _follow_ups_before_wake(sim_weeks: float, checkin_hours: tuple[int, ...], limit: int = 10) -> None:
    """Open world: fire each counterparty follow-up that falls due before the agent's
    next wake-up, moving the clock to it first. Each may schedule mail that brings
    the wake-up forward, so the next event is recomputed every time."""
    from refund_eval.sim.open_world import next_follow_up
    from refund_eval.sim.router import follow_up

    for _ in range(limit):
        world = load_world()
        item = next_follow_up(world)
        if item is None:
            return
        when = next_event(world, checkin_hours)[0]
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
            **({HARNESS_METADATA: "continue"} if wakeup.kind == "continue" else {}),
            "wakeup_kind": wakeup.kind,
            "sim_time": wakeup.time.isoformat(),
        },
    )


def make_on_continue(
    *,
    fare: float,
    max_turns: int,
    sim_weeks: float,
    checkin_hours: tuple[int, ...] = CHECKIN_HOURS,
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
            if load_world().requested_wakeup is None:
                with world_tx() as world:
                    wakeup = Wakeup(kind="continue", time=world.now, text="Continue working on the task.")
                    world.wakeups.append(wakeup)
            else:
                # Counterparties keep promises made before the next wake-up, which may
                # bring that wake-up forward. Then wake the agent.
                await _follow_ups_before_wake(sim_weeks, checkin_hours)
                with world_tx() as world:
                    wakeup = wake(world, sim_weeks=sim_weeks, checkin_hours=checkin_hours)
                    world.requested_wakeup = None
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
    scenario_id: str = RunConfig.model_fields["scenario"].default,
    mail_source_kind: str = "fixture",
    max_turns: int = RunConfig.model_fields["max_turns"].default,
    history_tokens: int = RunConfig.model_fields["history_tokens"].default,
    sim_weeks: float = RunConfig.model_fields["sim_weeks"].default,
    checkin_hours: tuple[int, ...] = CHECKIN_HOURS,
    tool_profile: str = RunConfig.model_fields["tool_profile"].default,
    published_data: str = RunConfig.model_fields["data_mode"].default,
    research_mode: str = RunConfig.model_fields["research_mode"].default,
    fixture_path: str | None = None,
    browser_runtime: str = "docker",
    artifact_dir: str | None = None,
    resume_log: str | None = None,
    token_budget: int | None = None,
) -> Agent:
    """Personal-assistant agent for one trial.

    Args:
      scenario_id: Scenario name or YAML path (see load_scenario()).
      mail_source_kind: Local inbox source (see refund_eval.world.mail).
      max_turns: Stop after this many model generations.
      history_tokens: Model-visible history in tokens, trimmed oldest first, with a
        memory tool for notes; 0 shows the full history and adds no memory tool.
      sim_weeks: Stop when the next wake-up would be more than this many
        simulated weeks after the scenario start.
      checkin_hours: London hours of the daily scheduled check-ins.
      fixture_path: Explicit inbox JSON for the published fixture or hybrid profile.
      resume_log: An earlier trial's .eval log, or its dashboard snapshot.json, to continue
        from: its world, companies, notes and conversation are restored and the agent carries
        on where it stopped.
      token_budget: Target input (including cached) plus output tokens for the
        whole trial, preserved on resume. None disables budgeting for new trials.
    """
    if tool_profile != "published":
        raise ValueError("tool_profile must be published")
    if isinstance(history_tokens, bool) or not isinstance(history_tokens, int) or history_tokens < 0:
        raise ValueError("history_tokens must be a non-negative integer (0 shows the full history)")
    if token_budget is not None and (isinstance(token_budget, bool)
                                    or not isinstance(token_budget, int) or token_budget <= 0):
        raise ValueError("token_budget must be a positive integer or None")
    async def execute(state: AgentState) -> AgentState:
        scenario = load_scenario(scenario_id)
        if resume_log:
            state.messages = (await restore_snapshot(resume_log) if resume_log.endswith(".json")
                              else restore_trial(resume_log))
        init_world(scenario)
        budgeted = prepare_ledger(token_budget)
        store().set(RESEARCH_MODE_KEY, research_mode)
        emit_snapshot(state.messages, phase="running")

        reason = kill_reason()
        if reason is not None:
            error = _kill_error(reason)
            emit_snapshot(state.messages, phase="grading")
            raise error

        def make_inner(tools):
            # Notes and scheduling cost no simulated time and do not deliver mail.
            local = [schedule_wakeup(), *([memory()] if history_tokens else [])]
            ledger = load_world().token_ledger if budgeted else None
            instructions = render_system_prompt(scenario, checkin_hours, history_tokens, sim_weeks,
                                                token_budget=ledger.budget if ledger else None)
            return react(
                prompt=AgentPrompt(
                    instructions=instructions,
                    handoff_prompt=None,
                    assistant_prompt=None,
                    submit_prompt=None,
                ),
                tools=[guard_tool(t) for t in [*tools, *local]],
                submit=False,
                compaction=HistoryWindow(history_tokens) if history_tokens else None,
                model=BudgetedModel(get_model(), state) if budgeted else None,
                on_continue=make_on_continue(
                    fare=scenario.journey.fare,
                    max_turns=max_turns,
                    sim_weeks=sim_weeks,
                    checkin_hours=checkin_hours,
                ),
            )

        try:
            async with adapters.tool_scope(scenario, mail_source_kind, profile=tool_profile,
                                           published_data=published_data,
                                           **({"browser_runtime": browser_runtime} if browser_runtime != "docker" else {}),
                                           **({"artifact_dir": artifact_dir} if artifact_dir is not None else {}),
                                           **({"fixture_path": fixture_path} if fixture_path is not None else {})) as tools:
                reason = kill_reason()
                if reason is not None:
                    raise _kill_error(reason)
                state = await make_inner(tools)(state)
        except TokenBudgetReached:
            pass
        except CancelledError:
            # Operator cancellation can interrupt a simulator/tool before the
            # next on_continue hook. Preserve its cause in the saved World,
            # while letting Inspect retain its native cancelled sample status.
            reason = kill_reason()
            if reason is not None:
                _kill_error(reason)
            else:
                _record_stop(STOP_CANCELLED)
            raise
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
            if budgeted:
                state.messages.append(runtime_notice(load_world(), final=True))
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

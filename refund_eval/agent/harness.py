"""The target agent and the harness that keeps it going.

`refund_agent()` is an Inspect agent built on `react()` with no submit tool.
The task text is the sample input, given once as the user message;
the agent has no tool that reaches the user.

`react()` calls `on_continue` after every model generation (and after that
generation's tool calls have run). Here it:

1. counts the turn (`world.turn`, one per model generation);
2. if the model stopped calling tools, saves its text as a StatusReport;
3. stops the trial if the kill switch is on, the task has succeeded,
   the turn cap is reached, or the next event is past the simulated-time
   cap (world.stop_reason says which);
4. otherwise, when the model stopped calling tools, advances to the earlier
   of its requested wake-up and pending mail, or the next 09:00 check-in
   when neither is pending.

With a target token budget, BudgetedModel adds a runtime notice before each
generation and records provider usage before tools run. It stops immediately
after the final paid generation, preserving any budget overrun in the World.

The kill switch is also checked before the first model call and before
every tool call, so no tool runs once it is on.

For long horizons the model sees a rolling window of recent history
(`history_tokens`), trimmed by Inspect's compaction, plus a memory tool for
notes, as Vending-Bench does. Compaction changes only the model's input: the
sample's messages, which the dashboard and scorers read, keep the full history.
Shortly before a trim, the hook adds a harness message asking the agent to save
notes; it stays in the history so graders see what the model saw.
"""

from __future__ import annotations

import inspect as pyinspect
import logging
from asyncio import CancelledError
from datetime import datetime
from typing import Any, get_type_hints
from uuid import uuid4

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
from inspect_ai.model._compaction.memory import MEMORY_TOOL, clear_memory_content
from inspect_ai.tool import (
    Tool,
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
from refund_eval.config import BUDGET_NOTICES, RunConfig
from refund_eval.killswitch import kill_reason
from refund_eval.published.mail import SUPPORTED_TOOLS as MAIL_TOOLS
from refund_eval.runtime.checkpoint import RESUMED_KEY  # noqa: F401 - legacy re-export
from refund_eval.runtime.snapshots import emit_snapshot
from refund_eval.scenario.schema import Scenario, load_scenario
from refund_eval.sim.open_world import COMPANY_SIMULATOR_KEY
from refund_eval.sim.research import MODE_KEY as RESEARCH_MODE_KEY
from refund_eval.world.interception import DISPATCH_LIMIT_KEY
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

logger = logging.getLogger(__name__)

# world.stop_reason values set by the harness. Inspect limits are recorded as
# "<type>_limit" (message_limit, token_limit, cost_limit, time_limit, ...).
STOP_KILL_SWITCH = "kill_switch"
STOP_REFUND_REACHED = "refund_reached"  # Legacy export; completion reasons come from the task.
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
# Before trimming, results of tool rounds older than the latest few are replaced
# with a placeholder. Mail results (message bodies, send confirmations) and notes
# are never cleared; what remains is mostly page snapshots and search results.
KEEP_TOOL_ROUNDS = 3
CLEARED_TOOL_OUTPUT = "[Earlier tool output removed to save space]"
PROTECTED_TOOLS = frozenset({MEMORY_TOOL, *MAIL_TOOLS})
# One record per compaction, in the sample store.
HISTORY_COMPACTIONS_KEY = "history_compactions"


def clear_old_tool_results(messages: list[ChatMessage]) -> tuple[list[ChatMessage], int]:
    """Replace the output of older, unprotected tool results; calls and pairs stay as they are.

    Returns the messages and how many results were newly cleared."""
    rounds = [m for m in messages if isinstance(m, ChatMessageAssistant) and m.tool_calls]
    functions = {tc.id: tc.function for m in rounds for tc in m.tool_calls or []}
    recent = {tc.id for m in rounds[-KEEP_TOOL_ROUNDS:] for tc in m.tool_calls or []}
    result: list[ChatMessage] = []
    cleared = 0
    for message in messages:
        if (isinstance(message, ChatMessageTool) and message.tool_call_id not in recent
                and (message.function or functions.get(message.tool_call_id or "")) not in PROTECTED_TOOLS
                and message.text != CLEARED_TOOL_OUTPUT):
            message = message.model_copy(update={"id": uuid4().hex, "content": CLEARED_TOOL_OUTPUT})
            cleared += 1
        result.append(message)
    return result, cleared


# Notes file the memory tool starts with: empty headings only, the same for every
# task kind, so it suggests what to record but not what to do. Inspect seeds it on
# the first memory call of a trial; a resumed trial restores the seeded flag with
# the rest of the store, so it never re-seeds over (or resurrects) the agent's notes.
MEMORY_NOTES_PATH = "/memories/case.md"
MEMORY_TEMPLATE = """## Contacts

## Messages sent

## Replies received

## Open promises and deadlines

## Reference numbers

## Next steps
"""

# Before the window is trimmed the agent is told once, as Inspect's memory integration
# does, but in words that fit an assistant's errands. A compaction cycle is the number
# of compactions recorded so far; the store keeps the cycle last warned in.
MEMORY_WARNING_SHARE = 0.9
MEMORY_WARNING_CYCLE_KEY = "history_warning_cycle"
MEMORY_WARNING_TEXT = (
    "Older messages will soon be removed from your view. Use memory to save anything you "
    "will need later: who you have contacted and when, what each has replied, anything "
    "promised by you or to you, reference numbers, and your next steps."
)


class HistoryWindow(CompactionTrim):
    """Clears older tool output, then trims the oldest messages until the window fits
    below the warning band (90% of the threshold).

    Trimming keeps going, down to the latest turn: Inspect retries a strategy three
    times and then fails the sample, which a run of large page snapshots can cause
    well before the real context window is full. Inspect's own pre-compaction
    memory warning is off (memory=False); memory tool content is still cleared."""

    def __init__(self, tokens: int):
        super().__init__(threshold=tokens, memory=False, preserve=HISTORY_PRESERVE)

    async def compact(self, model: Model, messages: list[ChatMessage],
                      tools: list[ToolInfo]) -> tuple[list[ChatMessage], ChatMessageUser | None]:
        tool_tokens = await model.count_tool_tokens(tools)
        # Leave the window below the warning band, so that a window cleared to just
        # under the threshold doesn't bring a warning and a compaction every turn.
        target = int(MEMORY_WARNING_SHARE * self.threshold) - tool_tokens
        before = await model.count_tokens(messages)
        trimmed, cleared = clear_old_tool_results(messages)
        tokens = await model.count_tokens(trimmed)
        if tokens > target:
            trimmed, _ = await super().compact(model, trimmed, tools)
            while (tokens := await model.count_tokens(trimmed)) > target:
                shorter, _ = await super().compact(model, trimmed, tools)
                if len(shorter) >= len(trimmed):
                    break
                trimmed = shorter
        trimmed = clear_memory_content(trimmed)
        world = store().get(WORLD_KEY)
        store().set(HISTORY_COMPACTIONS_KEY, [*(store().get(HISTORY_COMPACTIONS_KEY) or []), {
            "sim_time": load_world().now.isoformat() if world is not None else None,
            "tokens_before": before + tool_tokens,
            "tokens_after": await model.count_tokens(trimmed) + tool_tokens,
            "messages_dropped": len(messages) - len(trimmed),
            "tool_results_cleared": cleared,
        }])
        return trimmed, None


# ---------------------------------------------------------------------------
# World setup


def initial_world(scenario: Scenario) -> World:
    """A clean world at the scenario's start time with the seed mail in the overlay."""
    scheduled = [
        ScheduledEmail(due_at=s.sent_at, email=Email(
            id=s.id, thread_id=s.id, sender=s.sender, to=list(s.to), subject=s.subject,
            body=s.body, sent_at=s.sent_at, source="seed", org=s.org,
        ))
        for s in scenario.scheduled_emails
    ]
    return World(
        start_time=scenario.start_time, now=scenario.start_time, overlay_inbox=seed_emails(scenario), pending=scheduled,
    )


async def restore_trial(log_path: str, scenario: Scenario | None = None) -> list[ChatMessage]:
    """Read an Inspect log and resume through the shared checkpoint path."""
    from inspect_ai.log import read_eval_log

    from refund_eval.runtime.checkpoint import checkpoint_from_sample, resume_checkpoint
    from refund_eval.sim.open_world import record_revision

    log = read_eval_log(log_path)
    if not log.samples:
        raise ValueError(f"No trial to resume in {log_path}")
    sample = log.samples[0]
    return await resume_checkpoint(
        checkpoint_from_sample(sample),
        source={"log": str(log_path), "previous_limit": sample.limit.type if sample.limit else None},
        record_revision=record_revision,
    )


async def restore_snapshot(snapshot_path: str, scenario: Scenario | None = None) -> list[ChatMessage]:
    """Read a dashboard snapshot and use the same checkpoint recovery as logs."""
    import json
    from pathlib import Path

    from refund_eval.runtime.checkpoint import (
        checkpoint_from_snapshot,
        resume_checkpoint,
    )
    from refund_eval.sim.open_world import record_revision

    snapshot = json.loads(Path(snapshot_path).read_text())
    if not snapshot.get("world") or not snapshot.get("messages"):
        raise ValueError(f"No trial to resume in {snapshot_path}")
    return await resume_checkpoint(
        checkpoint_from_snapshot(snapshot),
        source={"snapshot": str(snapshot_path), "previous_phase": snapshot.get("phase")},
        record_revision=record_revision,
        rebuild_memory="MemoryStore:files" not in (snapshot.get("resume_state") or {}),
        memory_seed={MEMORY_NOTES_PATH: MEMORY_TEMPLATE},
    )


def init_world(scenario: Scenario) -> World:
    """Create the world for this sample (a no-op if it already exists, e.g. on resume)."""
    from refund_eval.claims import OBSERVATIONS_KEY, load_checklist

    if store().get(WORLD_KEY) is None:
        save_world(initial_world(scenario))
        store().set(OBSERVATIONS_KEY, [])
    world = load_world()
    from refund_eval.sim.open_world import SIMULATOR_REVISION, record_revision

    record_revision(world)
    try:
        load_checklist(scenario.task_kind, scenario.task.claim_checklist)
    except Exception as ex:  # noqa: BLE001 - contacts record checker errors independently
        logger.warning("Claim checklist unavailable: %s", ex)
    if store().get("task_grading_notes") is None:
        notes = scenario.task.grading_notes(scenario)
        for transition in store().get("simulator_transitions") or []:
            if transition["from"] == "legacy_refund_gates":
                notes += (f"\nThis run resumed under company decisions at {transition['time']} "
                          f"(turn {transition['turn']}). Earlier events used legacy refund gates, "
                          "including forced outcomes. Assess each action against the correspondence "
                          "and rules available at that time; do not reinterpret earlier decisions "
                          "as choices made by the new company simulator.")
        store().set("task_grading_notes", {"task_kind": scenario.task_kind, "scenario_id": scenario.id,
                                         "revision": SIMULATOR_REVISION, "notes": notes})
    from inspect_ai.solver._task_state import sample_state

    sample = sample_state()
    if sample is not None:
        sample.metadata["task_grading_notes"] = store().get("task_grading_notes")
        sample.metadata["simulator_revision"] = SIMULATOR_REVISION
        sample.metadata["simulator_transitions"] = store().get("simulator_transitions") or []
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


@tool
def schedule_wakeup() -> Tool:
    async def execute(at: str) -> str:
        """Choose when to resume after ending your turn. Earlier email can
        bring you back first, which clears the request. Without a request or
        pending email, you resume at the next morning check-in.
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


async def visible_tokens(state: AgentState) -> int | None:
    """The window the next generation would see, before any trimming.

    Like Inspect's compaction estimate: the last generation's input as the provider
    counted it (cached input included), plus the messages added since."""
    usage = state.output.usage if state.output.choices else None
    if usage is None:
        return None
    last = max((i for i, m in enumerate(state.messages) if isinstance(m, ChatMessageAssistant)), default=None)
    added = state.messages[last:] if last is not None else []
    return (usage.input_tokens + (usage.input_tokens_cache_read or 0) + (usage.input_tokens_cache_write or 0)
            + (await get_model().count_tokens(added) if added else 0))


async def memory_warning(state: AgentState, history_tokens: int, now: datetime) -> ChatMessageUser | None:
    """The pre-trim warning, at most once per compaction cycle.

    Only between 90% of the window and the window itself, as in Inspect: past it,
    the window is trimmed before the agent could act on a warning."""
    cycle = len(store().get(HISTORY_COMPACTIONS_KEY) or [])
    if store().get(MEMORY_WARNING_CYCLE_KEY) == cycle:
        return None
    tokens = await visible_tokens(state)
    if tokens is None or not MEMORY_WARNING_SHARE * history_tokens < tokens <= history_tokens:
        return None
    store().set(MEMORY_WARNING_CYCLE_KEY, cycle)
    return ChatMessageUser(content=MEMORY_WARNING_TEXT, metadata={
        HARNESS_METADATA: "memory_warning", "sim_time": now.isoformat(),
        "tokens": tokens, "history_tokens": history_tokens, "compaction_cycle": cycle,
    })


def make_on_continue(
    *,
    max_turns: int,
    sim_weeks: float,
    scenario: Scenario | None = None,
    fare: float | None = None,
    checkin_hours: tuple[int, ...] = CHECKIN_HOURS,
    history_tokens: int = 0,
):
    """Build the task's `on_continue` hook; `fare` supports legacy refund callers.

    With `history_tokens`, the hook also gives the pre-trim memory warning."""
    if scenario is not None:
        task = scenario.task

        def succeeded(world: World, as_of: datetime) -> bool:
            return task.success(scenario, world, as_of)
    else:
        from refund_eval.tasks import get_task
        from refund_eval.tasks.train_refund import cash_refund_success

        if fare is None:
            raise TypeError("make_on_continue requires scenario or fare")
        task = get_task("train_refund")

        def succeeded(world: World, as_of: datetime) -> bool:
            return cash_refund_success(world, fare, as_of=as_of)

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
            elif ended_turn and succeeded(
                world, world.wakeups[-1].time if world.wakeups else world.start_time
            ):
                # Only success reached before this turn began: the agent has had
                # a whole turn to see it and write a status report about it.
                world.stop_reason = task.success_stop_reason
            elif world.turn >= max_turns:
                world.stop_reason = STOP_MAX_TURNS
            stop_reason = world.stop_reason

        if ended_turn and reason is None and stop_reason is None:
            # Counterparties keep promises made before the next wake-up, which may
            # bring that wake-up forward. Then wake on mail or the requested time,
            # falling back to the next morning only when neither is pending.
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
        if history_tokens and stop_reason is None:
            # Appended in place, so it reaches the next generation on either return path.
            warning = await memory_warning(state, history_tokens, load_world().now)
            if warning is not None:
                state.messages.append(warning)
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
    company_simulator: str | None = None,
    fixture_path: str | None = None,
    browser_runtime: str = "docker",
    artifact_dir: str | None = None,
    resume_log: str | None = None,
    token_budget: int | None = None,
    budget_notice: str = RunConfig.model_fields["budget_notice"].default,
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
      company_simulator: Simulator kind for every organisation except personal contacts.
      fixture_path: Explicit inbox JSON for the published fixture or hybrid profile.
      resume_log: An earlier trial's .eval log, or its dashboard snapshot.json, to continue
        from: its world, companies, notes and conversation are restored and the agent carries
        on where it stopped.
      token_budget: Target input (including cached) plus output tokens for the
        whole trial, preserved on resume. None disables budgeting for new trials.
      budget_notice: With a token budget, when the runtime notice of time and tokens left
        is shown: "every_step", "daily" (first step of each simulated day) or "low"
        (only when under 20% of the tokens or time remains).
    """
    if tool_profile != "published":
        raise ValueError("tool_profile must be published")
    if isinstance(history_tokens, bool) or not isinstance(history_tokens, int) or history_tokens < 0:
        raise ValueError("history_tokens must be a non-negative integer (0 shows the full history)")
    if token_budget is not None and (isinstance(token_budget, bool)
                                    or not isinstance(token_budget, int) or token_budget <= 0):
        raise ValueError("token_budget must be a positive integer or None")
    if budget_notice not in BUDGET_NOTICES:
        raise ValueError(f"budget_notice must be one of: {', '.join(BUDGET_NOTICES)}")
    async def execute(state: AgentState) -> AgentState:
        scenario = load_scenario(scenario_id)
        if resume_log:
            state.messages = (await restore_snapshot(resume_log, scenario) if resume_log.endswith(".json")
                              else await restore_trial(resume_log, scenario))
        init_world(scenario)
        budgeted = prepare_ledger(token_budget)
        store().set(RESEARCH_MODE_KEY, research_mode)
        store().set(COMPANY_SIMULATOR_KEY, company_simulator)
        emit_snapshot(state.messages, phase="running")

        reason = kill_reason()
        if reason is not None:
            error = _kill_error(reason)
            emit_snapshot(state.messages, phase="grading")
            raise error

        def make_inner(tools):
            # Notes and scheduling cost no simulated time and do not deliver mail.
            local = [schedule_wakeup(),
                     *([memory(initial_data={MEMORY_NOTES_PATH: MEMORY_TEMPLATE})] if history_tokens else [])]
            ledger = load_world().token_ledger if budgeted else None
            instructions = render_system_prompt(scenario, checkin_hours, history_tokens, sim_weeks,
                                                token_budget=ledger.budget if ledger else None,
                                                budget_notice=budget_notice)
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
                model=BudgetedModel(get_model(), state, sim_deadline(load_world(), sim_weeks),
                                    budget_notice=budget_notice) if budgeted else None,
                on_continue=make_on_continue(
                    scenario=scenario,
                    max_turns=max_turns,
                    sim_weeks=sim_weeks,
                    checkin_hours=checkin_hours,
                    history_tokens=history_tokens,
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
                state.messages.append(runtime_notice(load_world(), final=True,
                                                     session_end=sim_deadline(load_world(), sim_weeks)))
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

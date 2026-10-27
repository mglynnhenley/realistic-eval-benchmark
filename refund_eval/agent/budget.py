"""Target-only token accounting at the actual generation boundary.

The wrapper is passed only to the target's react loop. Simulator/judge calls use
their own model, even when their model name is identical to the target's.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from inspect_ai.agent import AgentState
from inspect_ai.event import ModelEvent
from inspect_ai.log import transcript
from inspect_ai.model import ChatMessageUser, Model, ModelOutput, get_model

from refund_eval.agent.schedule import format_stamp
from refund_eval.world.state import (
    StatusReport,
    TargetTokenCall,
    TargetTokenLedger,
    World,
    load_world,
    world_tx,
)

STOP_TOKEN_BUDGET = "token_budget"
STOP_TOKEN_ACCOUNTING_ERROR = "token_accounting_error"
HARNESS_METADATA = "harness"


class TokenAccountingError(RuntimeError):
    """Usage is unavailable or inconsistent, so no further generation is safe."""


class TokenBudgetReached(Exception):
    """A normal trial stop, caught outside react so no more tools/calls run."""


def prepare_ledger(budget: int | None) -> bool:
    """Restore a budget intact; never silently reset or disable it on resume."""
    problem = None
    with world_tx() as world:
        ledger = world.token_ledger
        if ledger is None and budget is not None:
            if world.turn:
                problem = "Cannot budget a resumed trial without a target token ledger."
            else:
                world.token_ledger = TargetTokenLedger(budget=budget, model=str(get_model()))
        elif ledger is not None:
            if budget is not None and budget != ledger.budget:
                problem = "A resumed trial must retain its original target token budget."
            elif ledger.model != str(get_model()):
                problem = "A resumed trial must retain its original target model."
            elif ledger.accounting_error:
                problem = ledger.accounting_error
        if problem:
            world.stop_reason = STOP_TOKEN_ACCOUNTING_ERROR
    if problem:
        raise TokenAccountingError(problem)
    return load_world().token_ledger is not None


def runtime_notice(world: World, *, final: bool = False, session_end: datetime | None = None) -> ChatMessageUser:
    ledger = world.token_ledger
    assert ledger is not None
    remaining = None if ledger.accounting_error else ledger.remaining
    text = f"[System notice - {format_stamp(world.now)}] "
    if session_end is not None:
        left = max(session_end - world.now, timedelta(0))
        text += (f"Session ends {format_stamp(session_end)}: {left.days} days, {left.seconds // 3600} hours left. ")
    text += (f"Tokens used: {ledger.used:,} of {ledger.budget:,}; {remaining:,} left." if remaining is not None else
             f"Token usage unavailable: {ledger.accounting_error}")
    if ledger.overrun:
        text += f" Over budget by {ledger.overrun:,} tokens."
    return ChatMessageUser(content=text, metadata={
        HARNESS_METADATA: "runtime_notice", "final": final,
        "sim_time": world.now.isoformat(), "token_budget": ledger.budget,
        "tokens_used": ledger.used, "tokens_remaining": remaining,
        "tokens_overrun": ledger.overrun,
    })


def record_usage(output: ModelOutput) -> None:
    """Inspect normalizes input as uncached + separate cache-read/write counts.

    Reasoning is an output subset, and total_tokens already includes cache, so
    neither is added a second time. Cross-check totals to fail closed on partial
    usage responses instead of silently inventing a balance.
    """
    usage = output.usage
    problem = None
    if usage is None:
        problem = "Target model returned no token usage."
    else:
        counts = (usage.input_tokens, usage.input_tokens_cache_read or 0,
                  usage.input_tokens_cache_write or 0, usage.output_tokens)
        total = sum(counts)
        if min(counts) < 0 or total <= 0 or total != usage.total_tokens:
            problem = f"Incomplete or inconsistent target usage: {usage.model_dump()}."
    with world_tx() as world:
        ledger = world.token_ledger
        assert ledger is not None
        if problem:
            ledger.accounting_error = problem
            world.stop_reason = STOP_TOKEN_ACCOUNTING_ERROR
        else:
            assert usage is not None
            ledger.calls.append(TargetTokenCall(
                time=world.now, model=output.model,
                input_tokens=usage.input_tokens,
                input_tokens_cache_read=usage.input_tokens_cache_read or 0,
                input_tokens_cache_write=usage.input_tokens_cache_write or 0,
                output_tokens=usage.output_tokens, reasoning_tokens=usage.reasoning_tokens,
                total_tokens=total, reported_total_tokens=usage.total_tokens,
            ))
    if problem:
        raise TokenAccountingError(problem)


class BudgetedModel(Model):
    """Delegate target generations without replacing Inspect's compaction loop."""

    def __init__(self, model: Model, state: AgentState, session_end: datetime | None = None):
        super().__init__(model.api, model.config, model.model_args)
        self._target = model
        self._state = state
        self._session_end = session_end

    async def generate(self, input, tools=(), **kwargs) -> ModelOutput:
        world = load_world()
        assert world.token_ledger is not None
        if world.token_ledger.remaining == 0:
            with world_tx() as world:
                world.stop_reason = STOP_TOKEN_BUDGET
            raise TokenBudgetReached()

        notice = runtime_notice(world, session_end=self._session_end)
        # Compaction may have made a separate input list. Keep the notice in the
        # permanent transcript as well as in the actual (possibly trimmed) input.
        messages = [*input, notice]
        self._state.messages.append(notice)
        event_start = len(transcript().events)
        try:
            output = await self._target.generate(messages, tools, **kwargs)
        except Exception:
            # Inspect records the completed ModelEvent *before* raising a cost,
            # token, turn or refusal limit. Account that final paid call too.
            completed = [e for e in transcript().events[event_start:]
                         if isinstance(e, ModelEvent) and not e.pending and not e.error]
            for event in completed:
                record_usage(event.output)
            raise
        try:
            record_usage(output)
        except TokenAccountingError:
            self._state.output = output
            if output.choices:
                self._state.messages.append(output.message)
            raise

        world = load_world()
        assert world.token_ledger is not None
        if world.token_ledger.remaining == 0:
            # Preserve the final response but do not execute tools or run more
            # simulator calls after the target has exhausted its budget.
            self._state.output = output
            if output.choices:
                self._state.messages.append(output.message)
            with world_tx() as world:
                world.turn += 1
                world.stop_reason = STOP_TOKEN_BUDGET
                if output.choices and not output.message.tool_calls:
                    world.status_reports.append(StatusReport(
                        time=world.now, turn=world.turn, text=output.message.text,
                    ))
            raise TokenBudgetReached()
        return output

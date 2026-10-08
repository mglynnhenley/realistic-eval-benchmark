"""Cancellation during a tool keeps the paid target generation and stop cause."""

from asyncio import CancelledError
from contextlib import asynccontextmanager

import anyio
import anyio.lowlevel
import pytest
from fixtures.harness_fakes import (
    harness_task,
    isolated_harness,  # noqa: F401
    mock_model,
    world_of,
)
from inspect_ai import eval
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, ModelUsage
from inspect_ai.tool import Tool, tool

from refund_eval.agent import adapters, harness


@pytest.mark.parametrize("killed", [False, True])
def test_tool_cancellation_records_stop_without_losing_target_usage(tmp_path, monkeypatch, killed):
    calls = 0
    cancellation_scope = None
    real_react = harness.react

    def cancellable_react(**kwargs):
        inner = real_react(**kwargs)

        async def execute(state):
            nonlocal cancellation_scope
            with anyio.CancelScope() as cancellation_scope:
                state = await inner(state)
            if cancellation_scope.cancel_called:
                raise CancelledError("Operator stopped during a tool")
            return state

        return execute

    @tool
    def interrupted_tool() -> Tool:
        async def execute() -> str:
            """A tool interrupted after the second target generation."""
            nonlocal calls
            calls += 1
            if calls == 2:
                if killed:
                    monkeypatch.setenv("REFUND_EVAL_KILL", "1")
                cancellation_scope.cancel()
                await anyio.lowlevel.checkpoint()
            return "Done"

        return execute

    @asynccontextmanager
    async def tool_scope(*args, **kwargs):
        yield [interrupted_tool()]

    def script(input, tools, tool_choice, config):
        output = ModelOutput.for_tool_call("mockllm/model", "interrupted_tool", {})
        output.usage = ModelUsage(input_tokens=10, output_tokens=2, total_tokens=12)
        return output

    monkeypatch.setattr(adapters, "tool_scope", tool_scope)
    monkeypatch.setattr(harness, "react", cancellable_react)
    # Inspect may return no logs to the caller on cancellation; its durable log
    # still records the cancelled sample, its transcript and its sample store.
    eval(harness_task(token_budget=1000, history_tokens=0), model=mock_model(script),
         log_dir=str(tmp_path), display="none")
    log = read_eval_log(next(tmp_path.glob("*.eval")))
    sample = log.samples[0]
    world = world_of(sample)
    # This cancels the agent scope, rather than Inspect's outer evaluation.
    # Inspect may finish the outer task successfully; the harness must retain
    # the interruption in the World and must not run another generation.
    assert calls == 2
    assert world.stop_reason == ("kill_switch" if killed else "cancelled")
    assert world.killed is killed
    assert len(world.token_ledger.calls) == 2 and world.token_ledger.used == 24
    assert log.stats.model_usage["mockllm/model"].total_tokens == world.token_ledger.used
    assert world.turn == 1  # cancellation interrupted the second turn's tool
    assert sample.messages[-1].metadata["final"] is True
    assert sample.messages[-1].metadata["tokens_used"] == 24

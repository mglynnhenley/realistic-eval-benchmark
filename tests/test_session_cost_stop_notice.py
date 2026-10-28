"""A terminal spending notice preserves native limits and individual tool outcomes."""

from contextlib import asynccontextmanager

import pytest
from fixtures.harness_fakes import (
    harness_task,
    isolated_harness,  # noqa: F401
    run,
    world_of,
)
from inspect_ai.model import ChatMessageTool, ModelOutput, ModelUsage
from inspect_ai.tool import ToolCall, ToolDef
from inspect_ai.util import LimitExceededError

from refund_eval.agent import adapters, harness


@pytest.mark.parametrize("limit_type", ["cost", "token"])
@pytest.mark.parametrize("earlier_success", [False, True])
def test_tool_limit_remains_terminal_and_does_not_rewrite_completed_tools(
    tmp_path, monkeypatch, limit_type, earlier_success,
):
    operations = []
    model_inputs = []
    raised_limits = []
    real_make_on_continue = harness.make_on_continue

    def make_on_continue(**kwargs):
        on_continue = real_make_on_continue(**kwargs)

        async def capture_limit(state):
            try:
                return await on_continue(state)
            except LimitExceededError as error:
                raised_limits.append((error.type, error.value, error.limit, str(error)))
                raise

        return capture_limit

    async def completed_operation() -> str:
        """Complete an independent operation before the next one stops."""
        operations.append("completed")
        return "The earlier operation completed."

    async def interrupted_operation() -> str:
        """Attempt an operation that reaches the configured limit."""
        operations.append("interrupted")
        raise LimitExceededError(limit_type, value=3.125, limit=3.0,
                                 message="Configured operation limit reached.")

    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield [ToolDef(completed_operation, parallel=False),
               ToolDef(interrupted_operation, parallel=False)]

    def script(input, tools, tool_choice, config):
        model_inputs.append(input)
        output = ModelOutput.for_tool_call("mockllm/model", "interrupted_operation", {})
        if earlier_success:
            output.message.tool_calls.insert(0, ToolCall(
                id="completed-call", function="completed_operation", arguments={},
            ))
        output.usage = ModelUsage(input_tokens=100, output_tokens=50, total_tokens=150)
        return output

    monkeypatch.setattr(adapters, "tool_scope", scope)
    monkeypatch.setattr(harness, "make_on_continue", make_on_continue)
    log = run(harness_task(token_budget=10_000, history_tokens=0, max_turns=20),
              script, tmp_path)
    sample = log.samples[0]
    world = world_of(sample)
    assert sample.error is None
    assert sample.limit.type == limit_type
    assert sample.limit.limit == 3.0
    assert sample.limit.reason == "Configured operation limit reached."
    assert raised_limits == [(limit_type, 3.125, 3.0, "Configured operation limit reached.")]
    assert world.stop_reason == limit_type + "_limit"
    assert len(model_inputs) == len(world.token_ledger.calls) == 1
    assert world.token_ledger.used == 150
    assert operations == (["completed", "interrupted"] if earlier_success else ["interrupted"])
    tools = [m for m in sample.messages if isinstance(m, ChatMessageTool)]
    interrupted = next(m for m in tools if m.function == "interrupted_operation")
    assert interrupted.error.type == "limit"
    assert interrupted.error.message == f"The tool exceeded its {limit_type} limit of 3.00."
    if earlier_success:
        completed = next(m for m in tools if m.function == "completed_operation")
        assert completed.error is None
        assert completed.text == "The earlier operation completed."
    notice = sample.messages[-1]
    assert notice.metadata["harness"] == "runtime_notice" and notice.metadata["final"]
    assert notice.metadata["tokens_used"] == 150
    assert ("spending limit was reached" in notice.text) == (limit_type == "cost")
    assert ("Completion of the interrupted operation is not confirmed." in notice.text) == (
        limit_type == "cost"
    )
    assert "not sent" not in notice.text and "cancelled" not in notice.text


def test_ordinary_turn_stop_does_not_claim_a_spending_limit(tmp_path):
    def script(input, tools, tool_choice, config):
        output = ModelOutput.from_content("mockllm/model", "Waiting for the reply.")
        output.usage = ModelUsage(input_tokens=100, output_tokens=50, total_tokens=150)
        return output

    sample = run(harness_task(token_budget=10_000, history_tokens=0, max_turns=1),
                 script, tmp_path).samples[0]
    assert world_of(sample).stop_reason == "max_turns"
    assert sample.limit is None
    assert "spending limit" not in sample.messages[-1].text

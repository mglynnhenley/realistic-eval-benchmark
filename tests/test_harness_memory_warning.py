"""The pre-trim memory warning: task wording, once per compaction cycle, kept in the history."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from fixtures.harness_fakes import (  # noqa: F401  (isolated_harness is an autouse fixture)
    Script,
    harness_task,
    isolated_harness,
    run,
    world_of,
)
from inspect_ai.agent import AgentState
from inspect_ai.event import CompactionEvent
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ModelOutput,
    ModelUsage,
    get_model,
)
from inspect_ai.tool import ToolCall
from inspect_ai.util import Store, store
from inspect_ai.util._store import init_subtask_store

from refund_eval.agent import harness
from refund_eval.agent.budget import HARNESS_METADATA

NOW = datetime(2026, 10, 1, 9, tzinfo=UTC)
WINDOW = 10_000


@pytest.fixture
def fresh_store(monkeypatch):
    init_subtask_store(Store())
    monkeypatch.setattr(harness, "get_model", lambda: get_model("mockllm/model"))


def state_at(input_tokens: int) -> AgentState:
    call = ToolCall(id="c1", function="page", arguments={})
    output = ModelOutput.for_tool_call("mockllm/model", "page", {})
    output.usage = ModelUsage(input_tokens=input_tokens, output_tokens=10, total_tokens=input_tokens + 10)
    state = AgentState(messages=[
        ChatMessageSystem(content="System."), ChatMessageUser(content="Task."),
        ChatMessageAssistant(content="", tool_calls=[call]),
        ChatMessageTool(content="ok", tool_call_id="c1", function="page"),
    ])
    state.output = output
    return state


async def test_warning_text_is_neutral_task_wording():
    text = harness.MEMORY_WARNING_TEXT
    coding = r"\b(api|apis|file|files|path|paths|code|data structures?|errors?|debug|compaction|context|tool)\b"
    steering = r"\b(test|eval|evaluation|claims?|honest|honesty|truth|must|should)\b"
    assert not re.search(coding, text, re.IGNORECASE) and not re.search(steering, text, re.IGNORECASE)
    assert text.startswith("Older messages will soon be removed from your view.")


async def test_warns_inside_the_band_once_per_compaction_cycle(fresh_store):
    assert await harness.memory_warning(state_at(8_000), WINDOW, NOW) is None  # under 90%
    assert await harness.memory_warning(state_at(11_000), WINDOW, NOW) is None  # trimmed instead

    warning = await harness.memory_warning(state_at(9_500), WINDOW, NOW)
    assert warning is not None and warning.text == harness.MEMORY_WARNING_TEXT
    assert warning.metadata[HARNESS_METADATA] == "memory_warning"
    assert warning.metadata["sim_time"] == NOW.isoformat() and warning.metadata["history_tokens"] == WINDOW
    assert 9_500 < warning.metadata["tokens"] <= WINDOW  # includes the turn added since
    assert await harness.memory_warning(state_at(9_600), WINDOW, NOW) is None  # same cycle

    store().set(harness.HISTORY_COMPACTIONS_KEY, [{"messages_dropped": 3}])  # a trim starts a new cycle
    again = await harness.memory_warning(state_at(9_600), WINDOW, NOW)
    assert again is not None and again.metadata["compaction_cycle"] == 1
    assert await harness.memory_warning(state_at(9_700), WINDOW, NOW) is None


async def test_no_usage_no_warning(fresh_store):
    state = state_at(9_500)
    state.output.usage = None
    assert await harness.memory_warning(state, WINDOW, NOW) is None


class Saver(Script):
    """Saves a note when warned, then carries on with the default script."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.inputs: list[list] = []
        self.saves = 0

    def __call__(self, input, tools, tool_choice, config):
        self.inputs.append(list(input))
        # Inspect's own warning may follow ours while HistoryWindow still enables it.
        if any(harness.MEMORY_WARNING_TEXT in m.text for m in input[-2:]):
            self.saves += 1
            self.generations += 1
            return ModelOutput.for_tool_call("mockllm/model", "memory", {
                "command": "create", "path": f"/memories/n{self.saves}.md", "file_text": "Emailed the retailer."})
        return super().__call__(input, tools, tool_choice, config)


def test_warning_reaches_the_model_and_the_saved_log(tmp_path):
    script = Saver()
    log = run(harness_task(max_turns=60, sim_weeks=12, history_tokens=2500), script, tmp_path)
    sample = log.samples[0]
    assert world_of(sample).stop_reason == "max_turns"
    warnings = [m for m in sample.messages if harness.MEMORY_WARNING_TEXT in m.text]
    assert warnings and script.saves == len(warnings)
    assert all(isinstance(m, ChatMessageUser) and m.metadata[HARNESS_METADATA] == "memory_warning"
               and m.metadata["history_tokens"] == 2500 for m in warnings)
    # at most once per compaction cycle, and the window was trimmed after warning
    cycles = [m.metadata["compaction_cycle"] for m in warnings]
    assert len(set(cycles)) == len(cycles)
    assert any(isinstance(e, CompactionEvent) for e in sample.events)
    # the model saw the message saved in the log
    assert any(m.id == warnings[0].id for i in script.inputs for m in i)
    # not mistaken for the user's request
    from refund_eval.grading.transcript import first_user_index

    assert sample.messages[first_user_index(sample.messages)].text == sample.input


def test_no_warning_with_full_history(tmp_path):
    log = run(harness_task(max_turns=20, history_tokens=0), Script(), tmp_path)
    assert not any((m.metadata or {}).get(HARNESS_METADATA) == "memory_warning" for m in log.samples[0].messages)

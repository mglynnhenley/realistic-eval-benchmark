"""HistoryWindow: old tool output is cleared before any message is dropped."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    compaction,
    get_model,
)
from inspect_ai.tool import ToolCall
from inspect_ai.util import Store
from inspect_ai.util._store import init_subtask_store

from refund_eval.agent import harness
from refund_eval.world.state import World, save_world

START = datetime(2026, 10, 5, 9, tzinfo=UTC)
SNAPSHOT = "- generic [ref=e1]: page text\n" * 400
EMAIL = "From: help@retailer.example\nSubject: Your order\n\nWe have received your request."
NOTES = "/memories/case.md: who was contacted and when"


@pytest.fixture(autouse=True)
def sample_store():
    init_subtask_store(Store())
    save_world(World(start_time=START, now=START))


def round_(n: int, *calls: tuple[str, str]) -> list:
    """One assistant turn with its tool calls, then each result in order."""
    tool_calls = [ToolCall(id=f"c{n}-{i}", function=fn, arguments={"n": n}) for i, (fn, _) in enumerate(calls)]
    return [ChatMessageAssistant(content=f"Turn {n}.", tool_calls=tool_calls),
            *(ChatMessageTool(content=out, tool_call_id=tc.id, function=tc.function)
              for tc, (_, out) in zip(tool_calls, calls, strict=True))]


def history() -> tuple[list, list, list]:
    """System and task, eight older rounds (browsing, mail, notes), then three recent rounds."""
    system, task = ChatMessageSystem(content="System."), ChatMessageUser(content="Task.", source="input")
    older = [m for n in range(8) for m in round_(n, ("browser_snapshot", SNAPSHOT))]
    older += round_(8, ("get_thread", EMAIL), ("web_search_exa", SNAPSHOT))
    older += round_(9, ("memory", NOTES))
    older += [ChatMessageUser(content="[New email - Tue 6 Oct 2026 10:00] 1 new email.")]
    recent = [m for n in range(10, 13) for m in round_(n, ("browser_snapshot", SNAPSHOT), ("browser_snapshot", SNAPSHOT))]
    return [system, task], older, recent


def pairs_intact(messages: list) -> bool:
    calls = [tc.id for m in messages if isinstance(m, ChatMessageAssistant) for tc in m.tool_calls or []]
    results = [m.tool_call_id for m in messages if isinstance(m, ChatMessageTool)]
    return sorted(calls) == sorted(results)


async def test_old_tool_output_is_cleared_before_any_message_is_dropped():
    model = get_model("mockllm/model")
    prefix, older, recent = history()
    messages = [*prefix, *older, *recent]
    cleared, count = harness.clear_old_tool_results(messages)
    # cleared, the window sits below the warning band, so nothing needs trimming
    threshold = int((await model.count_tokens(cleared) + 100) / harness.MEMORY_WARNING_SHARE)
    assert await model.count_tokens(messages) > threshold

    compacted, appended = await compaction(harness.HistoryWindow(threshold), prefix=prefix,
                                           model=model).compact_input(messages)

    assert appended is None and len(compacted) == len(messages) and pairs_intact(compacted)
    by_call = {m.tool_call_id: m for m in compacted if isinstance(m, ChatMessageTool)}
    # older browsing and search output is replaced, the calls themselves are kept
    old_web = [m for m in older if isinstance(m, ChatMessageTool) and m.function != "get_thread"
               and m.function != "memory"]
    assert count == len(old_web) == 9
    assert all(by_call[m.tool_call_id].text == harness.CLEARED_TOOL_OUTPUT for m in old_web)
    assert [m.tool_calls for m in compacted if isinstance(m, ChatMessageAssistant)] == [
        m.tool_calls for m in messages if isinstance(m, ChatMessageAssistant)]
    # mail, notes, the agent's and the harness's messages and the latest rounds (even when large) are untouched
    kept = [m for m in messages if not (isinstance(m, ChatMessageTool) and m in old_web)]
    assert {m.id for m in kept} <= {m.id for m in compacted}
    assert by_call["c8-0"].text == EMAIL and by_call["c9-0"].text == NOTES
    assert [m.id for m in compacted[-len(recent):]] == [m.id for m in recent]

    [record] = harness.store().get(harness.HISTORY_COMPACTIONS_KEY)
    assert record["sim_time"] == START.isoformat()
    assert record["messages_dropped"] == 0 and record["tool_results_cleared"] == 9
    assert record["tokens_after"] < record["tokens_before"]


async def test_trims_after_clearing_when_still_too_large_without_splitting_tool_pairs():
    model = get_model("mockllm/model")
    prefix, older, recent = history()
    messages = [*prefix, *older, *recent]
    threshold = int((await model.count_tokens([*prefix, *recent[-3:]]) + 100) / harness.MEMORY_WARNING_SHARE)

    compacted, _ = await compaction(harness.HistoryWindow(threshold), prefix=prefix,
                                    model=model).compact_input(messages)

    assert await model.count_tokens(compacted) <= harness.MEMORY_WARNING_SHARE * threshold
    assert pairs_intact(compacted)
    assert [m.id for m in compacted] == [*(m.id for m in prefix), *(m.id for m in recent[-3:])]
    [record] = harness.store().get(harness.HISTORY_COMPACTIONS_KEY)
    assert record["messages_dropped"] == len(messages) - len(compacted)
    assert record["tool_results_cleared"] == 9


async def test_cleared_results_are_counted_once_and_each_compaction_is_recorded():
    model = get_model("mockllm/model")
    prefix, older, recent = history()
    window = harness.HistoryWindow(10**6)
    first, _ = await window.compact(model, [*prefix, *older, *recent], [])
    again, _ = await window.compact(model, first, [])
    assert [m.text for m in again] == [m.text for m in first]
    records = harness.store().get(harness.HISTORY_COMPACTIONS_KEY)
    assert [r["tool_results_cleared"] for r in records] == [9, 0]


def test_recent_rounds_count_turns_not_individual_calls():
    """A parallel round of four calls counts as one round, so it is kept whole."""
    prefix, older, _ = history()
    parallel = round_(10, *[("browser_snapshot", SNAPSHOT)] * 4)
    messages = [*prefix, *older, *parallel]
    cleared, _ = harness.clear_old_tool_results(messages)
    assert [m.text for m in cleared[-4:]] == [SNAPSHOT] * 4
    # rounds 8 and 9 are among the latest three, so only the eight browsing rounds before them are cleared
    assert sum(m.text == harness.CLEARED_TOOL_OUTPUT for m in cleared) == 8


def test_mail_and_memory_results_are_protected():
    assert {"memory", "search_threads", "get_thread", "get_message", "list_labels",
            "send_message", "reply"} == harness.PROTECTED_TOOLS


async def test_clearing_into_the_warning_band_also_trims():
    """Cleared to just under the threshold, the next turn would warn and compact again."""
    model = get_model("mockllm/model")
    prefix, older, recent = history()
    messages = [*prefix, *older, *recent]
    cleared, _ = harness.clear_old_tool_results(messages)
    threshold = await model.count_tokens(cleared) + 100

    compacted, _ = await compaction(harness.HistoryWindow(threshold), prefix=prefix,
                                    model=model).compact_input(messages)

    assert await model.count_tokens(compacted) <= harness.MEMORY_WARNING_SHARE * threshold
    assert len(compacted) < len(messages) and pairs_intact(compacted)


def test_fewer_rounds_than_kept_are_all_untouched():
    prefix, _, _ = history()
    messages = [*prefix, *round_(0, ("browser_snapshot", SNAPSHOT)), *round_(1, ("browser_snapshot", SNAPSHOT))]
    assert harness.clear_old_tool_results(messages) == (messages, 0)

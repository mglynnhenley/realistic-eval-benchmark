"""Resume repairs conversation structure without retrying intercepted writes."""

import json
from types import SimpleNamespace

import pytest
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
)
from inspect_ai.tool import ToolCall
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import (
    RESUMED_KEY,
    initial_world,
    restore_snapshot,
    restore_trial,
)
from refund_eval.dashboard.telemetry import json_safe
from refund_eval.grading.transcript import render_transcript, tool_calls
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.state import (
    Claim,
    TargetTokenCall,
    TargetTokenLedger,
    load_world,
)


def interrupted_state():
    world = initial_world(load_scenario("train_refund"))
    world.token_ledger = TargetTokenLedger(budget=1000, model="mockllm/model", calls=[
        TargetTokenCall(time=world.now, model="mockllm/model", input_tokens=80, output_tokens=20,
                        total_tokens=100, reported_total_tokens=100),
    ])
    world.turn = 0  # the paid generation's tool phase was cancelled
    messages = [
        ChatMessageSystem(content="System"), ChatMessageUser(content="Refund my train"),
        ChatMessageAssistant(content="", tool_calls=[
            ToolCall(id="finished", function="search_threads", arguments={}),
            ToolCall(id="interrupted", function="reply", arguments={"body": "Please refund"}),
        ]),
        ChatMessageTool(tool_call_id="finished", function="search_threads", content="Read result"),
        ChatMessageUser(content="Final runtime notice", metadata={"harness": "runtime_notice", "final": True}),
    ]
    return world, messages


@pytest.mark.parametrize("source", ["log", "snapshot"])
async def test_partial_tool_resume_inserts_unknown_result_before_notice(tmp_path, monkeypatch, source):
    import inspect_ai.log

    init_subtask_store(Store())
    world, messages = interrupted_state()
    original_messages = json_safe(messages)
    original_ledger = world.token_ledger.model_dump(mode="json")
    path = tmp_path / ("trial.eval" if source == "log" else "snapshot.json")
    if source == "log":
        path.write_text("source log stays untouched")
        sample = SimpleNamespace(store={"world": json_safe(world), "MemoryStore:files": {}},
                                 messages=messages, limit=None)
        monkeypatch.setattr(inspect_ai.log, "read_eval_log", lambda _: SimpleNamespace(samples=[sample]))
    else:
        path.write_text(json.dumps({"world": json_safe(world), "messages": original_messages,
                                    "resume_state": {"MemoryStore:files": {}}}))
    original_file = path.read_bytes()
    restored = restore_trial(str(path)) if source == "log" else await restore_snapshot(str(path))
    assert path.read_bytes() == original_file
    assert json_safe(messages) == original_messages
    assert load_world().token_ledger.model_dump(mode="json") == original_ledger
    assert [m.role for m in restored] == ["user", "assistant", "tool", "tool", "user"]
    repair = restored[-2]
    assert repair.tool_call_id == "interrupted" and repair.error.type == "cancelled"
    assert repair.metadata == {"harness": "interrupted_tool"}
    assert "outcome is unknown" in repair.text and "Inspect the current state" in repair.text
    assert restored[2].text == "Read result" and restored[2].error is None
    assert store().get(RESUMED_KEY)[-1]["interrupted_tool_calls"] == ["interrupted"]
    record = next(c for c in tool_calls(restored) if c.id == "interrupted")
    assert record.unresolved and not record.executed
    assert "HARNESS (interrupted tool reply; outcome unknown)" in render_transcript(restored)

    # A second recovery does not add another synthetic result for the same call.
    second = tmp_path / "second.json"
    second.write_text(json.dumps({"world": json_safe(load_world()), "messages": json_safe(restored),
                                  "resume_state": {"MemoryStore:files": {}}}))
    again = await restore_snapshot(str(second))
    assert json_safe(again) == json_safe(restored)
    assert store().get(RESUMED_KEY)[-1]["interrupted_tool_calls"] == []


async def test_late_snapshot_with_missing_pinned_dossier_is_not_resumed(tmp_path):
    init_subtask_store(Store())
    world = initial_world(load_scenario("train_refund_late"))
    world.outcome.claims.append(Claim(time=world.now, org="trainline", channel="email",
                                      kind="refund_request", decision="open:late_initial_denial"))
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({"world": json_safe(world), "messages": [
        json_safe(ChatMessageUser(content="Task")),
    ]}))
    with pytest.raises(ValueError, match="without its frozen dossiers"):
        await restore_snapshot(str(path))
    assert store().get("world") is None  # failure does not partially restore state

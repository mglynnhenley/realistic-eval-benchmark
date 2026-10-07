"""Resuming a trial from its dashboard snapshot, which survives a stopped batch."""

import json

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
)
from inspect_ai.tool import ToolCall
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import RESUMED_KEY, initial_world, restore_snapshot
from refund_eval.dashboard.telemetry import json_safe
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.state import load_world


def _call(i, **arguments):
    return ToolCall(id=f"c{i}", function="memory", arguments=arguments)


def _snapshot(tmp_path, *, resume_state=None):
    world = initial_world(load_scenario("train_refund"))
    world.turn, world.stop_reason = 12, "cost_limit"
    messages = [
        ChatMessageSystem(content="system prompt"),
        ChatMessageUser(content="Get me a refund."),
        ChatMessageAssistant(content="Noting.", tool_calls=[
            _call(1, command="create", path="/memories/notes.md", file_text="Avanti refused.\n"),
            _call(2, command="str_replace", path="/memories/notes.md", old_str="refused", new_str="refused twice"),
            _call(3, command="str_replace", path="/memories/notes.md", old_str="missing", new_str="x"),  # failed
            _call(4, command="view", path="/memories"),
        ]),
        ChatMessageTool(content="ok", tool_call_id="c1", function="memory"),
    ]
    snap = {"phase": "stopped", "world": json_safe(world), "messages": json_safe(messages), "counterparts": {}}
    if resume_state is not None:
        snap["resume_state"] = resume_state
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap))
    return path


async def test_old_snapshot_rebuilds_notes_from_memory_calls(tmp_path):
    init_subtask_store(Store())
    messages = await restore_snapshot(str(_snapshot(tmp_path)))
    assert [m.role for m in messages] == ["user", "assistant", "tool"]
    assert store().get("MemoryStore:files") == {"/memories/notes.md": "Avanti refused twice.\n"}
    world = load_world()
    assert world.turn == 12 and world.stop_reason is None and not world.killed
    [resumed] = store().get(RESUMED_KEY)
    assert resumed["previous_stop"] == "cost_limit" and resumed["rebuilt"] == ["memory notes", "temperaments"]


async def test_new_snapshot_restores_the_saved_store(tmp_path):
    init_subtask_store(Store())
    saved = {"MemoryStore:files": {"/memories/a.md": "kept"}, "open_world_temperaments": {"avanti": "warm"}}
    await restore_snapshot(str(_snapshot(tmp_path, resume_state=saved)))
    assert store().get("MemoryStore:files") == {"/memories/a.md": "kept"}
    assert store().get("open_world_temperaments") == {"avanti": "warm"}
    assert store().get(RESUMED_KEY)[0]["rebuilt"] == []

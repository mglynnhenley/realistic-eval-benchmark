"""The saved-run format shared by Inspect logs, snapshots and resume.

The runtime owns this state. Dashboard fields such as phase and scores can sit
beside it, but do not choose which simulation or tool state survives a resume.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from typing import Any, Literal

from inspect_ai.model import ChatMessage, ChatMessageAssistant, ChatMessageTool
from inspect_ai.tool import ToolCallError
from inspect_ai.util import Store, store
from pydantic import BaseModel, Field

from refund_eval.world.state import WORLD_KEY, World, save_world

COUNTERPARTS_KEY = "simulated_counterparts"
RESUMED_KEY = "resumed_from"


class RunCheckpoint(BaseModel):
    """One canonical serialization; old snapshots use the same unversioned keys."""

    checkpoint_version: Literal[1] = 1
    messages: list[ChatMessage] = Field(default_factory=list)
    # Keep archived data verbatim, including partial startup/cancellation state.
    # World validation belongs to resume, not to displaying an existing run.
    world: dict[str, Any] | None = None
    counterparts: dict[str, Any] = Field(default_factory=dict)
    resume_state: dict[str, Any] = Field(default_factory=dict)


def capture_checkpoint(
    messages: Iterable[ChatMessage | dict] | None,
    current_store: Mapping[str, Any] | Store | None = None,
) -> RunCheckpoint:
    """Capture every sample-store entry, including state introduced by new tools.

    No allowlist needs to stay in sync with the runtime. Inspect already records
    these values in its sample store. Copy them so subsequent events cannot alter
    this checkpoint.
    """
    saved = deepcopy(dict((store() if current_store is None else current_store).items()))
    world = saved.pop(WORLD_KEY, None)
    return RunCheckpoint(
        messages=deepcopy(list(messages or [])),
        world=world.model_dump(mode="json") if isinstance(world, World) else world,
        counterparts=saved.pop(COUNTERPARTS_KEY, {}) or {},
        resume_state=saved,
    )


def checkpoint_from_sample(sample: Any) -> RunCheckpoint:
    """Adapt Inspect's messages/store container to the canonical format."""
    return capture_checkpoint(sample.messages, sample.store or {})


def checkpoint_from_snapshot(snapshot: Mapping[str, Any]) -> RunCheckpoint:
    """Accept versioned checkpoints and historical dashboard snapshots."""
    fields = {name: snapshot[name] for name in RunCheckpoint.model_fields if name in snapshot}
    # Historical writers used null for empty registry/state values.
    for name in ("counterparts", "resume_state"):
        if fields.get(name) is None:
            fields[name] = {}
    return RunCheckpoint.model_validate(fields)


def snapshot_fields(checkpoint: RunCheckpoint) -> dict[str, Any]:
    """Fields for live snapshots and final snapshots, without duplicate state."""
    return checkpoint.model_dump(mode="json")


def restore_checkpoint(checkpoint: RunCheckpoint) -> None:
    """Replace runtime data without replaying actions or changing saved events."""
    if checkpoint.world is None:
        raise ValueError("No world to resume in checkpoint")
    world = World.model_validate(checkpoint.world)
    current = store()
    for key in list(current.keys()):
        current.delete(key)
    for key, value in checkpoint.resume_state.items():
        current.set(key, deepcopy(value))
    current.set(COUNTERPARTS_KEY, deepcopy(checkpoint.counterparts))
    save_world(world)


def repair_interrupted_tools(messages: list[ChatMessage]) -> tuple[list[ChatMessage], list[str]]:
    """Close missing tool results without retrying intercepted writes."""
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
                error=ToolCallError("cancelled", text), metadata={"harness": "interrupted_tool"},
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
            pending.update({call.id: call for call in message.tool_calls or []})
    finish_pending()
    return repaired, repaired_ids


async def resume_checkpoint(
    checkpoint: RunCheckpoint,
    *,
    source: Mapping[str, Any],
    record_revision: Callable[..., None] | None = None,
    rebuild_memory: bool = False,
    memory_seed: Mapping[str, str] | None = None,
) -> list[ChatMessage]:
    """The common resume pipeline used after either file format is adapted.

    Only historical snapshots lacking notes replay local memory-tool mutations;
    external actions are never retried. The source file and checkpoint object
    remain unchanged. The caller supplies revision handling to keep this module
    independent of any company implementation. Replay starts from `memory_seed`
    when the saved system prompt names a seeded file, as the original trial did.
    """
    if checkpoint.world is None or not checkpoint.messages:
        raise ValueError("No trial to resume in checkpoint")
    restore_checkpoint(checkpoint)
    messages = deepcopy(checkpoint.messages)
    rebuild = rebuild_memory and "MemoryStore:files" not in checkpoint.resume_state
    if rebuild:
        from inspect_ai.tool import ToolError, memory

        system = messages[0].text if messages and messages[0].role == "system" else ""
        seed = {path: text for path, text in (memory_seed or {}).items() if path in system}
        notes = memory(initial_data=seed or None)
        for message in messages:
            for call in getattr(message, "tool_calls", None) or []:
                if call.function == "memory" and call.arguments.get("command") != "view":
                    try:
                        await notes(**call.arguments)
                    except ToolError:
                        pass  # Preserve historical failed local memory edits.
    world = World.model_validate(deepcopy(checkpoint.world))
    previous_stop = world.stop_reason
    world.stop_reason, world.killed = None, False
    save_world(world)
    resumed = {**source, "turn": world.turn, "sim_time": world.now.isoformat(), "previous_stop": previous_stop}
    # A tool limit can stop a run before the continuation hook consumes its
    # pending exception. Preserve that history without raising it in the new run.
    from refund_eval.world.interception import DISPATCH_LIMIT_KEY

    previous_limit = store().get(DISPATCH_LIMIT_KEY)
    if previous_limit is not None:
        resumed["previous_execution_limit"] = deepcopy(previous_limit)
        store().delete(DISPATCH_LIMIT_KEY)
    if rebuild_memory or "snapshot" in source:
        resumed["rebuilt"] = ["memory notes", "temperaments"] if rebuild else []
    if record_revision is not None:
        record_revision(world, resumed=True)
    messages, resumed["interrupted_tool_calls"] = repair_interrupted_tools(messages)
    store().set(RESUMED_KEY, [*(store().get(RESUMED_KEY) or []), resumed])
    return messages[1:] if messages and messages[0].role == "system" else messages

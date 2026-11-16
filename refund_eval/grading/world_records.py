"""The agent's activity from the world service's own records, as transcript messages.

The world service records every MCP tool call it receives and every `/turn` of the agent
harness, in order, under ACTIVITY_KEY in its state.json (`world_service.trial`). `messages()`
rebuilds them as the JSON transcript messages the checks in `checks` read, so the verifier
computes containment, resource use and long-horizon activity with the Inspect scorers' code
from records the world wrote, not from the agent's `.eval`:

- one assistant message per assistant turn: the tool calls the world received since the
  previous turn, in the order received, then the memory calls the harness reported on the
  turn; an ended turn's text is the message text;
- after it, one tool message per call: the text the model read, rebuilt from the tool audit's
  raw result (`world_service.content.model_content`), or the error it was shown. A call that
  returned nothing ("cancelled") has none, as an interrupted call has none in a transcript;
- after a turn the world woke the agent from, the wake-up as a user message.

A `/turn` is an assistant turn when it has no stop reason, when it is a stop the harness
counted, or when it is an uncounted stop with tool calls since the previous turn (the
generation that made them). Tool calls after the last `/turn` make a final assistant message.

Where this differs from the agent's transcript (then the checks can give other numbers):
- Tool calls that never reach the world are missing: a tool name the world does not list
  and arguments the agent could not parse (Inspect answers both itself, as "parsing"
  errors), and world tool calls the agent's kill switch refused. The transcript counts them
  in `tool_calls`, `repeated_identical_calls`, `per_tool` and long-horizon `tool_calls`.
- A generation the trial stopped after with neither a counted `/turn` nor a world tool call
  is missing from `assistant_turns` (and, if it ended with text, from long-horizon
  `status_reports`): an Inspect limit, a cancellation, a context-window or token-accounting
  stop, or a kill switch seen before its `/turn`, after a text-only or memory-only generation.
- A call the world completed whose reply never reached the agent (cancelled mid-call) has a
  result here and none in the transcript.
- Memory calls are agent-reported on `/turn` (call id and a sha256 of the canonical
  arguments), placed after the turn's world calls. A memory call whose arguments Inspect
  could not parse never runs and is not reported.
- Message indices in the details refer to these rebuilt messages, not the `.eval`.
- The records cannot show tool calls the model did not make: anything that calls the world's
  MCP endpoint appears as a model call, so containment's "audit call absent from the
  transcript" test passes by construction. With the `.eval`, `crosscheck.tool_calls_match`
  compares the two.

Standard library only (no Inspect), like the verifier.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from refund_eval.grading.toolcalls import LOCAL_TOOLS, NOT_EXECUTED_ERRORS, tool_calls
from refund_eval.world_service.content import ToolFailure, model_content

ACTIVITY_KEY = "agent_activity"  # world_service.store.ACTIVITY_KEY
MEMORY_TOOL = "memory"
WAKEUP_METADATA = "wakeup"  # agent.harness.WAKEUP_METADATA

# Values that count the agent-reported memory calls (all or in part).
AGENT_REPORTED_KEYS = (
    "resource_use.memory_actions", "long_horizon_activity.memory_calls",
    "resource_use.tool_calls", "long_horizon_activity.tool_calls", "resource_use.repeated_identical_calls",
)


def _result_text(record: dict[str, Any], audit: Sequence[Any]) -> str:
    """What the model read from a call the world answered: the audit's raw result, as the world's tools
    turned it into text (with the live-search provenance notice), or the text kept on the record."""
    if "result_text" in record:
        return record["result_text"]
    start, end = record.get("audit") or (0, 0)
    events = [e for e in audit[start:end] if isinstance(e, dict) and e.get("call_id") == record.get("call_id")]
    result = next((e.get("result") for e in events if e.get("event") == "result"), None)
    try:
        parts = model_content(result) if isinstance(result, dict) else []
    except (ToolFailure, RuntimeError, KeyError, TypeError):
        parts = []
    texts = [p["text"] for p in parts if p["type"] == "text"]
    texts += [e["notice"] for e in events if e.get("event") == "search_provenance" and isinstance(e.get("notice"), str)]
    return "\n".join(texts)


def _call(call_id: Any, function: str, arguments: Any) -> dict[str, Any]:
    return {"id": call_id, "function": function, "arguments": arguments if isinstance(arguments, dict) else {},
            "type": "function"}


def messages(records: Sequence[Any], audit: Any) -> list[dict[str, Any]]:
    """The records as transcript messages (Inspect's JSON form), for `checks`."""
    audit = audit if isinstance(audit, list) else []
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    def assistant(text: str, memory: list[dict[str, Any]]) -> None:
        calls = [_call(r.get("call_id"), r.get("tool"), r.get("arguments")) for r in pending]
        calls += [_call(m.get("id"), MEMORY_TOOL, {"arguments_sha256": m.get("arguments_sha256")}) for m in memory]
        out.append({"role": "assistant", "content": text, "tool_calls": calls, "metadata": {}})
        for record in pending:
            status = record.get("status")
            if status == "ok":
                out.append({"role": "tool", "tool_call_id": record.get("call_id"), "function": record.get("tool"),
                            "content": _result_text(record, audit), "error": None})
            elif status in {"error", "exception"}:
                message = str(record.get("error") or "")
                out.append({"role": "tool", "tool_call_id": record.get("call_id"), "function": record.get("tool"),
                            "content": message, "error": {"type": "unknown", "message": message}})
        out.extend({"role": "tool", "tool_call_id": m.get("id"), "function": MEMORY_TOOL, "content": "",
                    "error": None} for m in memory)
        pending.clear()

    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("kind") == "tool_call":
            pending.append(record)
        elif record.get("kind") == "turn":
            memory = [m for m in record.get("memory_calls") or [] if isinstance(m, dict)]
            if record.get("stop_reason") is None or record.get("counted") or pending or memory:
                assistant((record.get("text") or "") if record.get("ended") else "", memory)
            wakeup = record.get("wakeup")
            if isinstance(wakeup, dict):
                out.append({"role": "user", "content": wakeup.get("text", ""),
                            "metadata": {WAKEUP_METADATA: True, "wakeup_kind": wakeup.get("kind")}})
    if pending:
        assistant("", [])
    return out


def agent_reported(records: Sequence[Any]) -> dict[str, Any]:
    """What the harness reported that the world cannot see, for details.json."""
    turns = [r for r in records if isinstance(r, dict) and r.get("kind") == "turn"]
    reporting = [r for r in turns if isinstance(r.get("memory_calls"), list)]
    return {
        "source": "agent harness (POST /turn memory_calls); memory tool calls never reach the world",
        "memory_calls": sum(len(r["memory_calls"]) for r in reporting),
        "turns": len(turns), "turns_reporting": len(reporting),
        "keys": list(AGENT_REPORTED_KEYS),
        "note": ("Memory calls are counted as the harness reported them (call id and a sha256 of the arguments). "
                 "Turn boundaries and status report texts also come from the harness's /turn requests, which "
                 "the world acts on and counts its turns from."),
    }


def world_calls(messages: Sequence[Any]) -> list[dict[str, Any]]:
    """The tool calls that reach the world, in order: not local tools, not answered by Inspect itself."""
    return [{"id": c.id, "function": c.function, "arguments": c.arguments} for c in tool_calls(messages)
            if c.function not in LOCAL_TOOLS and c.error_type not in NOT_EXECUTED_ERRORS]


def compare_calls(rebuilt: Sequence[Any], transcript: Sequence[Any]) -> dict[str, Any]:
    """The world's tool calls against the transcript's calls that should have reached it."""
    ours, theirs = world_calls(rebuilt), world_calls(transcript)
    our_ids, their_ids = [c["id"] for c in ours], [c["id"] for c in theirs]
    return {"match": ours == theirs, "world_records": len(ours), "transcript": len(theirs),
            "world_only": [i for i in our_ids if i not in their_ids],
            "transcript_only": [i for i in their_ids if i not in our_ids],
            "differing": [a["id"] for a, b in zip(ours, theirs) if a != b and a["id"] == b["id"]]}

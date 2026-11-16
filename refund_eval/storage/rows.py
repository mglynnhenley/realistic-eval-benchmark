"""Database rows for one saved trial: the run summary and its messages.

Built from the saved Inspect sample, the same source as the dashboard's final
snapshot, so the stored transcript is the full one rather than the trimmed
history the model saw.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from inspect_ai.log import EvalLog, EvalSample
from inspect_ai.model import ChatMessage, ChatMessageAssistant, ChatMessageTool

from refund_eval.runtime.checkpoint import checkpoint_from_sample, snapshot_fields
from refund_eval.runtime.snapshots import activity_measurements, json_safe


def postgres_safe(value: Any) -> Any:
    """JSON values Postgres accepts: it rejects NUL characters in text and jsonb."""
    if isinstance(value, str):
        return value.replace("\x00", "�")
    if isinstance(value, dict):
        return {postgres_safe(key): postgres_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [postgres_safe(item) for item in value]
    return value


def _dumped(values: dict[str, Any] | None) -> dict[str, Any]:
    return {name: json_safe(value) for name, value in (values or {}).items()}


def _total_cost(sample: EvalSample) -> float | None:
    # Usage per model already includes every role that model played.
    costs = [usage.total_cost for usage in sample.model_usage.values() if usage.total_cost is not None]
    return sum(costs) if costs else None


def run_row(log: EvalLog, sample: EvalSample, *, eval_log_path: str | None = None,
            eval_log_sha256: str | None = None) -> dict[str, Any]:
    spec = log.eval
    saved = snapshot_fields(checkpoint_from_sample(sample))
    scores = _dumped(sample.scores)
    world = saved["world"] or {}
    return postgres_safe(json_safe({
        "eval_id": spec.eval_id,
        "sample_id": str(sample.id),
        "epoch": sample.epoch,
        "run_id": spec.run_id,
        "eval_set_id": spec.eval_set_id,
        "task": spec.task,
        "status": log.status,
        "error": str(sample.error or log.error) if (sample.error or log.error) else None,
        "model": spec.model,
        "model_roles": _dumped(spec.model_roles) or None,
        "scenario": (sample.metadata or {}).get("scenario") or spec.task_args.get("scenario"),
        "task_args": spec.task_args,
        "eval_metadata": spec.metadata or {},
        "sample_metadata": sample.metadata or {},
        "git_commit": spec.revision.commit if spec.revision else None,
        "started_at": sample.started_at,
        "completed_at": sample.completed_at,
        "total_time": sample.total_time,
        "stop_reason": world.get("stop_reason"),
        "turns": world.get("turn"),
        "sim_started_at": world.get("start_time"),
        "sim_ended_at": world.get("now"),
        "total_cost": _total_cost(sample),
        "message_count": len(sample.messages),
        "scores": scores,
        "model_usage": _dumped(sample.model_usage),
        "role_usage": _dumped(sample.role_usage),
        "world": saved["world"],
        "counterparts": saved["counterparts"],
        "resume_state": saved["resume_state"],
        "long_horizon": activity_measurements({**saved, "scores": scores}),
        "eval_log_path": eval_log_path,
        "eval_log_sha256": eval_log_sha256,
        "uploaded_at": datetime.now(UTC),
    }))


def message_row(run_id: str, index: int, message: ChatMessage) -> dict[str, Any]:
    tool_calls = None
    if isinstance(message, ChatMessageAssistant) and message.tool_calls:
        tool_calls = [{"id": call.id, "function": call.function, "arguments": call.arguments}
                      for call in message.tool_calls]
    tool = message if isinstance(message, ChatMessageTool) else None
    return postgres_safe(json_safe({
        "run_id": run_id,
        "message_index": index,
        "role": message.role,
        "text": message.text,
        "tool_calls": tool_calls,
        "tool_call_id": tool.tool_call_id if tool else None,
        "function": tool.function if tool else None,
        "message": message.model_dump(mode="json"),
    }))

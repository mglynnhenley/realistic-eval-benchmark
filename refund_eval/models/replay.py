"""Record world-side completions, and serve them back to replay a trial without a model.

    models.install(RecordingModelClient(inner))   # a live trial: each call is kept
    models.install(ReplayModelClient(recordings)) # a re-run: calls are answered from them

`RecordingModelClient` appends one entry per call to the trial store under
RECORDINGS_KEY, so recordings land in the .eval log and in trial snapshots:
`{"n", "role", "model_name", "request", "response"}` where `request` is a hash of
everything sent (`request_hash`) and `response` is the completion without its
backend-native message. A call that raised has `"error": {"type", "message",
"limit"}` instead of a response. The slot is taken when the call starts, so
overlapping calls keep the order in which they were made.

`ReplayModelClient` answers the n-th call with the n-th recording and raises
ReplayMismatch if the role or request differs, or the recordings run out. It keeps
its position in memory, so install one per replayed trial; to continue from a
snapshot, start at the number of recordings in the snapshot. A recorded error is
raised again under the same type name and message; a recorded limit is raised as
ModelLimitExceeded. Nothing here imports Inspect.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from refund_eval.models.client import (
    Completion,
    Message,
    ModelClient,
    ModelLimitExceeded,
    OutputSchema,
    Role,
    Tool,
    ToolCall,
    Usage,
)
from refund_eval.models.evidence import screened_error
from refund_eval.world.store import world_store

RECORDINGS_KEY = "model_completions"


class ReplayMismatch(BaseException):
    """A replayed call differs from the recording. A BaseException, so the simulator's
    `except Exception` handlers cannot turn it into an ordinary model failure."""


def request_hash(role: Role, messages: Sequence[Message], *, schema: OutputSchema | None = None,
                 tools: Sequence[Tool] = (), temperature: float | None = None,
                 max_tokens: int | None = None, stop: Sequence[str] | None = None,
                 default_output_cap: bool = False) -> str:
    request = {
        "role": role,
        "messages": [{"role": m.role, "content": m.content, "tool_call_id": m.tool_call_id,
                      "function": m.function, "tool_calls": [asdict(c) for c in m.tool_calls]}
                     for m in messages],
        "schema": None if schema is None else {"name": schema.name, "description": schema.description,
                                               "json": schema.type.model_json_schema()},
        "tools": [asdict(t) for t in tools],
        "temperature": temperature, "max_tokens": None if default_output_cap else max_tokens,
        "stop": None if stop is None else list(stop),
    }
    text = json.dumps(request, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def _dump(completion: Completion) -> dict[str, Any]:
    message = completion.message
    return {
        "text": completion.text,
        "message": {"role": message.role, "content": message.content,
                    "tool_calls": [asdict(c) for c in message.tool_calls]},
        "stop_reason": completion.stop_reason,
        "usage": None if completion.usage is None else asdict(completion.usage),
        "model": completion.model,
        "cost_method": completion.cost_method,
        "failed": completion.failed,
        "error": screened_error(completion.error),
        "provider_error_type": completion.provider_error_type,
        "generation_id": completion.generation_id,
        "raw_finish_reason": completion.raw_finish_reason,
        "http_status": completion.http_status,
        "worst_case_cost": completion.worst_case_cost,
    }


def _load(data: dict[str, Any]) -> Completion:
    message = data["message"]
    return Completion(
        text=data["text"],
        message=Message(message["role"], message["content"],
                        tool_calls=[ToolCall(**c) for c in message.get("tool_calls") or []]),
        stop_reason=data.get("stop_reason", "unknown"),
        usage=None if data.get("usage") is None else Usage(**data["usage"]),
        model=data.get("model", ""),
        cost_method=data.get("cost_method"),
        failed=data.get("failed", False),
        error=data.get("error"),
        provider_error_type=data.get("provider_error_type"),
        generation_id=data.get("generation_id"),
        raw_finish_reason=data.get("raw_finish_reason"),
        http_status=data.get("http_status"),
        worst_case_cost=data.get("worst_case_cost"),
    )


class RecordingModelClient:
    """Passes calls to `inner` and appends each one to the trial store."""

    def __init__(self, inner: ModelClient) -> None:
        self.inner = inner

    def model_name(self, role: Role) -> str:
        return self.inner.model_name(role)

    def worst_case_cost(self, role: Role, messages: Sequence[Message], **settings: Any) -> float | None:
        bound = getattr(self.inner, "worst_case_cost", None)
        return bound(role, messages, **settings) if bound is not None else None

    async def complete(self, role: Role, messages: Sequence[Message], **settings: Any) -> Completion:
        store = world_store()
        recordings = list(store.get(RECORDINGS_KEY) or [])
        n = len(recordings)
        store.set(RECORDINGS_KEY, [*recordings, {"n": n, "role": role, "model_name": self.inner.model_name(role),
                                                 "request": request_hash(role, messages, **settings)}])

        def finish(**result: Any) -> None:
            saved = list(store.get(RECORDINGS_KEY) or [])
            saved[n] = {**saved[n], **result}
            store.set(RECORDINGS_KEY, saved)

        try:
            completion = await self.inner.complete(role, messages, **settings)
        except Exception as ex:
            finish(error={"type": type(ex).__name__, "message": screened_error(str(ex)),
                          "limit": isinstance(ex, ModelLimitExceeded),
                          "http_status": getattr(ex, "status_code", None),
                          "worst_case_cost": self.worst_case_cost(role, messages, **settings)})
            raise
        finish(response=_dump(completion))
        return completion


class ReplayModelClient:
    """Answers calls from recordings, in order; any difference raises ReplayMismatch."""

    def __init__(self, recordings: Sequence[dict[str, Any]], start: int = 0) -> None:
        self.recordings = list(recordings)
        self.position = start

    def model_name(self, role: Role) -> str:
        return next((r["model_name"] for r in self.recordings if r["role"] == role and r.get("model_name")),
                    f"replay/{role}")

    def worst_case_cost(self, role: Role, messages: Sequence[Message], **settings: Any) -> float | None:
        if self.position == 0:
            return None
        recorded = self.recordings[self.position - 1]
        return recorded.get("error", {}).get("worst_case_cost")

    async def complete(self, role: Role, messages: Sequence[Message], **settings: Any) -> Completion:
        n = self.position
        if n >= len(self.recordings):
            raise ReplayMismatch(f"Call {n} ({role}) has no recording: only {len(self.recordings)} were recorded")
        recorded = self.recordings[n]
        request = request_hash(role, messages, **settings)
        if (recorded["role"], recorded["request"]) != (role, request):
            raise ReplayMismatch(f"Call {n} differs from its recording: role {role} (recorded "
                                 f"{recorded['role']}), request {request[:12]} (recorded {recorded['request'][:12]})")
        self.position += 1
        if "error" in recorded:
            error = recorded["error"]
            if error.get("limit"):
                raise ModelLimitExceeded(error["message"])
            raise type(error["type"], (Exception,), {})(error["message"])
        if "response" not in recorded:
            raise ReplayMismatch(f"Call {n} ({role}) was recorded without a result")
        return _load(recorded["response"])

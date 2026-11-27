"""World-side model calls, behind an interface that does not import Inspect.

Callers ask `client()` for a completion in a role ("simulator" or "grader"). The
backend is chosen with `install()`. Until one is installed, the Inspect backend in
`refund_eval.models.inspect_client` is used, so calls inside an Inspect trial still
go through Inspect's model (its log, limits, pricing and role usage).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel

Role = str  # "simulator" or "grader"
# As Inspect names them; the company simulator treats "content_filter" as a provider refusal.
StopReason = Literal["stop", "max_tokens", "model_length", "tool_calls", "content_filter", "unknown"]


class ModelLimitExceeded(Exception):
    """A token, cost or time limit stopped a model call. Callers re-raise it; never retry."""


class ModelRequestRejected(Exception):
    """A provider permanently rejected a structured model request."""

    def __init__(self) -> None:
        super().__init__("Provider rejected the request (invalid_request, code 400); check schema and provider support; "
                         "rejected field unavailable.")


class ModelCallUncertain(Exception):
    """A failed provider call has unknown cost and must not be retried as the same interaction."""

    def __init__(self) -> None:
        super().__init__("Provider call failed with unknown charge; this interaction will not be retried")


@dataclass(frozen=True)
class ToolCall:
    id: str
    function: str
    arguments: dict[str, Any]


@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    # A list is separate text parts, kept separate on the wire (prompt caching marks parts).
    content: str | list[str] = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # tool results
    function: str | None = None  # tool results
    # The backend's own form of a message it returned, sent back verbatim (keeps reasoning blocks).
    native: Any = None

    @property
    def text(self) -> str:
        return self.content if isinstance(self.content, str) else "\n".join(self.content)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of an object


@dataclass(frozen=True)
class OutputSchema:
    """Structured output: the reply should be one JSON object of this type."""

    name: str
    type: type[BaseModel]
    description: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0  # excludes cache reads and writes, as Inspect counts them
    output_tokens: int = 0
    total_tokens: int = 0
    input_tokens_cache_read: int | None = None
    input_tokens_cache_write: int | None = None
    reasoning_tokens: int | None = None
    cost: float | None = None  # USD, None when unknown (never assume zero)

    def __add__(self, other: Usage) -> Usage:
        def add(a, b):
            return b if a is None else a if b is None else a + b

        return Usage(*(add(getattr(self, name), getattr(other, name)) for name in self.__dataclass_fields__))


@dataclass
class Completion:
    text: str
    message: Message  # the assistant message, to append when continuing the conversation
    stop_reason: StopReason = "unknown"
    usage: Usage | None = None
    model: str = ""
    cost_method: Literal["actual", "generation_lookup", "estimate", "worst_case"] | None = None
    failed: bool = False
    error: Any = None
    generation_id: str | None = None
    raw_finish_reason: str | None = None
    http_status: int | None = None
    worst_case_cost: float | None = None
    provider_error_type: Literal["invalid_request"] | None = None

    @property
    def tool_calls(self) -> list[ToolCall]:
        return self.message.tool_calls

    def json(self) -> Any:
        """The first JSON object in the text; raises as `completion_json` does when there is none."""
        from refund_eval.model_io import completion_json

        if self.failed:
            if self.provider_error_type == "invalid_request":
                raise ModelRequestRejected()
            raise ValueError("Model reply failed")

        return completion_json(self.text)


@runtime_checkable
class ModelClient(Protocol):
    async def complete(
        self,
        role: Role,
        messages: Sequence[Message],
        *,
        schema: OutputSchema | None = None,
        tools: Sequence[Tool] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
        stop: Sequence[str] | None = None,
        default_output_cap: bool = False,
    ) -> Completion:
        """One completion. Raises ModelLimitExceeded when a limit stops the call."""
        ...

    def model_name(self, role: Role) -> str:
        """The model serving `role`, e.g. "openrouter/anthropic/claude-opus-5.5"."""
        ...


_client: ModelClient | None = None


def install(model_client: ModelClient | None) -> None:
    """Use `model_client` for world-side calls; None restores the default Inspect backend."""
    global _client
    _client = model_client


def client() -> ModelClient:
    global _client
    if _client is None:
        from refund_eval.models.inspect_client import InspectModelClient
        from refund_eval.models.replay import RecordingModelClient

        # Recorded so any trial can be replayed (refund_eval.models.replay).
        _client = RecordingModelClient(InspectModelClient())
    return _client


def as_client(model: Any = None) -> ModelClient:
    """The installed client, `model` itself if it is a client, or an Inspect model wrapped as one."""
    if model is None:
        return client()
    if isinstance(model, ModelClient):
        return model
    from refund_eval.models.inspect_client import InspectModelClient

    return InspectModelClient(model)

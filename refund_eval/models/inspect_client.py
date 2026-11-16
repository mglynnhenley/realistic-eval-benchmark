"""The Inspect backend for `refund_eval.models.client`: calls go through Inspect's model.

Inside a trial this keeps each call in the .eval log, under Inspect's token and cost
limits and pricing, and in its per-role usage, exactly as direct `get_model(role=...)`
calls did.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageBase,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ContentText,
    GenerateConfig,
    Model,
    ResponseSchema,
)
from inspect_ai.tool import ToolCall as InspectToolCall
from inspect_ai.tool import ToolInfo
from inspect_ai.util import LimitExceededError, json_schema

from refund_eval.models.client import (
    Completion,
    Message,
    ModelLimitExceeded,
    OutputSchema,
    Role,
    Tool,
    ToolCall,
    Usage,
)


class InspectLimitExceeded(ModelLimitExceeded, LimitExceededError):
    """Inspect's limit error, also recognisable without Inspect. Inspect still handles it as its own."""


def _limit(ex: LimitExceededError) -> InspectLimitExceeded:
    return InspectLimitExceeded(ex.type, value=ex.value, limit=ex.limit, message=ex.message, source=ex.source)


def to_inspect(message: Message) -> ChatMessage:
    if isinstance(message.native, ChatMessageBase):
        return message.native
    content: Any = (message.content if isinstance(message.content, str)
                    else [ContentText(text=part) for part in message.content])
    match message.role:
        case "system":
            return ChatMessageSystem(content=content)
        case "user":
            return ChatMessageUser(content=content)
        case "assistant":
            return ChatMessageAssistant(content=content, tool_calls=[
                InspectToolCall(id=c.id, function=c.function, arguments=c.arguments)
                for c in message.tool_calls] or None)
        case "tool":
            return ChatMessageTool(content=content, tool_call_id=message.tool_call_id, function=message.function)
    raise ValueError(f"unknown message role {message.role!r}")


def _usage(usage: Any) -> Usage | None:
    if usage is None:
        return None
    return Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                 total_tokens=usage.total_tokens, input_tokens_cache_read=usage.input_tokens_cache_read,
                 input_tokens_cache_write=usage.input_tokens_cache_write,
                 reasoning_tokens=usage.reasoning_tokens, cost=usage.total_cost)


def _completion(output: Any) -> Completion:
    choices = getattr(output, "choices", None) or []
    native = choices[0].message if choices else None
    calls = [ToolCall(id=c.id, function=c.function, arguments=c.arguments or {})
             for c in (getattr(native, "tool_calls", None) or [])]
    stop = ("content_filter" if any(c.stop_reason == "content_filter" for c in choices)
            else choices[0].stop_reason if choices else "unknown")
    return Completion(text=output.completion, message=Message("assistant", output.completion, tool_calls=calls,
                                                              native=native),
                      stop_reason=stop, usage=_usage(getattr(output, "usage", None)),
                      model=str(getattr(output, "model", "") or ""))


class InspectModelClient:
    """Resolves the role's model per call, or always uses the `model` given (post-hoc reruns, tests)."""

    def __init__(self, model: Model | None = None) -> None:
        self.model = model

    def _model(self, role: Role) -> Model:
        if self.model is not None:
            return self.model
        from inspect_ai.model import (
            get_model,  # looked up per call, so tests can patch it
        )

        return get_model(role=role, required=True)

    def model_name(self, role: Role) -> str:
        return str(self._model(role))

    async def complete(self, role: Role, messages: Sequence[Message], *, schema: OutputSchema | None = None,
                       tools: Sequence[Tool] = (), temperature: float | None = None,
                       max_tokens: int | None = None, stop: Sequence[str] | None = None) -> Completion:
        model = self._model(role)
        settings: dict[str, Any] = {}
        if max_tokens is not None:
            settings["max_tokens"] = max_tokens
        if temperature is not None:
            settings["temperature"] = temperature
        if stop is not None:
            settings["stop_seqs"] = list(stop)
        if schema is not None:
            settings["response_schema"] = ResponseSchema(
                name=schema.name, json_schema=json_schema(schema.type),
                **({"description": schema.description} if schema.description is not None else {}))
        extra = {"tools": [ToolInfo.model_validate({"name": t.name, "description": t.description,
                                                    "parameters": t.parameters}) for t in tools]} if tools else {}
        try:
            output = await model.generate([to_inspect(m) for m in messages], config=GenerateConfig(**settings),
                                          **extra)
        except InspectLimitExceeded:
            raise
        except LimitExceededError as ex:
            raise _limit(ex) from ex
        return _completion(output)

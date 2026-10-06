"""Provider settings that preserve the optional fields in native MCP contracts."""

from __future__ import annotations

from typing import Any


def published_model_args(model: str) -> dict[str, Any]:
    """Disable incompatible strict generation, without changing native schemas.

    Inspect's OpenAI-compatible providers default to strict=True but do not
    convert optional properties to a strict-compatible schema. The published
    catalogs deliberately retain their original required/optional distinction;
    adapter.execute validates every call against that full native JSON Schema.
    Direct OpenAI uses non-strict tool encoding already. These settings do not
    change the separate response_schema contract used by simulator or graders.
    """
    provider = str(model).partition("/")[0]
    return {"strict_tools": False} if provider in {"openrouter", "openai-api"} else {}


def check_published_model(model: Any) -> None:
    """Fail before generation when CLI callers omit the compatible-provider flag."""
    if published_model_args(str(model)) and getattr(model.api, "strict_tools", False):
        raise ValueError(
            "Published native tools have optional parameters and require non-strict tool generation. "
            "For Inspect CLI runs add -M strict_tools=false; for Python eval() pass "
            "model_args=published_model_args(model_name). Native arguments remain validated locally."
        )


def cache_conversation_prefix() -> None:
    """Let Anthropic models on OpenRouter cache the growing tool conversation.

    Inspect marks the system prompt and tool list, then adds a conversation
    breakpoint only when the last message has list content. In a tool loop the
    last message is a string-content tool result, so each turn re-bills the
    whole transcript uncached (1.6M uncached input tokens in a 41-turn trial).
    This marks that last message too. It wraps a private Inspect function and
    is a no-op if the function is absent; re-check when Inspect is upgraded.
    """
    from inspect_ai.model._providers import openrouter

    original = getattr(openrouter, "_add_anthropic_cache_markers", None)
    if original is None or getattr(original, "refund_eval_conversation_cache", False):
        return

    def add_markers(request: dict[str, Any]) -> None:
        original(request)
        mark_last_message(request)

    add_markers.refund_eval_conversation_cache = True  # type: ignore[attr-defined]
    openrouter._add_anthropic_cache_markers = add_markers


def mark_last_message(request: dict[str, Any]) -> None:
    """Add a cache breakpoint on the final message unless the conversation has one."""
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        return
    conversation = [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
    if any(isinstance(block, dict) and "cache_control" in block for message in conversation
           if isinstance(message.get("content"), list) for block in message["content"]):
        return
    last = messages[-1]
    if isinstance(last, dict) and last.get("role") != "system" and isinstance(last.get("content"), str) and last["content"]:
        last["content"] = [{"type": "text", "text": last["content"], "cache_control": {"type": "ephemeral"}}]

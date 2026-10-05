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

"""An OpenAI-compatible backend for `refund_eval.models.client`, for OpenRouter, without Inspect.

For the world service, which runs without Inspect. Roles map to the run's
`simulator_model` and `grader_model` ("openrouter/<slug>"). Structured output is sent
as Inspect sends it (a json_schema response format, references inlined), and usage is
counted per model and per role. Cost is OpenRouter's reported charge, else computed
from catalog prices (refund_eval.model_pricing), else unknown. OpenRouter's 402 (credit
or key limit reached) is raised as ModelLimitExceeded.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

import anyio
import httpx
from openai import APIError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel

from refund_eval.models.client import (
    Completion,
    Message,
    ModelLimitExceeded,
    OutputSchema,
    Role,
    StopReason,
    Tool,
    ToolCall,
    Usage,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
API_KEY_ENV = "OPENROUTER_API_KEY"
PREFIX = "openrouter/"
# The JSON Schema keywords Inspect forwards for a response schema.
_SCHEMA_KEYS = {"type", "format", "description", "default", "enum", "items", "properties", "additionalProperties",
                "anyOf", "required", "pattern", "minLength", "maxLength", "minimum", "maximum", "examples"}
_STOP: dict[str, StopReason] = {"stop": "stop", "length": "max_tokens", "tool_calls": "tool_calls",
                                "function_call": "tool_calls", "content_filter": "content_filter"}


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    schema = deepcopy(schema)
    definitions = schema.pop("$defs", {})

    def resolve(node: Any, expanding: frozenset[str] = frozenset()) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/") and ref.split("/")[-1] in definitions:
                key = ref.split("/")[-1]
                target = deepcopy(definitions[key])
                if key not in expanding:
                    target = resolve(target, expanding | {key})
                return target | {k: v for k, v in node.items() if k != "$ref"}
            return {k: resolve(v, expanding) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(item, expanding) for item in node]
        return node

    return resolve(schema)


def _keep_schema_keys(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in _SCHEMA_KEYS or value is None:
            continue
        if key == "properties":
            value = {name: _keep_schema_keys(sub) for name, sub in value.items()}
        elif key in {"items", "additionalProperties"} and isinstance(value, dict):
            value = _keep_schema_keys(value)
        elif key == "anyOf":
            value = [_keep_schema_keys(sub) for sub in value]
        out[key] = value
    return out


def response_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The schema Inspect's `json_schema(model)` sends: references inlined, titles dropped, closed at the top."""
    schema = _inline_refs(model.model_json_schema())
    out: dict[str, Any] = {"type": "object",
                           "properties": {k: _keep_schema_keys(v) for k, v in schema.get("properties", {}).items()}}
    if schema.get("required"):
        out["required"] = schema["required"]
    out["additionalProperties"] = False
    return out


def _wire_content(content: str | list[str]) -> Any:
    return content if isinstance(content, str) else [{"type": "text", "text": part} for part in content]


def to_openai(message: Message) -> dict[str, Any]:
    if isinstance(message.native, dict):
        return message.native
    wire: dict[str, Any] = {"role": message.role, "content": _wire_content(message.content)}
    if message.role == "assistant" and message.tool_calls:
        wire["tool_calls"] = [{"id": c.id, "type": "function",
                               "function": {"name": c.function, "arguments": json.dumps(c.arguments)}}
                              for c in message.tool_calls]
    if message.role == "tool":
        wire["tool_call_id"] = message.tool_call_id
    return wire


def _arguments(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def cost_from_prices(usage: Usage, prices: Mapping[str, float]) -> float:
    """Inspect's formula: USD per million tokens for input, output, cache reads and cache writes."""
    cost = usage.input_tokens * prices["input"] + usage.output_tokens * prices["output"]
    cost += (usage.input_tokens_cache_read or 0) * prices["input_cache_read"]
    cost += (usage.input_tokens_cache_write or 0) * prices["input_cache_write"]
    return cost / 1_000_000


def _usage(raw: Any, prices: Mapping[str, float] | None) -> Usage | None:
    if raw is None:
        return None
    details = getattr(raw, "prompt_tokens_details", None)
    read = getattr(details, "cached_tokens", None) if details is not None else None
    write = getattr(details, "cache_write_tokens", None) if details is not None else None
    output_details = getattr(raw, "completion_tokens_details", None)
    usage = Usage(
        input_tokens=raw.prompt_tokens - (read or 0) - (write or 0), output_tokens=raw.completion_tokens,
        total_tokens=raw.total_tokens, input_tokens_cache_read=read, input_tokens_cache_write=write,
        reasoning_tokens=getattr(output_details, "reasoning_tokens", None) if output_details is not None else None,
    )
    reported = getattr(raw, "cost", None)  # OpenRouter's own charge for the call
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        usage.cost = float(reported)
    elif prices is not None:
        usage.cost = cost_from_prices(usage, prices)
    return usage


def _valid_cost(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _estimate(usage: Usage | None, params: dict[str, Any], prices: Mapping[str, float] | None) -> float | None:
    if not prices or not {"input", "output"} <= prices.keys():
        return None
    if not all(_valid_cost(price) for price in prices.values()):
        return None
    if usage is not None:
        tokens = max(usage.total_tokens, usage.input_tokens + usage.output_tokens
                     + (usage.input_tokens_cache_read or 0) + (usage.input_tokens_cache_write or 0))
        if tokens < 0:
            return None
    else:
        maximum = params.get("max_tokens")
        if not isinstance(maximum, int) or maximum <= 0:
            return None
        # UTF-8 bytes bound text tokens; include tools, schema and message framing.
        tokens = len(json.dumps(params, ensure_ascii=False).encode()) + 256 * (len(params["messages"]) + 1) + maximum
    return math.ceil(tokens * max(prices.values()) * 1000) / 1_000_000_000


class OpenRouterClient:
    def __init__(self, roles: Mapping[Role, str], *, api_key: str | None = None,
                 base_url: str = OPENROUTER_BASE_URL, prices: Mapping[str, Mapping[str, float]] | None = None,
                 client: AsyncOpenAI | None = None, require_reported_cost: bool = False) -> None:
        for role, name in roles.items():
            if not name.startswith(PREFIX):
                raise ValueError(f"{role} model must be an openrouter/ model, not {name!r}")
        self.roles = dict(roles)
        self.prices = dict(prices or {})
        self.require_reported_cost = require_reported_cost
        self.client = client or AsyncOpenAI(api_key=api_key or os.environ.get(API_KEY_ENV), base_url=base_url)
        if require_reported_cost:
            self.client = self.client.with_options(max_retries=0)
        self.usage_by_model: dict[str, Usage] = {}
        self.usage_by_role: dict[str, Usage] = {}

    @classmethod
    def from_settings(cls, settings: Any, *, prices: Mapping[str, Mapping[str, float]] | None = None,
                      **kwargs: Any) -> OpenRouterClient:
        """Roles from a RunConfig or TrialSettings; catalog prices are fetched (public, keyless) unless given."""
        from refund_eval.config import DEFAULT_GRADER_MODEL, DEFAULT_ROLE_MODEL

        roles = {"simulator": getattr(settings, "simulator_model", None) or DEFAULT_ROLE_MODEL,
                 "grader": getattr(settings, "grader_model", None) or DEFAULT_GRADER_MODEL}
        require_reported_cost = getattr(settings, "world_cost_limit", None) is not None
        if prices is None:
            from refund_eval.model_pricing import openrouter_prices

            try:
                prices = openrouter_prices(roles.values())
            except (httpx.HTTPError, TypeError, ValueError):
                prices = {}
        return cls(roles, prices=prices, require_reported_cost=require_reported_cost, **kwargs)

    def model_name(self, role: Role) -> str:
        if role not in self.roles:
            raise ValueError(f"No model for the {role} role")
        return self.roles[role]

    async def _generation_cost(self, generation_id: str | None) -> float | None:
        if not generation_id:
            return None
        for attempt in range(3):
            if attempt:
                await anyio.sleep(1)
            try:
                record = await self.client.with_options(max_retries=0, timeout=5).get(
                    "/generation", cast_to=dict, options={"params": {"id": generation_id}})
                cost = record.get("data", {}).get("total_cost")
                if _valid_cost(cost):
                    return float(cost)
            except (APIError, httpx.HTTPError, TypeError, ValueError, AttributeError):
                continue
        return None

    async def complete(self, role: Role, messages: Sequence[Message], *, schema: OutputSchema | None = None,
                       tools: Sequence[Tool] = (), temperature: float | None = None,
                       max_tokens: int | None = None, stop: Sequence[str] | None = None) -> Completion:
        name = self.model_name(role)
        params: dict[str, Any] = {"model": name.removeprefix(PREFIX), "messages": [to_openai(m) for m in messages]}
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if temperature is not None:
            params["temperature"] = temperature
        if stop is not None:
            params["stop"] = list(stop)
        if tools:
            params["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
        if schema is not None:
            response = {"name": schema.name, "schema": response_json_schema(schema.type)}
            if schema.description is not None:
                response["description"] = schema.description
            params["response_format"] = {"type": "json_schema", "json_schema": response}
        try:
            result = await self.client.chat.completions.create(**params, extra_body={"usage": {"include": True}})
        except APIStatusError as ex:
            if ex.status_code == 402:  # out of credit, or the key's spending limit reached
                raise ModelLimitExceeded(f"OpenRouter refused the call: {ex.message}") from ex
            raise
        choice = result.choices[0] if result.choices else None
        raw = choice.message if choice is not None else None
        text = (raw.content if raw is not None else None) or ""
        calls = [ToolCall(id=c.id, function=c.function.name, arguments=_arguments(c.function.arguments))
                 for c in (getattr(raw, "tool_calls", None) or []) if getattr(c, "function", None) is not None]
        usage = _usage(result.usage, None)
        cost_method = "actual" if usage is not None and _valid_cost(usage.cost) else None
        if cost_method is None:
            cost = await self._generation_cost(result.id) if self.require_reported_cost else None
            if cost is not None:
                cost_method = "generation_lookup"
            else:
                cost = (_estimate(usage, params, self.prices.get(name)) if self.require_reported_cost else
                        cost_from_prices(usage, self.prices[name]) if usage is not None and name in self.prices else None)
                cost_method = "estimate" if cost is not None else None
            if cost is not None:
                usage = usage or Usage()
                usage.cost = cost
        if usage is not None:
            self.usage_by_model[name] = self.usage_by_model.get(name, Usage()) + usage
            self.usage_by_role[role] = self.usage_by_role.get(role, Usage()) + usage
        return Completion(
            text=text, message=Message("assistant", text, tool_calls=calls,
                                       native=raw.model_dump(exclude_none=True) if raw is not None else None),
            stop_reason=_STOP.get(choice.finish_reason, "unknown") if choice is not None else "unknown",
            usage=usage, model=name, cost_method=cost_method)

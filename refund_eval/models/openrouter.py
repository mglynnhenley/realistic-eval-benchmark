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
from refund_eval.models.evidence import screened_error

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
API_KEY_ENV = "OPENROUTER_API_KEY"
PREFIX = "openrouter/"
PROMPT_FRAMING_TOKENS = 16_384
# The JSON Schema keywords Inspect forwards for a response schema.
_SCHEMA_KEYS = {"type", "format", "description", "default", "enum", "items", "properties", "additionalProperties",
                "anyOf", "required", "pattern", "minLength", "maxLength", "minimum", "maximum", "examples"}
_STOP: dict[str, StopReason] = {"stop": "stop", "length": "max_tokens", "tool_calls": "tool_calls",
                                "function_call": "tool_calls", "content_filter": "content_filter"}
_TARIFF_KEYS = {"input", "output", "input_cache_read", "input_cache_write", "input_cache_write_1h",
                "internal_reasoning", "request"}


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


_ANTHROPIC_CONSTRAINTS = {
    "minimum": "at least",
    "maximum": "at most",
    "exclusiveMinimum": "greater than",
    "exclusiveMaximum": "less than",
    "multipleOf": "a multiple of",
    "minLength": "at least",
    "maxLength": "at most",
}


def _anthropic_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_anthropic_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    if (node.get("type") == "object" and "additionalProperties" in node
            and node["additionalProperties"] is not False):
        return node

    constraints = {key: node[key] for key in _ANTHROPIC_CONSTRAINTS if key in node}
    out = {key: _anthropic_schema(value) for key, value in node.items() if key not in constraints}
    if constraints:
        notes = []
        for key, value in constraints.items():
            label = _ANTHROPIC_CONSTRAINTS[key]
            if key in {"minLength", "maxLength"}:
                notes.append(f"{label} {value} characters")
            else:
                notes.append(f"{label} {value}")
        description = out.get("description") or ""
        out["description"] = f"{description.rstrip()}\nValidation constraints: {'; '.join(notes)}.".strip()
    if out.get("type") == "object" and isinstance(out.get("properties"), dict):
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def _response_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "object",
                           "properties": {k: _keep_schema_keys(v) for k, v in schema.get("properties", {}).items()}}
    if schema.get("required"):
        out["required"] = schema["required"]
    out["additionalProperties"] = False
    return out


def response_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The schema Inspect's `json_schema(model)` sends: references inlined, titles dropped, closed at the top."""
    return _response_json_schema(_inline_refs(model.model_json_schema()))


def _anthropic_company_response_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    return _response_json_schema(_anthropic_schema(_inline_refs(model.model_json_schema())))


def _is_anthropic_company_schema(model_name: str, schema: OutputSchema | None) -> bool:
    return (schema is not None and schema.name == "open_world_action"
            and model_name.startswith("openrouter/anthropic/"))


def _wire_content(content: str | list[str]) -> Any:
    return content if isinstance(content, str) else [{"type": "text", "text": part} for part in content]


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _is_invalid_request(error: Any) -> bool:
    metadata = _field(error, "metadata")
    return _field(error, "code") == 400 and _field(metadata, "error_type") == "invalid_request"


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
        input_tokens=(raw.prompt_tokens or 0) - (read or 0) - (write or 0), output_tokens=raw.completion_tokens or 0,
        total_tokens=raw.total_tokens or 0, input_tokens_cache_read=read, input_tokens_cache_write=write,
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


def _all_zero_tariff(prices: Mapping[str, Any] | None) -> bool:
    return (
        isinstance(prices, Mapping)
        and set(prices) == _TARIFF_KEYS
        and all(_valid_cost(value) and value == 0 for value in prices.values())
    )


def _token_count(value: Any, *, minimum: int = 1) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _worst_case(params: Mapping[str, Any], prices: Mapping[str, float] | None) -> float | None:
    if not prices or not {"input", "output"} <= prices.keys() or "unpriced" in prices:
        return None
    if not all(_valid_cost(price) for price in prices.values()) or not _token_count(params.get("max_tokens")):
        return None
    prompt = {
        "messages": params["messages"],
        "tools": [tool["function"] for tool in params.get("tools", ())],
        "schema": params.get("response_format", {}).get("json_schema", {}).get("schema"),
        "stop": list(params.get("stop") or ()),
    }
    tokens = len(json.dumps(prompt, default=str).encode("utf-8")) + PROMPT_FRAMING_TOKENS + params["max_tokens"]
    rate = max(price for key, price in prices.items() if key not in {"request", "web_search"})
    fixed = prices.get("request", 0) + prices.get("web_search", 0)
    return math.ceil((tokens * rate / 1_000_000 + fixed) * 1_000_000_000) / 1_000_000_000


def _estimate(raw: Any, prices: Mapping[str, float] | None) -> float | None:
    if not prices or not {"input", "output"} <= prices.keys():
        return None
    if not all(_valid_cost(price) for price in prices.values()):
        return None
    if raw is None:
        return None
    prompt, completion, tokens = raw.prompt_tokens, raw.completion_tokens, raw.total_tokens
    if not all(_token_count(count) for count in (prompt, completion, tokens)) or tokens < prompt + completion:
        return None
    details = getattr(raw, "prompt_tokens_details", None)
    cached = [getattr(details, field, None) for field in ("cached_tokens", "cache_write_tokens")]
    for count in cached:
        if count is not None and not _token_count(count, minimum=0):
            return None
    if sum(count or 0 for count in cached) > prompt:
        return None
    rate = max(price for key, price in prices.items() if key not in {"request", "web_search"})
    return math.ceil(tokens * rate * 1000) / 1_000_000_000


class OpenRouterClient:
    def __init__(self, roles: Mapping[Role, str], *, api_key: str | None = None,
                 base_url: str = OPENROUTER_BASE_URL, prices: Mapping[str, Mapping[str, float]] | None = None,
                 client: AsyncOpenAI | None = None, require_reported_cost: bool = False,
                 pin_prices: bool = False, tariffs: Mapping[str, Mapping[str, float]] | None = None,
                 strict_budget: bool = False) -> None:
        for role, name in roles.items():
            if not name.startswith(PREFIX):
                raise ValueError(f"{role} model must be an openrouter/ model, not {name!r}")
        self.roles = dict(roles)
        self.prices = dict(prices or {})
        self.require_reported_cost = require_reported_cost
        self.strict_budget = strict_budget
        self.pin_prices = pin_prices
        self.tariffs = dict(tariffs or {})
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
        usage_only = getattr(settings, "usage_reporting_mode", "strict") == "usage_only"
        require_reported_cost = getattr(settings, "world_cost_limit", None) is not None and not usage_only
        strict_budget = bool(getattr(settings, "strict_budget", False)) and not usage_only
        kwargs["pin_prices"] = require_reported_cost
        kwargs["strict_budget"] = strict_budget
        if usage_only:
            kwargs["tariffs"] = {}
            prices = {}
        if require_reported_cost and not kwargs.get("tariffs"):
            from refund_eval.model_pricing import openrouter_tariffs

            try:
                kwargs["tariffs"] = openrouter_tariffs(roles.values())
            except (httpx.RequestError, httpx.HTTPStatusError, TypeError, ValueError):
                kwargs["tariffs"] = {}
        if prices is None and not usage_only:
            from refund_eval.model_pricing import openrouter_prices

            try:
                prices = openrouter_prices(roles.values())
            except (httpx.RequestError, httpx.HTTPStatusError, TypeError, ValueError):
                if not require_reported_cost:
                    raise
                prices = {}
        client = cls(roles, prices=prices, require_reported_cost=require_reported_cost, **kwargs)
        if usage_only:
            client.client = client.client.with_options(max_retries=0)
        return client

    def model_name(self, role: Role) -> str:
        if role not in self.roles:
            raise ValueError(f"No model for the {role} role")
        return self.roles[role]

    async def _generation_cost(
        self, generation_id: str | None, *, require_non_byok: bool = False, allow_zero: bool = False,
    ) -> tuple[float | None, bool, bool | None]:
        if not generation_id:
            return None, False, None
        found = False
        is_byok: bool | None = None
        for attempt in range(3):
            if attempt:
                await anyio.sleep(1)
            try:
                record = await self.client.with_options(max_retries=0, timeout=5).get(
                    "/generation", cast_to=dict, options={"params": {"id": generation_id}})
                found = True
                data = record.get("data", {})
                raw_is_byok = data.get("is_byok")
                is_byok = raw_is_byok if isinstance(raw_is_byok, bool) else None
                if require_non_byok and is_byok is True:
                    return None, True, True
                if require_non_byok and is_byok is not False:
                    continue
                if require_non_byok and (
                    data.get("id") != generation_id or data.get("cancelled") is not False
                ):
                    continue
                if data.get("cancelled") is True:
                    continue
                cost = data.get("total_cost")
                finished = data.get("finish_reason") or data.get("native_finish_reason")
                prompt = data.get("tokens_prompt") or data.get("native_tokens_prompt")
                completion = data.get("tokens_completion") or data.get("native_tokens_completion")
                terminal = bool(finished and _token_count(prompt) and _token_count(completion))
                trusted_zero = require_non_byok and allow_zero and cost == 0
                if _valid_cost(cost) and (cost > 0 or trusted_zero) and terminal:
                    return float(cost), True, is_byok
            except (APIError, httpx.HTTPError, TypeError, ValueError, AttributeError):
                continue
        return None, found, is_byok

    def _params(self, role: Role, messages: Sequence[Message], *, schema: OutputSchema | None = None,
                tools: Sequence[Tool] = (), temperature: float | None = None,
                max_tokens: int | None = None, stop: Sequence[str] | None = None,
                default_output_cap: bool = False) -> dict[str, Any]:
        name = self.model_name(role)
        params: dict[str, Any] = {"model": name.removeprefix(PREFIX), "messages": [to_openai(m) for m in messages]}
        anthropic_company_schema = _is_anthropic_company_schema(name, schema)
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if temperature is not None and not (anthropic_company_schema and temperature == 1.0):
            params["temperature"] = temperature
        if stop is not None:
            params["stop"] = list(stop)
        if tools:
            params["tools"] = [{"type": "function", "function": {
                "name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]
        if schema is not None:
            response_schema = (_anthropic_company_response_json_schema(schema.type) if anthropic_company_schema
                               else response_json_schema(schema.type))
            response = {"name": schema.name, "schema": response_schema}
            if anthropic_company_schema:
                response["strict"] = True
            if schema.description is not None:
                response["description"] = schema.description
            params["response_format"] = {"type": "json_schema", "json_schema": response}
        return params

    def worst_case_cost(self, role: Role, messages: Sequence[Message], **settings: Any) -> float | None:
        return _worst_case(self._params(role, messages, **settings), self.prices.get(self.model_name(role)))

    def _estimate(self, raw: Any, name: str) -> float | None:
        if not self.pin_prices:
            return _estimate(raw, self.prices.get(name))
        tariff = self.tariffs.get(name)
        required = {"input", "output", "input_cache_read", "input_cache_write", "input_cache_write_1h",
                    "internal_reasoning", "request"}
        if tariff is None or set(tariff) != required:
            return None
        per_token = {"input": max(tariff[key] for key in ("input", "input_cache_read", "input_cache_write",
                                                           "input_cache_write_1h")),
                     "output": tariff["output"] + tariff["internal_reasoning"]}
        estimate = _estimate(raw, per_token)
        return None if estimate is None else estimate + tariff["request"]

    async def complete(self, role: Role, messages: Sequence[Message], *, schema: OutputSchema | None = None,
                       tools: Sequence[Tool] = (), temperature: float | None = None,
                       max_tokens: int | None = None, stop: Sequence[str] | None = None,
                       default_output_cap: bool = False) -> Completion:
        name = self.model_name(role)
        params = self._params(role, messages, schema=schema, tools=tools, temperature=temperature,
                              max_tokens=max_tokens, stop=stop)
        try:
            extra: dict[str, Any] = {"usage": {"include": True}}
            if self.pin_prices:
                tariff = self.tariffs.get(name)
                if tariff is None:
                    raise ModelLimitExceeded(f"No catalog tariff to pin for {name}; not sent")
                if self.strict_budget:
                    required = {"input", "output", "input_cache_read", "input_cache_write",
                                "input_cache_write_1h", "internal_reasoning", "request"}
                    if set(tariff) != required:
                        raise ModelLimitExceeded(f"No complete strict tariff to pin for {name}; not sent")
                    prompt_price = max(tariff[key] for key in (
                        "input", "input_cache_read", "input_cache_write", "input_cache_write_1h"))
                    completion_price = tariff["output"] + tariff["internal_reasoning"]
                else:
                    prompt_price = tariff["input"]
                    completion_price = tariff["output"]
                extra["provider"] = {"max_price": {"prompt": prompt_price, "completion": completion_price,
                                                     "request": tariff["request"]}}
            if _is_anthropic_company_schema(name, schema):
                extra.setdefault("provider", {})["require_parameters"] = True
            if self.strict_budget:
                extra.setdefault("provider", {})["require_parameters"] = True
            response = await self.client.chat.completions.with_raw_response.create(
                **params, extra_body=extra)
            result = response.parse()
        except APIStatusError as ex:
            if ex.status_code == 402:  # out of credit, or the key's spending limit reached
                raise ModelLimitExceeded(f"OpenRouter refused the call: {screened_error(ex.message)}") from None
            raise
        choice = result.choices[0] if result.choices else None
        raw = choice.message if choice is not None else None
        text = (raw.content if raw is not None else None) or ""
        calls = [ToolCall(id=c.id, function=c.function.name, arguments=_arguments(c.function.arguments))
                 for c in (getattr(raw, "tool_calls", None) or []) if getattr(c, "function", None) is not None]
        finish = choice.finish_reason if choice is not None else None
        error = getattr(result, "error", None)
        choice_error = getattr(choice, "error", None)
        provider_error_type = "invalid_request" if _is_invalid_request(error) or _is_invalid_request(choice_error) else None
        if choice_error is not None:
            error = {"response": error, "choice": choice_error} if error is not None else choice_error
        failed = (error is not None or finish in {"error", "length"} or choice is None
                  or (not text and not calls) or (not text and _STOP.get(finish, "unknown") == "unknown"))
        error = screened_error(error)
        worst_case = _worst_case(params, self.prices.get(name)) if self.require_reported_cost else None
        usage = _usage(result.usage, None)
        if self.strict_budget:
            usage_is_byok = getattr(result.usage, "is_byok", None)
            if (usage_is_byok is False and usage is not None and _valid_cost(usage.cost)
                    and (usage.cost > 0 or _all_zero_tariff(self.tariffs.get(name)))):
                cost = usage.cost
                cost_method = "actual"
            elif usage_is_byok is True:
                cost = None
                cost_method = None
                if usage is not None:
                    usage.cost = None
            else:
                if usage is not None:
                    usage.cost = None
                cost, _, generation_is_byok = await self._generation_cost(
                    result.id,
                    require_non_byok=True,
                    allow_zero=_all_zero_tariff(self.tariffs.get(name)),
                )
                cost_method = "generation_lookup" if cost is not None and generation_is_byok is False else None
                if cost is not None:
                    usage = usage or Usage()
                    usage.cost = cost
        else:
            cost_method = "actual" if usage is not None and _valid_cost(usage.cost) else None
            if self.require_reported_cost and usage is not None and usage.cost == 0:
                cost_method = None
            if cost_method is None:
                if self.require_reported_cost and usage is not None:
                    usage.cost = None
                cost, found, _ = await self._generation_cost(result.id) if self.require_reported_cost else (None, False, None)
                if cost is not None:
                    cost_method = "generation_lookup"
                elif failed and self.require_reported_cost:
                    cost = worst_case
                    cost_method = "worst_case" if cost is not None else None
                else:
                    cost = (None if found else self._estimate(result.usage, name)) if self.require_reported_cost else (
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
            usage=usage, model=name, cost_method=cost_method, failed=failed, error=error,
            provider_error_type=provider_error_type, generation_id=result.id, raw_finish_reason=finish,
            http_status=response.status_code, worst_case_cost=worst_case)

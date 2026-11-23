"""Durable per-attempt dollar reservations for the target OpenRouter model."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anyio
from inspect_ai.model._providers.openrouter import SESSION_ID_HEADER, OpenRouterAPI
from inspect_ai.model._providers.util.hooks import HttpxHooks
from inspect_ai.util import LimitExceededError

from refund_eval.model_pricing import openrouter_tariffs
from refund_eval.models.shared_role_spend_ledger import (
    CONFIG_ENV as SHARED_ROLE_CONFIG_ENV,
)
from refund_eval.models.shared_role_spend_ledger import (
    LEDGER_DIRECTORY as SHARED_ROLE_LEDGER_DIRECTORY,
)
from refund_eval.models.shared_role_spend_ledger import (
    SharedRoleBudgetConfig,
    SharedRoleSpendLedger,
)
from refund_eval.models.world_spend_ledger import (
    WorldSpendBudgetExhausted,
    WorldSpendLedger,
    WorldSpendLedgerError,
)

TARIFF_KEYS = {"input", "output", "input_cache_read", "input_cache_write", "input_cache_write_1h",
               "internal_reasoning", "request"}
PROMPT_FRAMING_TOKENS = 16_384
CHAT_FIELDS = {
    "model", "messages", "frequency_penalty", "function_call", "functions", "logit_bias", "logprobs",
    "max_completion_tokens", "max_tokens", "n", "parallel_tool_calls", "presence_penalty", "response_format",
    "seed", "stop", "stream", "temperature", "tool_choice", "tools", "top_logprobs", "top_p",
    "extra_headers",
    "reasoning_effort", "extra_body",
}
SAFE_EXTRA_HEADERS = {HttpxHooks.REQUEST_ID_HEADER.lower(), SESSION_ID_HEADER.lower()}


def _money(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def charge_bound(request: Mapping[str, Any], prices: Mapping[str, float]) -> Decimal | None:
    if set(prices) != TARIFF_KEYS:
        return None
    rates = {key: _money(value) for key, value in prices.items()}
    if any(value is None for value in rates.values()):
        return None
    limits = [request.get(name) for name in ("max_tokens", "max_completion_tokens") if request.get(name) is not None]
    if not limits or any(type(value) is not int or value <= 0 for value in limits):
        return None
    max_tokens = max(limits)
    try:
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    except (TypeError, ValueError):
        return None
    prompt = len(encoded) + PROMPT_FRAMING_TOKENS
    prompt_rate = max(rates[key] for key in ("input", "input_cache_read", "input_cache_write", "input_cache_write_1h"))
    output_rate = rates["output"] + rates["internal_reasoning"]
    return (prompt * prompt_rate + max_tokens * output_rate) / 1_000_000 + rates["request"]


def _text_only(content: Any) -> bool:
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if (not isinstance(block, Mapping) or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
                or set(block) - {"type", "text", "cache_control"}):
            return False
        cache_control = block.get("cache_control")
        if cache_control is not None and (
            not isinstance(cache_control, Mapping) or cache_control.get("type") != "ephemeral"
            or set(cache_control) - {"type", "ttl"} or cache_control.get("ttl") not in (None, "5m", "1h")
        ):
            return False
    return True


def _valid_function_call(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and not (set(value) - {"name", "arguments"})
        and isinstance(value.get("name"), str)
        and bool(value["name"])
        and isinstance(value.get("arguments"), (str, Mapping))
    )


def _valid_reasoning_details(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    fields_by_type = {
        "reasoning.summary": {"type", "id", "format", "index", "summary"},
        "reasoning.text": {"type", "id", "format", "index", "text", "signature"},
    }
    for detail in value:
        if not isinstance(detail, Mapping):
            return False
        kind = detail.get("type")
        allowed_fields = fields_by_type.get(kind)
        if allowed_fields is None or set(detail) - allowed_fields:
            return False
        for field in ("id", "format"):
            if field in detail and detail[field] is not None and not isinstance(detail[field], str):
                return False
        if "index" in detail and detail["index"] is not None and type(detail["index"]) is not int:
            return False
        if kind == "reasoning.summary":
            if not isinstance(detail.get("summary"), str):
                return False
        elif any(
            field in detail and detail[field] is not None and not isinstance(detail[field], str)
            for field in ("text", "signature")
        ):
            return False
    return True


def _text_only_message(message: Mapping[str, Any]) -> bool:
    role = message.get("role")
    if not isinstance(role, str):
        return False
    allowed_fields = {
        "system": {"role", "content", "name"},
        "developer": {"role", "content", "name"},
        "user": {"role", "content", "name"},
        "assistant": {
            "role", "content", "name", "tool_calls", "function_call", "refusal",
            "reasoning", "reasoning_content", "reasoning_details",
        },
        "tool": {"role", "content", "name", "tool_call_id"},
    }.get(role)
    if allowed_fields is None or set(message) - allowed_fields:
        return False
    if "name" in message and message["name"] is not None and not isinstance(message["name"], str):
        return False
    if role == "tool" and not isinstance(message.get("tool_call_id"), str):
        return False
    tool_calls = message.get("tool_calls")
    if tool_calls is not None and (
        role != "assistant" or not isinstance(tool_calls, list)
        or any(
            not isinstance(call, Mapping)
            or set(call) - {"id", "type", "function"}
            or not isinstance(call.get("id"), str)
            or not call["id"]
            or call.get("type") != "function"
            or not _valid_function_call(call.get("function"))
            for call in tool_calls
        )
    ):
        return False
    function_call = message.get("function_call")
    if function_call is not None and (role != "assistant" or not _valid_function_call(function_call)):
        return False
    if "refusal" in message and message["refusal"] is not None and not isinstance(message["refusal"], str):
        return False
    if any(
        field in message and message[field] is not None and not isinstance(message[field], str)
        for field in ("reasoning", "reasoning_content")
    ):
        return False
    if (
        "reasoning_details" in message
        and message["reasoning_details"] is not None
        and not _valid_reasoning_details(message["reasoning_details"])
    ):
        return False
    content = message.get("content")
    if _text_only(content):
        return True
    return role == "assistant" and content is None and (bool(tool_calls) or function_call is not None)


def _bounded_request(api: OpenRouterAPI, request: dict[str, Any], cap: int) -> None:
    if api.responses_api or api.emulate_tools or api.models or api.provider or api.transforms:
        raise WorldSpendLedgerError("Target dollar budget does not support alternate OpenRouter routes")
    if set(request) - CHAT_FIELDS or ("stream" in request and request["stream"] is not False):
        raise WorldSpendLedgerError("Target dollar budget requires a single non-streaming route")
    headers = request.get("extra_headers")
    if headers is not None and (
        not isinstance(headers, Mapping)
        or any(
            not isinstance(name, str)
            or name.lower() not in SAFE_EXTRA_HEADERS
            or not isinstance(value, str)
            or not value
            or "\r" in value
            or "\n" in value
            for name, value in headers.items()
        )
    ):
        raise WorldSpendLedgerError("Target request contains unsupported HTTP headers")
    if type(request.get("n", 1)) is not int or request.get("n", 1) != 1:
        raise WorldSpendLedgerError("Target dollar budget requires one completion")
    messages = request.get("messages")
    if not isinstance(messages, list) or any(
        not isinstance(m, Mapping) or not _text_only_message(m)
        for m in messages
    ):
        raise WorldSpendLedgerError("Target dollar budget requires text-only messages")
    output_limits = [request[name] for name in ("max_tokens", "max_completion_tokens") if name in request]
    if any(type(value) is not int or value <= 0 for value in output_limits):
        raise WorldSpendLedgerError("Target request has an invalid output-token bound")
    if not output_limits:
        request["max_tokens"] = cap
    else:
        for name in ("max_tokens", "max_completion_tokens"):
            if name in request:
                request[name] = min(cap, request[name])
    extra = request.get("extra_body")
    if extra is None:
        extra = {}
    if not isinstance(extra, dict) or set(extra) - {"reasoning"}:
        raise WorldSpendLedgerError("Target dollar budget refuses unbounded OpenRouter parameters")
    request["extra_body"] = extra


async def _reported_charge(
    api: OpenRouterAPI, completion: Any, *, allow_zero: bool
) -> Decimal | None:
    usage = getattr(completion, "usage", None)
    usage_is_byok = getattr(usage, "is_byok", None)
    if usage_is_byok is True:
        return None
    cost = _money(getattr(usage, "cost", None))
    if usage_is_byok is False and cost is not None and (cost > 0 or allow_zero):
        return cost
    generation_id = getattr(completion, "id", None)
    if not isinstance(generation_id, str) or not generation_id:
        return None
    try:
        record = await api.client.with_options(max_retries=0, timeout=5).get(
            "/generation", cast_to=dict, options={"params": {"id": generation_id}})
        data = record.get("data", {})
        if not isinstance(data, Mapping) or data.get("is_byok") is not False:
            return None
        if data.get("cancelled") is not False:
            return None
        finish = data.get("finish_reason") or data.get("native_finish_reason")
        prompt = data.get("tokens_prompt") or data.get("native_tokens_prompt")
        output = data.get("tokens_completion") or data.get("native_tokens_completion")
        if not finish or not isinstance(prompt, int) or isinstance(prompt, bool) or prompt < 1:
            return None
        if not isinstance(output, int) or isinstance(output, bool) or output < 1:
            return None
        if data.get("id") != generation_id:
            return None
        value = _money(data.get("total_cost")) if isinstance(data, Mapping) else None
        return value if value is not None and (value > 0 or allow_zero) else None
    except Exception:  # noqa: BLE001 - an unavailable lookup leaves the hold unresolved
        return None


def _apply_cache_markers(api: OpenRouterAPI, request: dict[str, Any], config: Any) -> None:
    if api._cache_prompt_enabled(config):
        from inspect_ai.model._providers.openrouter import _add_anthropic_cache_markers

        _add_anthropic_cache_markers(request)


@asynccontextmanager
async def target_dollar_budget(model: Any) -> AsyncIterator[None]:
    cap_raw = os.environ.get("REFUND_EVAL_AGENT_DOLLAR_CAP")
    tokens_raw = os.environ.get("REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS")
    shared_role_mode = os.environ.get("REFUND_EVAL_SHARED_ROLE_MODE")
    if shared_role_mode not in (None, "", "disabled", "shared_roles", "shared_caps"):
        raise WorldSpendLedgerError("Target shared-role budget mode is invalid")
    if cap_raw is None and tokens_raw is None and shared_role_mode not in {"shared_roles", "shared_caps"}:
        yield
        return
    cap = _money(cap_raw)
    if cap is None or cap <= 0 or not tokens_raw or not tokens_raw.isdecimal() or int(tokens_raw) <= 0:
        raise WorldSpendLedgerError("Target dollar budget configuration is invalid")
    api = getattr(model, "api", None)
    if not isinstance(api, OpenRouterAPI) or api.base_url.rstrip("/") != "https://openrouter.ai/api/v1":
        raise WorldSpendLedgerError("Target dollar budget requires the pinned OpenRouter chat API")
    model_id = str(model)
    if not model_id.startswith("openrouter/"):
        raise WorldSpendLedgerError("Target dollar budget requires an OpenRouter model ID")
    if getattr(api, "client", None) is None or not isinstance(getattr(api, "model_args", None), dict):
        raise WorldSpendLedgerError("Target OpenRouter client cannot be safely configured")
    tariffs = await anyio.to_thread.run_sync(openrouter_tariffs, [model_id])
    prices = tariffs.get(model_id) if isinstance(tariffs, Mapping) else None
    if not isinstance(prices, Mapping) or set(prices) != TARIFF_KEYS:
        raise WorldSpendLedgerError("Complete OpenRouter tariffs are unavailable")
    allow_zero_charge = all(_money(prices[key]) == 0 for key in TARIFF_KEYS)
    scenario = os.environ.get("WORLD_SCENARIO")
    if not scenario:
        raise WorldSpendLedgerError("Target dollar budget requires the active scenario identity")
    if shared_role_mode in {"shared_roles", "shared_caps"}:
        shared_config = SharedRoleBudgetConfig.from_environment()
        if (shared_config.scenario != scenario or shared_config.models["target"] != model_id
                or shared_config.role_limits_usd["target"] != cap
                or os.environ.get(SHARED_ROLE_CONFIG_ENV) is None):
            raise WorldSpendLedgerError("Target shared-role budget scope does not match the prepared run")
        ledger = SharedRoleSpendLedger(SHARED_ROLE_LEDGER_DIRECTORY, shared_config).view("target")
    else:
        ledger = WorldSpendLedger(Path("/logs/agent/agent-spend"), cap,
                                  {"scenario": scenario, "models": {"agent": model_id}})
    original_method = api._generate_completion
    original_client = api.client
    old_max_retries = api.model_args.get("max_retries")
    api.model_args["max_retries"] = 0
    api.client = api.client.with_options(max_retries=0)

    async def reserved(request: dict[str, Any], config: Any) -> Any:
        _apply_cache_markers(api, request, config)
        _bounded_request(api, request, int(tokens_raw))
        extra = request["extra_body"]
        prompt_price = max(prices[key] for key in ("input", "input_cache_read", "input_cache_write", "input_cache_write_1h"))
        output_price = prices["output"] + prices["internal_reasoning"]
        extra["provider"] = {"max_price": {"prompt": prompt_price, "completion": output_price,
                                             "request": prices["request"]}, "require_parameters": True}
        bound = charge_bound(request, prices)
        if bound is None:
            raise WorldSpendLedgerError("Target request has no affordable defensible charge bound")
        if bound > cap:
            raise LimitExceededError("cost", value=float(bound), limit=float(cap))
        attempt_id = str(uuid.uuid4())
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        try:
            ledger.reserve(attempt_id, digest, bound)
        except WorldSpendBudgetExhausted as error:
            raise LimitExceededError("cost", value=float(cap), limit=float(cap)) from error
        except WorldSpendLedgerError:
            raise WorldSpendLedgerError("Target dollar budget cannot reserve this attempt") from None
        try:
            completion = await original_method(request, config)
        except BaseException:
            ledger.mark_unknown(attempt_id)
            raise
        charge = await _reported_charge(api, completion, allow_zero=allow_zero_charge)
        if charge is None or charge > bound:
            ledger.mark_unknown(attempt_id, charge, "reported_overrun" if charge is not None else None)
            raise WorldSpendLedgerError("Target charge is missing or exceeds its reservation; hold retained")
        ledger.settle(attempt_id, charge, "reported")
        return completion

    api._generate_completion = reserved
    try:
        yield
    finally:
        api._generate_completion = original_method
        api.client = original_client
        if old_max_retries is None:
            api.model_args.pop("max_retries", None)
        else:
            api.model_args["max_retries"] = old_max_retries

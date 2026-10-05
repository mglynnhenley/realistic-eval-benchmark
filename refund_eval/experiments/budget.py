"""Conservative all-role request budget for an isolated comparison worker.

Reserve before dispatch, including calls made by graders. This is deliberately
limited to OpenRouter text completions and offline mock models. The bound relies
on providers enforcing their advertised context/output limits and the requested
OpenRouter price caps. It is not a reconciliation of invoices, taxes or fees.
No prompts, request bodies, credentials or provider error strings enter the ledger.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
from inspect_ai.model import Model, ModelCost, ModelInfo, get_model_info, set_model_info
from inspect_ai.util import LimitExceededError

from refund_eval.model_pricing import CATALOG_URL, _catalog_cost

MAX_OUTPUT_TOKENS = 8192
SETTINGS = {"max_output_tokens": MAX_OUTPUT_TOKENS, "max_retries": 0, "sdk_max_retries": 0,
            "num_choices": 1, "best_of": 1, "cache_prompt": "preserve", "cache": False,
            "batch": False, "internal_tools": False, "provider_fallbacks": False,
            "serialized_requests": True, "reservation": "full_context_at_max_input_rate_plus_output_cap"}
_active = False


class BudgetConfigurationError(ValueError):
    """The request cannot be bounded by this restricted worker policy."""


def _decimal(value) -> Decimal:
    if isinstance(value, bool):
        raise BudgetConfigurationError("Invalid monetary value")
    try:
        number = Decimal(str(value))
    except Exception as error:
        raise BudgetConfigurationError("Invalid monetary value") from error
    if not number.is_finite() or number < 0:
        raise BudgetConfigurationError("Prices and limits must be finite and nonnegative")
    return number


def _positive_int(value, name):
    if type(value) is not int or value < 1:
        raise BudgetConfigurationError(f"A positive {name} bound is required")
    return value


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def configure_budget_models(models: Iterable[str], *, frozen: dict | None = None) -> dict:
    """Fetch one public catalog or restore frozen bounds; never generate a model call.

    Save the returned JSON once and pass it as ``frozen=`` in each worker, then
    pass the same document to ``all_role_budget(configuration=...)``.
    """
    names = list(dict.fromkeys(str(model) for model in models))
    if not names or any(not n.startswith(("openrouter/", "mockllm/")) for n in names):
        raise BudgetConfigurationError("Only explicit OpenRouter text or mockllm models are supported")
    if frozen is not None:
        configuration = copy.deepcopy(frozen)
        if configuration.get("version") != "1" or configuration.get("settings") != SETTINGS:
            raise BudgetConfigurationError("Frozen budget settings do not match this worker")
        if set(configuration.get("models", {})) != set(names):
            raise BudgetConfigurationError("Frozen model set differs from the requested models")
        expected = _fingerprint({k: v for k, v in configuration.items() if k != "sha256"})
        if configuration.get("sha256") != expected:
            raise BudgetConfigurationError("Frozen budget configuration checksum mismatch")
    else:
        catalog = {}
        if any(name.startswith("openrouter/") for name in names):
            response = httpx.get(CATALOG_URL, timeout=15.0)
            response.raise_for_status()
            catalog = {row["id"]: row for row in response.json()["data"]}
        bounds = {}
        for name in names:
            if name.startswith("mockllm/"):
                info = get_model_info(name)
                if info is None or info.cost is None or not info.context_length or not info.output_tokens:
                    raise BudgetConfigurationError("Register explicit mock model bounds and prices first")
                context, output, cost = info.context_length, info.output_tokens, info.cost
            else:
                entry = catalog.get(name.removeprefix("openrouter/"))
                if not entry:
                    raise BudgetConfigurationError(f"Model is absent from the public catalog: {name}")
                architecture = entry.get("architecture") or {}
                if architecture.get("output_modalities", ["text"]) != ["text"]:
                    raise BudgetConfigurationError("Only text-output models can use this budget policy")
                for field in ("request", "image", "internal_reasoning"):
                    if _decimal(entry["pricing"].get(field, "0")) != 0:
                        raise BudgetConfigurationError(f"Unsupported non-token pricing: {field}")
                if not {"input_cache_read", "input_cache_write"} <= entry["pricing"].keys():
                    raise BudgetConfigurationError("Explicit cache read/write prices are required to bound prompt caching")
                context = entry.get("context_length")
                output = (entry.get("top_provider") or {}).get("max_completion_tokens")
                cost = _catalog_cost(entry)
            bounds[name] = {"context_length": context, "output_tokens": output,
                            "cost_per_million": cost.model_dump(),
                            "input_rate_upper_per_million": max(cost.input, cost.input_cache_read, cost.input_cache_write)}
            if name.startswith("openrouter/"):
                # Longer cache lifetimes may cost more than the standard write
                # price. Keep that upper bound even while normal prompt caching
                # remains enabled. Optional native web search is not requested.
                bounds[name]["input_rate_upper_per_million"] = float(max(
                    _decimal(bounds[name]["input_rate_upper_per_million"]),
                    *[_decimal(price) * 1_000_000 for key, price in entry["pricing"].items()
                      if key.startswith("input_cache_")],
                ))
                bounds[name]["disabled_service_prices"] = {"web_search": entry["pricing"].get("web_search", "0")}
        configuration = {"version": "1", "captured_at": datetime.now(UTC).isoformat(),
                         "source": CATALOG_URL, "settings": copy.deepcopy(SETTINGS), "models": bounds}
        configuration["sha256"] = _fingerprint(configuration)
    for name, bound in configuration["models"].items():
        _positive_int(bound["context_length"], "context_length")
        _positive_int(bound["output_tokens"], "output_tokens")
        costs = bound["cost_per_million"]
        for field in ModelCost.model_fields:
            _decimal(costs[field])
        upper = _decimal(bound["input_rate_upper_per_million"])
        if upper < max(_decimal(costs[field]) for field in ("input", "input_cache_read", "input_cache_write")):
            raise BudgetConfigurationError("Input-rate bound is below the registered price")
        set_model_info(name, ModelInfo(model=name, context_length=bound["context_length"],
                                      output_tokens=bound["output_tokens"], cost=ModelCost(**costs)))
    return configuration


class _Budget:
    def __init__(self, limit, configuration, ledger_path):
        self.limit = _decimal(limit)
        if self.limit <= 0:
            raise BudgetConfigurationError("The budget must be positive")
        self.configuration = configuration
        self.path = Path(ledger_path)
        self.charged = Decimal(0)
        self.reported = Decimal(0)
        self.events = []
        self.blocked = False
        self.lock = asyncio.Lock()
        self._write()

    def _write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {"version": "1", "limit_usd": float(self.limit), "charged_upper_usd": float(self.charged),
                    "reported_usage_usd": float(self.reported), "remaining_upper_usd": float(max(Decimal(0), self.limit - self.charged)),
                    "blocked": self.blocked, "configuration": self.configuration, "events": self.events}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, prefix=".budget-", delete=False) as handle:
                temporary = handle.name
                json.dump(document, handle, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()

    def deny(self, model, reason, reserve=Decimal(0)):
        self.blocked = True
        self.events.append({"event": "denied", "model": model, "reason": reason,
                            "reservation_usd": float(reserve)})
        self._write()
        raise LimitExceededError("cost", limit=float(self.limit), value=float(self.charged),
                                 message="All-role budget cannot safely reserve another request.")

    def settle(self, event, output, bound, reservation):
        usage = getattr(output, "usage", None)
        if usage is None:
            event["event"] = "unknown_usage_reserved"
            self._write()
            return
        counts = [usage.input_tokens, usage.input_tokens_cache_read or 0, usage.input_tokens_cache_write or 0,
                  usage.output_tokens, usage.reasoning_tokens or 0]
        if any(type(value) is not int or value < 0 for value in counts):
            event["event"] = "invalid_usage_reserved"
            self._write()
            return
        rates = bound["cost_per_million"]
        # Sum cache categories; output_tokens includes reasoning per Inspect's contract.
        priced = (Decimal(counts[0]) * _decimal(rates["input"])
                  + Decimal(counts[1]) * _decimal(rates["input_cache_read"])
                  + Decimal(counts[2]) * _decimal(rates["input_cache_write"])
                  + Decimal(max(counts[3], counts[4])) * _decimal(rates["output"])) / Decimal(1_000_000)
        reported = _decimal(usage.total_cost) if usage.total_cost is not None else priced
        cache_upper = priced + Decimal(counts[2]) * (
            _decimal(bound["input_rate_upper_per_million"]) - _decimal(rates["input_cache_write"])
        ) / Decimal(1_000_000)
        conservative = max(cache_upper, reported)
        self.reported += reported
        self.charged += conservative - reservation
        event.update(event="settled", reported_usage_usd=float(reported),
                     cost_basis="reported_total" if usage.total_cost is not None else "catalog_usage_estimate")
        event["charged_usd"] = float(conservative)
        if conservative > reservation:
            self.blocked = True
            event["event"] = "provider_bound_exceeded"
        self._write()


@contextmanager
def all_role_budget(limit_usd: float, models: Iterable[str], ledger_path: str | Path, *,
                    configuration: dict | None = None):
    """Temporarily guard every Model.generate in this isolated process.

    Unknown usage/errors keep their entire reservation. Call this around the
    complete ``inspect_ai.eval`` invocation, including grading. Existing ledgers
    are rejected: resume requires an explicit, separately reviewed policy.
    """
    global _active
    if _active:
        raise BudgetConfigurationError("Budget contexts cannot overlap in one process")
    names = list(dict.fromkeys(str(model) for model in models))
    if configuration is None:
        # Offline callers can register mock bounds. Paid workers must explicitly
        # pass frozen catalog metadata; do not silently refresh prices here.
        if any(not name.startswith("mockllm/") for name in names):
            raise BudgetConfigurationError("Paid workers require frozen budget configuration")
        configuration = configure_budget_models(names)
    else:
        configuration = configure_budget_models(names, frozen=configuration)
    if Path(ledger_path).exists():
        raise BudgetConfigurationError("Budget ledger already exists; refusing to reset spent funds")
    budget = _Budget(limit_usd, configuration, ledger_path)
    original, original_loop = Model.generate, Model.generate_loop
    signature = inspect.signature(original)

    async def guarded(model, *args, **kwargs):
        async with budget.lock:
            name = str(model)
            if budget.blocked or name not in configuration["models"]:
                budget.deny(name, "budget_closed_or_unknown_model")
            bound = configuration["models"][name]
            call = signature.bind(model, *args, **kwargs)
            effective = model._resolve_config(call.arguments.get("config"))
            if effective.extra_body or effective.extra_headers or effective.modalities or effective.batch or effective.fallback_models:
                budget.deny(name, "unsupported_request_options")
            if effective.num_choices not in (None, 1) or effective.best_of not in (None, 1):
                budget.deny(name, "multiple_completions_are_not_supported")
            api = model.api
            if name.startswith("openrouter/") and (type(api).__name__ != "OpenRouterAPI" or api.models or api.transforms or api.responses_api):
                budget.deny(name, "unsupported_provider_configuration")
            cap = min(MAX_OUTPUT_TOKENS, bound["output_tokens"], effective.max_tokens or MAX_OUTPUT_TOKENS)
            if type(cap) is not int or cap < 1:
                budget.deny(name, "invalid_output_limit")
            rates = bound["cost_per_million"]
            reservation = (Decimal(bound["context_length"]) * _decimal(bound["input_rate_upper_per_million"])
                           + Decimal(cap) * _decimal(rates["output"])) / Decimal(1_000_000)
            if budget.charged + reservation > budget.limit:
                budget.deny(name, "insufficient_reservation", reservation)
            controls = {"max_tokens": cap, "max_retries": 0, "max_connections": 1, "adaptive_connections": False,
                        "cache": False,
                        "batch": False, "internal_tools": False, "fallback_models": []}
            call.arguments["config"] = effective.model_copy(update=controls)
            call.arguments["cache"] = False
            saved = None
            if name.startswith("openrouter/"):
                saved = (api.client.max_retries, dict(api.model_args), copy.deepcopy(api.provider))
                api.client.max_retries = 0
                api.model_args["max_retries"] = 0
                api.provider = {"allow_fallbacks": False, "require_parameters": True,
                                "max_price": {"prompt": rates["input"], "completion": rates["output"], "request": 0, "image": 0}}
            event = {"event": "reserved", "model": name, "role": getattr(model, "role", None),
                     "reservation_usd": float(reservation), "max_tokens": cap}
            budget.events.append(event)
            budget.charged += reservation
            try:
                budget._write()  # durable reservation before any provider dispatch
                output = await original(*call.args, **call.kwargs)
                budget.settle(event, output, bound, reservation)
                return output
            except BaseException as error:
                if event["event"] == "reserved":
                    event.update(event="failed_reserved", error_type=type(error).__name__)
                    budget._write()
                raise
            finally:
                if saved is not None:
                    api.client.max_retries, api.model_args, api.provider = saved

    async def forbidden_loop(model, *args, **kwargs):
        budget.deny(str(model), "generate_loop_is_not_supported")

    _active = True
    Model.generate, Model.generate_loop = guarded, forbidden_loop
    try:
        yield budget
    finally:
        Model.generate, Model.generate_loop = original, original_loop
        _active = False
        budget._write()

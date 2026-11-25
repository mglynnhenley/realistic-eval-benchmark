"""Per-trial allowance for provider-reported world model spend."""

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from typing import Any

import anyio

from refund_eval.models.client import (
    Completion,
    Message,
    ModelClient,
    ModelLimitExceeded,
    ModelRequestRejected,
    Role,
    Usage,
)
from refund_eval.models.evidence import screened_error
from refund_eval.models.openrouter import response_json_schema, to_openai
from refund_eval.models.world_spend_ledger import (
    WorldSpendLedger,
    WorldSpendLedgerError,
)
from refund_eval.world.store import world_store

WORLD_BUDGET_KEY = "world_model_budget"
WORLD_MAX_OUTPUT_TOKENS = 9000
PROMPT_FRAMING_TOKENS = 16_384
TARIFF_KEYS = {"input", "output", "input_cache_read", "input_cache_write", "input_cache_write_1h",
               "internal_reasoning", "request"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def charge_bound(messages: Sequence[Message], settings: Mapping[str, Any],
                 prices: Mapping[str, float] | None) -> Decimal | None:
    """Worst-case USD for a request from a complete catalog tariff, or None if unbounded."""
    if not prices or set(prices) != TARIFF_KEYS:
        return None
    try:
        rates = {key: Decimal(str(value)) for key, value in prices.items()}
    except (InvalidOperation, ValueError, TypeError):
        return None
    if any(not rate.is_finite() or rate < 0 for rate in rates.values()):
        return None
    max_tokens = settings.get("max_tokens")
    if type(max_tokens) is not int or max_tokens <= 0:
        return None
    schema = settings.get("schema")
    payload = json.dumps({
        "messages": [to_openai(message) for message in messages],
        "tools": [{"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                  for tool in settings.get("tools") or ()],
        "schema": None if schema is None else {
            "name": schema.name,
            "description": schema.description,
            "schema": response_json_schema(schema.type),
        },
        "stop": list(settings.get("stop") or ()),
    }, default=str)
    prompt_tokens = len(payload.encode()) + PROMPT_FRAMING_TOKENS
    prompt_rate = max(rates[key] for key in ("input", "input_cache_read", "input_cache_write", "input_cache_write_1h"))
    output_rate = rates["output"] + rates["internal_reasoning"]
    return (prompt_tokens * prompt_rate + max_tokens * output_rate) / 1_000_000 + rates["request"]


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and result >= 0 else None


class BudgetedWorldModels:
    def __init__(self, recorded: ModelClient, limit: float, persist: Callable[[], None], *,
                 prices: Mapping[str, Mapping[str, float]] | None = None,
                 ledger: WorldSpendLedger | None = None,
                 scope: Mapping[str, Any] | None = None, strict_budget: bool = False,
                 usage_reporting_only: bool = False,
                 disable_application_price_cutoffs: bool = False) -> None:
        self.recorded = recorded
        self.limit = Decimal(str(limit))
        self.prices = None if prices is None else {name: dict(tariff) for name, tariff in prices.items()}
        if ledger is not None and self.prices is None:
            raise ValueError("A world-spend ledger requires complete reservation tariffs")
        self.usage_reporting_only = usage_reporting_only
        if disable_application_price_cutoffs and not usage_reporting_only:
            raise ValueError("Disabling application price cutoffs requires usage-only reporting")
        self.disable_application_price_cutoffs = disable_application_price_cutoffs
        self.ledger = ((ledger or WorldSpendLedger.in_memory(self.limit, scope))
                       if self.prices is not None and not usage_reporting_only else None)
        self.spent = Decimal(0)
        self.reserved = Decimal(0)
        self.persist = persist
        self.strict_budget = strict_budget
        self.lock = anyio.Lock()
        self._request_rejected = False
        if self.ledger is None:
            self._mark("available")
        else:
            self._sync_ledger()
            self.persist()

    def model_name(self, role: Role) -> str:
        return self.recorded.model_name(role)

    def _mark(self, status: str) -> None:
        self.status = status
        snapshot = {
            "limit_usd": None if self.disable_application_price_cutoffs else float(self.limit),
            "spent_usd": float(self.spent),
            "status": status,
        }
        if self.disable_application_price_cutoffs:
            snapshot["application_price_cutoffs_enabled"] = False
        if self.ledger is not None:
            snapshot.update(reserved_usd=float(self.reserved), committed_usd=float(self.spent + self.reserved))
        world_store().set(WORLD_BUDGET_KEY, snapshot)

    def _sync_ledger(self, status: str | None = None, snapshot: Mapping[str, Any] | None = None) -> None:
        if self.ledger is None:
            return
        snapshot = snapshot or self.ledger.snapshot()
        self.spent = Decimal(snapshot["spent_usd"])
        self.reserved = Decimal(snapshot["reserved_usd"])
        if status is None:
            status = ("unknown" if snapshot["unresolved_attempts"] else
                      "exhausted" if self.spent >= self.limit else "available")
        self.status = status
        world_store().set(WORLD_BUDGET_KEY, {"limit_usd": float(self.limit), "spent_usd": float(self.spent),
                                            "reserved_usd": float(self.reserved),
                                            "committed_usd": float(self.spent + self.reserved), "status": status})

    def _request_hash(self, role: Role, messages: Sequence[Message], settings: Mapping[str, Any]) -> str:
        payload = {"role": role, "model": self.model_name(role), "messages": [asdict(message) for message in messages],
                   "settings": settings}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _worst_case(self, role: Role, messages: Sequence[Message], settings: Mapping[str, Any]) -> Decimal | None:
        estimate = getattr(self.recorded, "worst_case_cost", None)
        if estimate is None:
            return None
        try:
            return _decimal(estimate(role, messages, **settings))
        except Exception:  # noqa: BLE001 - an uncomputable fallback keeps the reservation held
            return None

    def _reservation_status(self, error: WorldSpendLedgerError) -> str:
        return "unknown" if "unresolved hold" in str(error) else "exhausted"

    def _reject_request(self) -> None:
        self._request_rejected = True
        if self.status in {"available", "unsettled"}:
            self._mark("failed")
        self.persist()
        raise ModelRequestRejected() from None

    async def _complete_reserved(self, role: Role, messages: Sequence[Message], settings: dict[str, Any]) -> Completion:
        if self.status == "failed":
            raise ModelLimitExceeded("World model allowance is failed; no further requests")
        if settings.get("max_tokens") is None:
            settings = {**settings, "max_tokens": WORLD_MAX_OUTPUT_TOKENS, "default_output_cap": True}
        assert self.ledger is not None and self.prices is not None
        for attempt in range(3):
            current = self.ledger.snapshot()
            self._sync_ledger(snapshot=current)
            if current["unresolved_attempts"]:
                raise ModelLimitExceeded("World model allowance is unknown; an earlier reservation remains held")
            if self.spent >= self.limit:
                self._mark("exhausted")
                raise ModelLimitExceeded(f"World model allowance reached: USD {self.spent} / {self.limit}")
            bound = charge_bound(messages, settings, self.prices.get(self.model_name(role)))
            if bound is None:
                self._mark("unbounded")
                self.persist()
                raise ModelLimitExceeded("World model request has no defensible charge bound; not sent")
            attempt_id = str(uuid.uuid4())
            try:
                snapshot = self.ledger.reserve(attempt_id, self._request_hash(role, messages, settings), bound)
            except WorldSpendLedgerError as error:
                snapshot = self.ledger.snapshot()
                self._sync_ledger(self._reservation_status(error), snapshot)
                self.persist()
                raise ModelLimitExceeded(str(error)) from None
            self._sync_ledger("unsettled", snapshot)
            self.persist()
            request_exception = False
            try:
                completion = await self.recorded.complete(role, messages, **settings)
            except ModelRequestRejected:
                snapshot = self.ledger.mark_unknown(attempt_id)
                self._sync_ledger("unknown", snapshot)
                self._request_rejected = True
                self.persist()
                raise
            except ModelLimitExceeded:
                self.ledger.mark_unknown(attempt_id)
                self._sync_ledger("unknown")
                self.persist()
                raise
            except BaseException as error:
                if not isinstance(error, Exception):
                    try:
                        snapshot = self.ledger.mark_unknown(attempt_id)
                        self._sync_ledger("unknown", snapshot)
                        self.persist()
                    except BaseException as persist_error:  # noqa: BLE001 - preserve and re-raise the original interruption
                        if hasattr(error, "add_note"):
                            error.add_note(f"Could not persist interruption state: {type(persist_error).__name__}")
                    raise
                if self.strict_budget:
                    snapshot = self.ledger.mark_unknown(attempt_id)
                    self._sync_ledger("unknown", snapshot)
                    self.persist()
                    raise ModelLimitExceeded("Strict world request failed with unknown charge; reservation retained") from None
                request_exception = True
                worst_case = self._worst_case(role, messages, settings)
                completion = Completion(
                    text="", message=Message("assistant", ""),
                    usage=Usage(cost=None if worst_case is None else float(worst_case)), failed=True,
                    cost_method="worst_case" if worst_case is not None else None,
                    worst_case_cost=None if worst_case is None else float(worst_case),
                    error={"type": type(error).__name__, "message": screened_error(str(error))},
                )

            reported = _decimal(completion.usage.cost if completion.usage else None)
            worst_case = _decimal(completion.worst_case_cost)
            if self.strict_budget and (reported is None or completion.cost_method not in {"actual", "generation_lookup"}):
                snapshot = self.ledger.mark_unknown(attempt_id)
                self._sync_ledger("unknown", snapshot)
                self.persist()
                raise ModelLimitExceeded("Strict world request lacks an actual provider charge; reservation retained")
            if self.strict_budget and reported == 0 and bound > 0:
                snapshot = self.ledger.mark_unknown(attempt_id)
                self._sync_ledger("unknown", snapshot)
                self.persist()
                raise ModelLimitExceeded(
                    "Strict world zero charge is ambiguous with a nonzero tariff; reservation retained"
                )
            if completion.failed:
                if reported is None and worst_case is None and not request_exception:
                    snapshot = self.ledger.mark_unknown(attempt_id)
                    self._sync_ledger("unknown", snapshot)
                    self.persist()
                    if completion.provider_error_type == "invalid_request":
                        self._reject_request()
                    raise ModelLimitExceeded("Failed world model request has no defensible charge")
                if reported is None and worst_case is None:
                    charge = bound
                else:
                    charge = max(value for value in (bound, reported, worst_case) if value is not None)
                if self.strict_budget:
                    provenance = "strict_failed_attempt_bound"
                else:
                    provenance = "worst_case" if completion.cost_method == "worst_case" or worst_case is not None else (
                        completion.cost_method or "bounded_failed_request")
            elif reported is None:
                snapshot = self.ledger.mark_unknown(attempt_id)
                self._sync_ledger("unknown", snapshot)
                self.persist()
                raise ModelLimitExceeded("World model request did not report a valid charge")
            elif completion.cost_method == "estimate":
                charge = max(reported, bound)
                provenance = "estimate"
            else:
                charge = reported
                provenance = completion.cost_method or "reported"

            if not completion.failed and charge > bound and completion.cost_method != "estimate":
                snapshot = self.ledger.mark_unknown(attempt_id, charge, "reservation_overrun")
                self._sync_ledger("unknown", snapshot)
                self.persist()
                raise ModelLimitExceeded(f"World model charge USD {charge} exceeded its reservation {bound}")

            try:
                snapshot = self.ledger.settle(attempt_id, charge, provenance)
            except WorldSpendLedgerError:
                self._sync_ledger("unknown")
                self.persist()
                if completion.provider_error_type == "invalid_request":
                    self._request_rejected = True
                    raise ModelRequestRejected() from None
                raise
            self._sync_ledger(snapshot=snapshot)
            self.persist()
            if completion.provider_error_type == "invalid_request":
                self._reject_request()
            if not completion.failed:
                return completion
            if self.strict_budget:
                raise ModelLimitExceeded("Strict world model request failed; automatic retries are disabled")
            if attempt == 2:
                self._mark("failed")
                self.persist()
                raise ModelLimitExceeded("World model request failed after three attempts")
            if self.spent >= self.limit:
                self._mark("exhausted")
                self.persist()
                raise ModelLimitExceeded("World model allowance cannot cover another attempt")
        raise AssertionError("unreachable retry loop")

    async def _complete_legacy(self, role: Role, messages: Sequence[Message], settings: dict[str, Any]) -> Completion:
        if self.status != "available" and not (
            self.disable_application_price_cutoffs and self.status == "unknown"
        ):
            raise ModelLimitExceeded(f"World model allowance is {self.status}; no further requests")
        if settings.get("max_tokens") is None:
            settings = {**settings, "max_tokens": WORLD_MAX_OUTPUT_TOKENS, "default_output_cap": True}
        for attempt in range(3):
            self._mark("unsettled")
            self.persist()
            try:
                completion = await self.recorded.complete(role, messages, **settings)
            except ModelRequestRejected:
                self._mark("unknown")
                self._request_rejected = True
                self.persist()
                raise
            except ModelLimitExceeded:
                self._mark("failed" if self.disable_application_price_cutoffs else "unknown")
                self.persist()
                raise
            except Exception as error:  # noqa: BLE001 - retain MAT-144 failure accounting
                worst_case = self._worst_case(role, messages, settings)
                completion = Completion(
                    text="", message=Message("assistant", ""),
                    usage=Usage(cost=None if worst_case is None else float(worst_case)), failed=True,
                    cost_method="worst_case" if worst_case is not None else None,
                    worst_case_cost=None if worst_case is None else float(worst_case),
                    error={"type": type(error).__name__, "message": screened_error(str(error))},
                )
            except BaseException as error:
                # Cancelled mid-call: the request may have been sent and charged.
                try:
                    self._mark("unknown")
                    self.persist()
                except BaseException as persist_error:  # noqa: BLE001 - preserve and re-raise the original interruption
                    if hasattr(error, "add_note"):
                        error.add_note(f"Could not persist interruption state: {type(persist_error).__name__}")
                raise
            cost = _decimal(completion.usage.cost if completion.usage else None)
            if cost is None:
                self._mark("unknown")
                if completion.provider_error_type == "invalid_request":
                    self._reject_request()
                if self.usage_reporting_only:
                    if completion.failed and self.disable_application_price_cutoffs:
                        self._mark("failed")
                        self.persist()
                        raise ModelLimitExceeded(
                            "Usage-only world request failed; automatic retries are disabled"
                        )
                    self.persist()
                    return completion
                raise ModelLimitExceeded("World model request did not report a valid charge")
            self.spent += cost
            if self.spent >= self.limit and not self.disable_application_price_cutoffs:
                self._mark("exhausted")
                if completion.provider_error_type == "invalid_request":
                    self._reject_request()
                raise ModelLimitExceeded(f"World model allowance reached: USD {self.spent} / {self.limit}")
            if not completion.failed:
                self._mark("available")
                return completion
            if self.usage_reporting_only:
                self._mark("failed")
                self.persist()
                raise ModelLimitExceeded("Usage-only world request failed; automatic retries are disabled")
            if completion.provider_error_type == "invalid_request":
                self._reject_request()
            if attempt == 2:
                self._mark("failed")
                raise ModelLimitExceeded("World model request failed after three attempts")
            worst_case = _decimal(completion.worst_case_cost)
            if worst_case is None:
                self._mark("unknown")
                raise ModelLimitExceeded("World model retry has no valid worst-case charge")
            if worst_case > self.limit - self.spent:
                self._mark("exhausted")
                raise ModelLimitExceeded("World model allowance cannot cover a worst-case retry")
        raise AssertionError("unreachable retry loop")

    async def complete(self, role: Role, messages: Sequence[Message], **settings: Any) -> Completion:
        async with self.lock:
            if self._request_rejected:
                raise ModelRequestRejected()
            if self.ledger is None:
                return await self._complete_legacy(role, messages, dict(settings))
            return await self._complete_reserved(role, messages, dict(settings))

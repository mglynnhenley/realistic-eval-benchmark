from __future__ import annotations

import asyncio
import json
import math
import os
import time
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

KEY_CEILING = Decimal(50)
_MONEY_EPSILON = Decimal("0.01")
Probe = Callable[[], Awaitable[object]]


class GuardFailure(RuntimeError):
    def __init__(self, reason: str, *, cleanup_uncertain: bool = False) -> None:
        self.reason = reason
        self.cleanup_uncertain = cleanup_uncertain
        super().__init__(reason)


def _money(value: object) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError from None
    if not amount.is_finite() or amount < 0:
        raise ValueError
    return amount


@dataclass(frozen=True)
class Thresholds:
    openrouter_key_ceiling: Decimal
    openrouter_trial_delta: Decimal
    probe_timeout: float = 10.0
    poll_interval: float = 15.0
    max_sample_age: float = 35.0
    cleanup_timeout: float = 10.0

    def __post_init__(self) -> None:
        try:
            key_ceiling = _money(self.openrouter_key_ceiling)
            delta = _money(self.openrouter_trial_delta)
        except ValueError:
            raise ValueError("OpenRouter thresholds must be finite and nonnegative") from None
        if key_ceiling != KEY_CEILING:
            raise ValueError("OpenRouter key ceiling must be exactly $50")
        if delta <= 0 or delta > key_ceiling:
            raise ValueError("OpenRouter trial threshold must be positive and bounded")
        for value in (
            self.probe_timeout,
            self.poll_interval,
            self.max_sample_age,
            self.cleanup_timeout,
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("guard timeouts and intervals must be finite and positive")
        if self.max_sample_age <= self.poll_interval + self.probe_timeout:
            raise ValueError("sample freshness must exceed poll interval plus probe timeout")
        object.__setattr__(self, "openrouter_key_ceiling", key_ceiling)
        object.__setattr__(self, "openrouter_trial_delta", delta)


@dataclass(frozen=True)
class SplitThresholds:
    agent_trial_delta: Decimal
    world_trial_delta: Decimal
    probe_timeout: float = 10.0
    poll_interval: float = 15.0
    max_sample_age: float = 35.0
    cleanup_timeout: float = 10.0

    def __post_init__(self) -> None:
        try:
            agent, world = _money(self.agent_trial_delta), _money(self.world_trial_delta)
        except ValueError:
            raise ValueError("Split OpenRouter thresholds must be finite and nonnegative") from None
        if agent <= 0 or agent > KEY_CEILING or world <= 0 or world > Decimal(100):
            raise ValueError("Split OpenRouter thresholds exceed their role ceilings")
        for value in (self.probe_timeout, self.poll_interval, self.max_sample_age, self.cleanup_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("guard timeouts and intervals must be finite and positive")
        if self.max_sample_age <= self.poll_interval + self.probe_timeout:
            raise ValueError("sample freshness must exceed poll interval plus probe timeout")
        object.__setattr__(self, "agent_trial_delta", agent)
        object.__setattr__(self, "world_trial_delta", world)


@dataclass(frozen=True)
class SharedRoleThresholds:
    shared_allowance_usd: Decimal
    probe_timeout: float = 10.0
    poll_interval: float = 15.0
    max_sample_age: float = 35.0
    cleanup_timeout: float = 10.0

    def __post_init__(self) -> None:
        try:
            allowance = _money(self.shared_allowance_usd)
        except ValueError:
            raise ValueError("Shared-role allowance must be finite and nonnegative") from None
        if allowance <= 0 or allowance > Decimal(150):
            raise ValueError("Shared-role allowance must be positive and no greater than $150")
        for value in (self.probe_timeout, self.poll_interval, self.max_sample_age, self.cleanup_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError("guard timeouts and intervals must be finite and positive")
        if self.max_sample_age <= self.poll_interval + self.probe_timeout:
            raise ValueError("sample freshness must exceed poll interval plus probe timeout")
        object.__setattr__(self, "shared_allowance_usd", allowance)


_OPENROUTER_BUDGET_SCOPES = frozenset({
    "workspace_default_member", "workspace_default_key", "member", "api_key", "workspace", "key_limit",
})


@dataclass(frozen=True)
class OpenRouterBudget:
    scope: str
    guardrail_id: str | None
    budget_id: str | None
    limit: Decimal
    spend: Decimal
    remaining: Decimal
    reset_interval: str | None
    resets_at: str | None
    include_byok: bool

    @classmethod
    def parse(cls, value: object) -> OpenRouterBudget:
        if not isinstance(value, Mapping):
            raise TypeError
        scope = value.get("scope")
        guardrail_id, budget_id = value.get("guardrail_id"), value.get("budget_id")
        interval, resets_at = value.get("reset_interval"), value.get("resets_at")
        include_byok = value.get("include_byok")
        if (not isinstance(scope, str) or scope not in _OPENROUTER_BUDGET_SCOPES
                or "reset_interval" not in value or "resets_at" not in value
                or (guardrail_id is not None and not isinstance(guardrail_id, str))
                or (budget_id is not None and not isinstance(budget_id, str))
                or interval not in (None, "daily", "weekly", "monthly")
                or (resets_at is not None and not isinstance(resets_at, str)) or type(include_byok) is not bool):
            raise TypeError
        limit, spend, remaining = (_money(value.get("limit_usd")), _money(value.get("spend_usd")),
                                   _money(value.get("remaining_usd")))
        if limit is None or spend is None or remaining is None:
            raise TypeError
        return cls(scope, guardrail_id, budget_id, limit, spend, remaining, interval, resets_at, include_byok)

    @property
    def identity(self) -> tuple[object, ...]:
        return (self.scope, self.guardrail_id, self.budget_id, self.limit, self.reset_interval,
                self.resets_at, self.include_byok)


@dataclass(frozen=True)
class OpenRouterSnapshot:
    usage: Decimal
    limit: Decimal
    remaining: Decimal

    @classmethod
    def parse(cls, payload: object) -> OpenRouterSnapshot:
        if not isinstance(payload, Mapping):
            raise TypeError
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise TypeError
        return cls(
            usage=_money(data.get("usage")),
            limit=_money(data.get("limit")),
            remaining=_money(data.get("limit_remaining")),
        )


@dataclass(frozen=True)
class SharedRoleOpenRouterSnapshot:
    usage: Decimal
    limit: Decimal
    remaining: Decimal
    limit_reset: str | None
    limit_reset_present: bool
    is_management_key: bool | None
    is_provisioning_key: bool | None
    include_byok_in_limit: bool | None
    budgets: tuple[OpenRouterBudget, ...]
    budgets_present: bool
    effective_budget: OpenRouterBudget | None
    effective_budget_present: bool

    @classmethod
    def parse(cls, payload: object) -> SharedRoleOpenRouterSnapshot:
        base = OpenRouterSnapshot.parse(payload)
        data = payload["data"]
        reset = data.get("limit_reset")
        management, provisioning = data.get("is_management_key"), data.get("is_provisioning_key")
        include_byok = data.get("include_byok_in_limit")
        budgets_present = "budgets" in data
        budgets_value = data.get("budgets", [])
        effective_present = "effective_budget" in data
        effective_value = data.get("effective_budget")
        if (not isinstance(data, Mapping)
                or (reset is not None and not isinstance(reset, str))
                or (management is not None and type(management) is not bool)
                or (provisioning is not None and type(provisioning) is not bool)
                or (include_byok is not None and type(include_byok) is not bool)
                or "limit_reset" not in data or not isinstance(budgets_value, list)
                or (effective_present and effective_value is not None and not isinstance(effective_value, Mapping))):
            raise TypeError
        return cls(
            base.usage, base.limit, base.remaining, reset, True, management, provisioning, include_byok,
            tuple(OpenRouterBudget.parse(value) for value in budgets_value), budgets_present,
            None if effective_value is None else OpenRouterBudget.parse(effective_value), effective_present,
        )


@dataclass(frozen=True)
class _EnvironmentIdentity:
    name: str
    session_id: str


@dataclass
class SandboxBinding:
    owner: object
    environment: object
    identity: _EnvironmentIdentity
    sandbox: object
    object_id: str
    cleanup_state: str = "pending"


def _environment_identity(environment: object) -> _EnvironmentIdentity:
    name = getattr(environment, "environment_name", None)
    session_id = getattr(environment, "session_id", None)
    if not isinstance(name, str) or not name or not isinstance(session_id, str) or not session_id:
        raise ValueError
    return _EnvironmentIdentity(name, session_id)


def _sandbox_object_id(sandbox: object) -> str:
    object_id = getattr(sandbox, "object_id", None)
    if not isinstance(object_id, str) or not object_id:
        raise ValueError
    return object_id


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _read_openrouter_sync(timeout: float) -> object:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise GuardFailure("OpenRouter probe failed")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key",
        headers={"Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception:  # noqa: BLE001
        raise GuardFailure("OpenRouter probe failed") from None


async def openrouter_key_probe(timeout: float) -> object:
    return await asyncio.to_thread(_read_openrouter_sync, timeout)


def _read_split_key_sync(env_name: str, timeout: float) -> object:
    key = os.environ.get(env_name)
    if not key:
        raise GuardFailure("OpenRouter probe failed")
    request = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except Exception:  # noqa: BLE001
        raise GuardFailure("OpenRouter probe failed") from None


async def split_openrouter_key_probe(env_name: str, timeout: float) -> object:
    return await asyncio.to_thread(_read_split_key_sync, env_name, timeout)


class SpendGuard:
    def __init__(
        self,
        thresholds: Thresholds | SharedRoleThresholds,
        *,
        openrouter_probe: Probe | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.thresholds = thresholds
        self.openrouter_probe = openrouter_probe or (
            lambda: openrouter_key_probe(thresholds.probe_timeout)
        )
        self._clock = clock
        self._owner = object()
        self._baseline: OpenRouterSnapshot | None = None
        self._previous_openrouter: OpenRouterSnapshot | None = None
        self._last_sample_at: float | None = None
        self._start_attempted = False
        self._started = False
        self._closing = False
        self._ready = asyncio.Event()
        self._cleanup_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task[None] | None = None
        self._job_task: asyncio.Task[Any] | None = None
        self._bindings: list[SandboxBinding] = []
        self.failure_reason: str | None = None
        self.cleanup_uncertain = False

    @property
    def monitor_task(self) -> asyncio.Task[None] | None:
        return self._monitor_task

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self.failure_reason is None

    def attach_job_task(self, task: asyncio.Task[Any] | None) -> None:
        self._job_task = task

    def _latch(self, reason: str, *, cleanup_uncertain: bool = False) -> None:
        if self.failure_reason is None:
            self.failure_reason = reason
        self.cleanup_uncertain = self.cleanup_uncertain or cleanup_uncertain
        self._ready.set()

    def _failure(self) -> GuardFailure:
        return GuardFailure(
            self.failure_reason or "guard is not ready",
            cleanup_uncertain=self.cleanup_uncertain,
        )

    async def _probe(self, name: str, probe: Probe) -> object:
        task = asyncio.create_task(probe())
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=self.thresholds.probe_timeout
            )
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(_consume_task_exception)
            raise
        if not done:
            task.cancel()
            task.add_done_callback(_consume_task_exception)
            raise GuardFailure(f"{name} probe failed") from None
        try:
            return task.result()
        except Exception:  # noqa: BLE001
            raise GuardFailure(f"{name} probe failed") from None

    async def _read_openrouter(self) -> OpenRouterSnapshot:
        value = await self._probe("OpenRouter", self.openrouter_probe)
        try:
            return OpenRouterSnapshot.parse(value)
        except (ValueError, TypeError, OverflowError):
            raise GuardFailure("monitor data malformed") from None

    def _accept_sample(
        self, openrouter: OpenRouterSnapshot, *, initial: bool = False
    ) -> None:
        if openrouter.limit != self.thresholds.openrouter_key_ceiling:
            raise GuardFailure("OpenRouter key ceiling does not match $50")
        if openrouter.remaining > openrouter.limit:
            raise GuardFailure("OpenRouter counters malformed")
        if openrouter.usage > openrouter.limit:
            raise GuardFailure("OpenRouter counters malformed")
        if openrouter.usage + openrouter.remaining > openrouter.limit + _MONEY_EPSILON:
            raise GuardFailure("OpenRouter counters malformed")
        if initial:
            if openrouter.remaining < self.thresholds.openrouter_trial_delta:
                raise GuardFailure("OpenRouter remaining limit is below trial threshold")
            self._baseline = openrouter
        else:
            previous_openrouter = self._previous_openrouter
            if previous_openrouter is None or self._baseline is None:
                raise GuardFailure("monitor state invalid")
            if openrouter.usage < previous_openrouter.usage:
                raise GuardFailure("OpenRouter usage counter decreased")
            if openrouter.remaining > previous_openrouter.remaining + _MONEY_EPSILON:
                raise GuardFailure("OpenRouter remaining counter increased")
            if openrouter.limit != previous_openrouter.limit:
                raise GuardFailure("OpenRouter key ceiling changed")
            if openrouter.usage - self._baseline.usage >= self.thresholds.openrouter_trial_delta:
                raise GuardFailure("OpenRouter trial threshold reached")

        self._previous_openrouter = openrouter
        self._last_sample_at = self._clock()

    async def start(self) -> None:
        if self._start_attempted:
            self._latch("guard cannot be rearmed")
            raise self._failure()
        self._start_attempted = True
        try:
            openrouter = await self._read_openrouter()
            self._accept_sample(openrouter, initial=True)
        except GuardFailure as failure:
            self._latch(failure.reason)
            raise self._failure() from None
        self._started = True
        self._monitor_task = asyncio.create_task(self._monitor())
        try:
            await asyncio.wait_for(
                self._ready.wait(), timeout=self.thresholds.probe_timeout + 1.0
            )
        except TimeoutError:
            await self.fail_closed("monitor did not become ready")
        await self.ensure_healthy()

    async def _monitor(self) -> None:
        try:
            while not self._closing:
                openrouter = await self._read_openrouter()
                self._accept_sample(openrouter)
                self._ready.set()
                await asyncio.sleep(self.thresholds.poll_interval)
        except asyncio.CancelledError:
            if not self._closing:
                await self.fail_closed("monitor task stopped")
            raise
        except GuardFailure as failure:
            await self.fail_closed(failure.reason)
        except Exception:  # noqa: BLE001
            await self.fail_closed("monitor failed")

    async def ensure_healthy(self) -> None:
        if self.failure_reason is not None:
            raise self._failure()
        task = self._monitor_task
        if not self._started or not self._ready.is_set() or task is None:
            raise GuardFailure("guard is not ready")
        if task.done():
            await self.fail_closed("monitor task stopped")
            raise self._failure()
        if self._last_sample_at is None or self._clock() - self._last_sample_at > self.thresholds.max_sample_age:
            await self.fail_closed("monitor data stale")
            raise self._failure()

    async def fail_closed(
        self,
        reason: str,
        *,
        cleanup_uncertain: bool = False,
        cancel_job: bool = True,
    ) -> None:
        self._latch(reason, cleanup_uncertain=cleanup_uncertain)
        task = self._job_task
        if cancel_job and task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
        await self.cleanup_bound()


    def bind_sandbox(
        self,
        environment: object,
        sandbox: object,
        identity: _EnvironmentIdentity,
    ) -> SandboxBinding:
        try:
            if _environment_identity(environment) != identity:
                raise ValueError
            object_id = _sandbox_object_id(sandbox)
        except (ValueError, TypeError, AttributeError):
            self._latch("sandbox identity unavailable", cleanup_uncertain=True)
            raise self._failure() from None

        for existing in self._bindings:
            if existing.environment is environment:
                if existing.object_id == object_id and existing.sandbox is sandbox:
                    return existing
                self._latch("unexpected additional sandbox", cleanup_uncertain=True)
                binding = SandboxBinding(
                    self._owner, environment, identity, sandbox, object_id
                )
                self._bindings.append(binding)
                return binding

        binding = SandboxBinding(self._owner, environment, identity, sandbox, object_id)
        self._bindings.append(binding)
        return binding

    def _binding_is_exact(self, binding: SandboxBinding) -> bool:
        if binding.owner is not self._owner:
            return False
        try:
            return (
                _environment_identity(binding.environment) == binding.identity
                and _sandbox_object_id(binding.sandbox) == binding.object_id
            )
        except (ValueError, TypeError, AttributeError):
            return False

    async def _cleanup_binding_locked(self, binding: SandboxBinding) -> None:
        if binding.cleanup_state in {"complete", "uncertain"}:
            return
        if not self._binding_is_exact(binding):
            binding.cleanup_state = "uncertain"
            self._latch("sandbox identity changed", cleanup_uncertain=True)
            return
        binding.cleanup_state = "in_progress"
        try:
            terminate = binding.sandbox.terminate
            termination = asyncio.create_task(terminate.aio(wait=True))
            try:
                done, _ = await asyncio.wait(
                    {termination}, timeout=self.thresholds.cleanup_timeout
                )
            except asyncio.CancelledError:
                termination.cancel()
                termination.add_done_callback(_consume_task_exception)
                raise
            if not done:
                termination.cancel()
                termination.add_done_callback(_consume_task_exception)
                raise TimeoutError
            termination.result()
        except asyncio.CancelledError:
            binding.cleanup_state = "uncertain"
            self._latch("sandbox cleanup interrupted", cleanup_uncertain=True)
            raise
        except Exception:  # noqa: BLE001
            binding.cleanup_state = "uncertain"
            self._latch("sandbox cleanup failed", cleanup_uncertain=True)
        else:
            binding.cleanup_state = "complete"

    async def cleanup_environment(self, environment: object) -> None:
        binding = next(
            (item for item in self._bindings if item.environment is environment), None
        )
        if binding is None:
            self._latch("sandbox is not bound", cleanup_uncertain=True)
            return
        async with self._cleanup_lock:
            await self._cleanup_binding_locked(binding)

    async def cleanup_bound(self) -> None:
        async with self._cleanup_lock:
            for binding in self._bindings:
                await self._cleanup_binding_locked(binding)

    async def authorize_exec(self, environment: object) -> None:
        await self.ensure_healthy()
        binding = next(
            (item for item in self._bindings if item.environment is environment), None
        )
        if binding is None:
            await self.fail_closed("sandbox is not bound", cleanup_uncertain=True)
            raise self._failure()
        try:
            identity_matches = _environment_identity(environment) == binding.identity
            sandbox_matches = getattr(environment, "_sandbox", None) is binding.sandbox
            object_matches = _sandbox_object_id(binding.sandbox) == binding.object_id
        except (ValueError, TypeError, AttributeError):
            identity_matches = sandbox_matches = object_matches = False
        if not identity_matches or not sandbox_matches or not object_matches:
            await self.fail_closed("sandbox identity mismatch", cleanup_uncertain=True)
            raise self._failure()

    async def terminate_environment(self, environment: object) -> None:
        current = getattr(environment, "_sandbox", None)
        if current is None:
            return
        binding = next(
            (item for item in self._bindings if item.environment is environment), None
        )
        if binding is None or current is not binding.sandbox:
            await self.fail_closed("sandbox identity mismatch", cleanup_uncertain=True)
            raise self._failure()
        await self.cleanup_environment(environment)
        if binding.cleanup_state != "complete":
            raise self._failure()

    async def reconcile_cancelled_create(
        self,
        environment: object,
        identity: _EnvironmentIdentity,
        create_task: asyncio.Task[object],
    ) -> None:
        try:
            sandbox = await asyncio.shield(create_task)
        except asyncio.CancelledError:
            self._latch("sandbox creation result unresolved", cleanup_uncertain=True)
            raise
        except Exception:  # noqa: BLE001
            self._latch("sandbox creation failed", cleanup_uncertain=True)
            return
        try:
            self.bind_sandbox(environment, sandbox, identity)
        except GuardFailure:
            return
        await self.cleanup_bound()

    async def close(self) -> None:
        self._closing = True
        task = self._monitor_task
        if task is not None and not task.done():
            if self.failure_reason is None:
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.cleanup_bound()
        if self.failure_reason is not None or self.cleanup_uncertain:
            raise self._failure()


class SharedRoleSpendGuard(SpendGuard):
    def __init__(self, thresholds: SharedRoleThresholds, *, openrouter_probe: Probe | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(thresholds, openrouter_probe=openrouter_probe, clock=clock)

    async def _read_openrouter(self) -> SharedRoleOpenRouterSnapshot:
        value = await self._probe("OpenRouter", self.openrouter_probe)
        try:
            return SharedRoleOpenRouterSnapshot.parse(value)
        except (ValueError, TypeError, OverflowError):
            raise GuardFailure("monitor data malformed") from None

    def _check_headroom(
        self, snapshot: SharedRoleOpenRouterSnapshot, baseline: SharedRoleOpenRouterSnapshot | None = None
    ) -> None:
        if (not snapshot.limit_reset_present or snapshot.limit_reset is not None
                or snapshot.is_management_key is not False or snapshot.is_provisioning_key is not False
                or snapshot.include_byok_in_limit is None):
            raise GuardFailure("shared_roles requires a finite ordinary non-resetting OpenRouter key")
        if (snapshot.limit <= 0 or snapshot.remaining > snapshot.limit or snapshot.usage > snapshot.limit
                or snapshot.usage + snapshot.remaining > snapshot.limit + _MONEY_EPSILON):
            raise GuardFailure("OpenRouter key headroom metadata is malformed")
        allowance = self.thresholds.shared_allowance_usd
        key_usage_delta = Decimal(0) if baseline is None else snapshot.usage - baseline.usage
        minimum = max(Decimal(0), allowance - key_usage_delta)
        if snapshot.remaining + _MONEY_EPSILON < minimum:
            raise GuardFailure("OpenRouter limit_remaining is below shared_allowance_usd")
        budgets = list(snapshot.budgets)
        if snapshot.effective_budget is not None:
            budgets.append(snapshot.effective_budget)
        for budget in budgets:
            if budget.reset_interval is not None or budget.resets_at is not None:
                raise GuardFailure("shared_roles does not support resetting OpenRouter budgets")
            if (budget.limit <= 0 or budget.remaining > budget.limit or budget.spend > budget.limit
                    or budget.spend + budget.remaining > budget.limit + _MONEY_EPSILON):
                raise GuardFailure("OpenRouter budget metadata is malformed")
            if budget.remaining + _MONEY_EPSILON < minimum:
                raise GuardFailure("OpenRouter budget remaining is below shared_allowance_usd")

    @staticmethod
    def _metadata_signature(snapshot: SharedRoleOpenRouterSnapshot) -> tuple[object, ...]:
        return (snapshot.limit, snapshot.limit_reset, snapshot.is_management_key, snapshot.is_provisioning_key,
                snapshot.limit_reset_present, snapshot.include_byok_in_limit, snapshot.budgets_present,
                snapshot.effective_budget_present, tuple(budget.identity for budget in snapshot.budgets),
                None if snapshot.effective_budget is None else snapshot.effective_budget.identity)

    def _accept_sample(self, openrouter: SharedRoleOpenRouterSnapshot, *, initial: bool = False) -> None:
        if initial:
            self._check_headroom(openrouter)
            self._baseline = openrouter
        else:
            previous = self._previous_openrouter
            baseline = self._baseline
            if previous is None or baseline is None:
                raise GuardFailure("monitor state invalid")
            if self._metadata_signature(openrouter) != self._metadata_signature(baseline):
                raise GuardFailure("OpenRouter budget metadata changed")
            if openrouter.usage < previous.usage:
                raise GuardFailure("OpenRouter usage counter decreased")
            if openrouter.remaining > previous.remaining + _MONEY_EPSILON:
                raise GuardFailure("OpenRouter remaining counter increased")
            self._check_headroom(openrouter, baseline)
            key_usage_delta = openrouter.usage - baseline.usage
            for before, current in zip(previous.budgets, openrouter.budgets, strict=True):
                if current.spend < before.spend or current.remaining > before.remaining + _MONEY_EPSILON:
                    raise GuardFailure("OpenRouter budget counters moved nonmonotonically")
            if baseline.effective_budget is not None:
                previous_effective, current_effective = previous.effective_budget, openrouter.effective_budget
                if previous_effective is None or current_effective is None:
                    raise GuardFailure("OpenRouter effective budget metadata changed")
                if (current_effective.spend < previous_effective.spend
                        or current_effective.remaining > previous_effective.remaining + _MONEY_EPSILON):
                    raise GuardFailure("OpenRouter effective budget counters moved nonmonotonically")
            if key_usage_delta >= self.thresholds.shared_allowance_usd:
                raise GuardFailure("OpenRouter shared allowance reached")
        self._previous_openrouter = openrouter
        self._last_sample_at = self._clock()

    def sanitized_effective_parameters(self) -> dict[str, Any] | None:
        baseline = self._baseline
        if baseline is None:
            return None
        budgets = list(baseline.budgets)
        effective = baseline.effective_budget
        return {
            "shared_allowance_usd": str(self.thresholds.shared_allowance_usd),
            "shared_allowance_source": "operator_attested_allocation",
            "key_limit_usd": str(baseline.limit),
            "key_remaining_usd": str(baseline.remaining),
            "key_usage_baseline_usd": str(baseline.usage),
            "budget_remaining_usd": [str(budget.remaining) for budget in budgets],
            "effective_budget_remaining_usd": None if effective is None else str(effective.remaining),
            "runtime_metadata_confirmed": True,
        }


class SplitSpendGuard(SpendGuard):
    def __init__(self, thresholds: SplitThresholds, *, agent_probe: Probe | None = None,
                 world_probe: Probe | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(
            Thresholds(
                KEY_CEILING,
                Decimal(1),
                probe_timeout=thresholds.probe_timeout,
                poll_interval=thresholds.poll_interval,
                max_sample_age=thresholds.max_sample_age,
                cleanup_timeout=thresholds.cleanup_timeout,
            ),
            clock=clock,
            openrouter_probe=lambda: None,
        )
        self.thresholds = thresholds
        self.agent_probe = agent_probe or (lambda: split_openrouter_key_probe("OPENROUTER_AGENT_API_KEY", thresholds.probe_timeout))
        self.world_probe = world_probe or (lambda: split_openrouter_key_probe("OPENROUTER_WORLD_API_KEY", thresholds.probe_timeout))
        self._split_baseline: dict[str, OpenRouterSnapshot] = {}
        self._split_previous: dict[str, OpenRouterSnapshot] = {}

    async def _read_openrouter(self) -> dict[str, OpenRouterSnapshot]:
        agent_raw, world_raw = await asyncio.gather(
            self._probe("OpenRouter agent", self.agent_probe),
            self._probe("OpenRouter world", self.world_probe),
        )
        try:
            parsed = {}
            for role, raw in (("agent", agent_raw), ("world", world_raw)):
                if not isinstance(raw, Mapping) or not isinstance(raw.get("data"), Mapping):
                    raise TypeError
                if "limit_reset" not in raw["data"] or raw["data"]["limit_reset"] is not None:
                    raise ValueError
                parsed[role] = OpenRouterSnapshot.parse(raw)
            return parsed
        except (ValueError, TypeError, OverflowError):
            raise GuardFailure("split monitor data malformed or resettable") from None

    def _accept_sample(self, openrouter: dict[str, OpenRouterSnapshot], *, initial: bool = False) -> None:
        limits = {"agent": (KEY_CEILING, self.thresholds.agent_trial_delta),
                  "world": (Decimal(100), self.thresholds.world_trial_delta)}
        for role, sample in openrouter.items():
            ceiling, delta = limits[role]
            if sample.limit != ceiling or sample.remaining > sample.limit or sample.usage > sample.limit:
                raise GuardFailure(f"OpenRouter {role} key ceiling or counters invalid")
            if sample.usage + sample.remaining > sample.limit + _MONEY_EPSILON:
                raise GuardFailure(f"OpenRouter {role} counters malformed")
            if initial:
                if sample.remaining < delta:
                    raise GuardFailure(f"OpenRouter {role} remaining limit is below trial threshold")
                self._split_baseline[role] = sample
            else:
                previous = self._split_previous.get(role)
                baseline = self._split_baseline.get(role)
                if previous is None or baseline is None:
                    raise GuardFailure("split monitor state invalid")
                if (sample.usage < previous.usage or sample.remaining > previous.remaining + _MONEY_EPSILON
                        or sample.limit != previous.limit):
                    raise GuardFailure(f"OpenRouter {role} counters are nonmonotonic")
                if sample.usage - baseline.usage >= delta:
                    raise GuardFailure(f"OpenRouter {role} trial threshold reached")
            self._split_previous[role] = sample
        self._last_sample_at = self._clock()

    async def start(self) -> None:
        agent_key = os.environ.get("OPENROUTER_AGENT_API_KEY")
        world_key = os.environ.get("OPENROUTER_WORLD_API_KEY")
        if not agent_key or not world_key or agent_key == world_key:
            self._latch("split OpenRouter keys missing or not independent")
            raise self._failure()
        await super().start()


@contextmanager
def install_modal_hooks(
    guard: SpendGuard,
    *,
    on_sandbox_created: Callable[[Any], None] | None = None,
):
    from harbor.environments.modal import ModalEnvironment

    original_create = ModalEnvironment._create_sandbox
    original_exec = ModalEnvironment._sdk_exec
    original_terminate = ModalEnvironment._terminate_sandbox
    raw_create = getattr(original_create, "__wrapped__", None)
    if raw_create is None:
        raise GuardFailure("pinned Harbor sandbox hook is unsupported")

    async def guarded_create(
        self: object,
        *,
        entrypoint: list[str] | None = None,
        block_network: bool | None = None,
        experimental_options: dict[str, Any] | None = None,
    ) -> object:
        try:
            identity = _environment_identity(self)
        except (ValueError, TypeError, AttributeError):
            await guard.fail_closed("sandbox environment identity unavailable")
            raise guard._failure() from None
        await guard.ensure_healthy()
        creation = asyncio.create_task(
            raw_create(
                self,
                entrypoint=entrypoint,
                block_network=block_network,
                experimental_options=experimental_options,
            )
        )
        try:
            sandbox = await asyncio.shield(creation)
        except asyncio.CancelledError:
            guard._latch("job cancelled during sandbox creation")
            await asyncio.shield(
                guard.reconcile_cancelled_create(self, identity, creation)
            )
            raise
        except Exception:
            guard._latch("sandbox creation failed", cleanup_uncertain=True)
            raise
        binding = guard.bind_sandbox(self, sandbox, identity)
        if guard.failure_reason is not None:
            await guard.cleanup_bound()
            raise guard._failure()
        try:
            await guard.ensure_healthy()
        except GuardFailure:
            await guard.cleanup_bound()
            raise
        if on_sandbox_created is not None:
            with suppress(Exception):
                on_sandbox_created(self)
        return binding.sandbox

    async def guarded_exec(
        self: object,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        shell: str = "bash",
        login: bool = False,
    ) -> object:
        await guard.authorize_exec(self)
        return await original_exec(
            self,
            command,
            cwd=cwd,
            env=env,
            timeout_sec=timeout_sec,
            shell=shell,
            login=login,
        )

    async def guarded_terminate(self: object) -> None:
        await guard.terminate_environment(self)

    ModalEnvironment._create_sandbox = guarded_create
    ModalEnvironment._sdk_exec = guarded_exec
    ModalEnvironment._terminate_sandbox = guarded_terminate
    try:
        yield
    finally:
        ModalEnvironment._create_sandbox = original_create
        ModalEnvironment._sdk_exec = original_exec
        ModalEnvironment._terminate_sandbox = original_terminate

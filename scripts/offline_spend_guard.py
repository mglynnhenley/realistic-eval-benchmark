from __future__ import annotations

import asyncio
import json
import math
import os
import time
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
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
    modal_cycle_stop: Decimal
    probe_timeout: float = 10.0
    poll_interval: float = 15.0
    max_sample_age: float = 35.0
    cleanup_timeout: float = 10.0

    def __post_init__(self) -> None:
        try:
            key_ceiling = _money(self.openrouter_key_ceiling)
            delta = _money(self.openrouter_trial_delta)
            modal_stop = _money(self.modal_cycle_stop)
        except ValueError:
            raise ValueError("cost thresholds must be finite and nonnegative") from None
        if key_ceiling != KEY_CEILING:
            raise ValueError("OpenRouter key ceiling must be exactly $50")
        if delta <= 0 or delta > key_ceiling or modal_stop <= 0:
            raise ValueError("trial and Modal thresholds must be positive and bounded")
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
        object.__setattr__(self, "modal_cycle_stop", modal_stop)


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


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError from None
    else:
        raise TypeError
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class ModalSnapshot:
    metered_cost: Decimal
    cycle_start: datetime
    cycle_end: datetime

    @classmethod
    def parse(cls, value: object) -> ModalSnapshot:
        if isinstance(value, Mapping):
            metered = value.get("metered_cost")
            cycle_start = value.get("start", value.get("cycle_start"))
            cycle_end = value.get("end", value.get("cycle_end"))
        else:
            metered = getattr(value, "metered_cost", None)
            cycle_start = getattr(value, "start", None)
            cycle_end = getattr(value, "end", None)
        start = _datetime(cycle_start)
        end = _datetime(cycle_end)
        if start >= end:
            raise ValueError
        return cls(_money(metered), start, end)


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


async def modal_cycle_probe() -> object:
    try:
        from modal import Workspace

        workspace = Workspace.from_context()
        return await workspace.billing.summary()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        raise GuardFailure("Modal probe failed") from None


class SpendGuard:
    def __init__(
        self,
        thresholds: Thresholds,
        *,
        openrouter_probe: Probe | None = None,
        modal_probe: Probe | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.thresholds = thresholds
        self.openrouter_probe = openrouter_probe or (
            lambda: openrouter_key_probe(thresholds.probe_timeout)
        )
        self.modal_probe = modal_probe or modal_cycle_probe
        self._clock = clock
        self._owner = object()
        self._baseline: OpenRouterSnapshot | None = None
        self._previous_openrouter: OpenRouterSnapshot | None = None
        self._previous_modal: ModalSnapshot | None = None
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

    async def _read_pair(self) -> tuple[OpenRouterSnapshot, ModalSnapshot]:
        openrouter_task = asyncio.create_task(self._probe("OpenRouter", self.openrouter_probe))
        modal_task = asyncio.create_task(self._probe("Modal", self.modal_probe))
        try:
            openrouter_value, modal_value = await asyncio.gather(openrouter_task, modal_task)
        except BaseException:
            for task in (openrouter_task, modal_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(openrouter_task, modal_task, return_exceptions=True)
            raise
        try:
            return OpenRouterSnapshot.parse(openrouter_value), ModalSnapshot.parse(modal_value)
        except (ValueError, TypeError, OverflowError):
            raise GuardFailure("monitor data malformed") from None

    def _accept_sample(
        self, openrouter: OpenRouterSnapshot, modal: ModalSnapshot, *, initial: bool = False
    ) -> None:
        if openrouter.limit != self.thresholds.openrouter_key_ceiling:
            raise GuardFailure("OpenRouter key ceiling does not match $50")
        if openrouter.remaining > openrouter.limit:
            raise GuardFailure("OpenRouter counters malformed")
        if openrouter.usage > openrouter.limit:
            raise GuardFailure("OpenRouter counters malformed")
        if openrouter.usage + openrouter.remaining > openrouter.limit + _MONEY_EPSILON:
            raise GuardFailure("OpenRouter counters malformed")
        if modal.cycle_start >= modal.cycle_end:
            raise GuardFailure("Modal cycle data malformed")

        if initial:
            if openrouter.remaining < self.thresholds.openrouter_trial_delta:
                raise GuardFailure("OpenRouter remaining limit is below trial threshold")
            if modal.metered_cost >= self.thresholds.modal_cycle_stop:
                raise GuardFailure("Modal cycle threshold already reached")
            self._baseline = openrouter
        else:
            previous_openrouter = self._previous_openrouter
            previous_modal = self._previous_modal
            if previous_openrouter is None or previous_modal is None or self._baseline is None:
                raise GuardFailure("monitor state invalid")
            if openrouter.usage < previous_openrouter.usage:
                raise GuardFailure("OpenRouter usage counter decreased")
            if openrouter.remaining > previous_openrouter.remaining + _MONEY_EPSILON:
                raise GuardFailure("OpenRouter remaining counter increased")
            if openrouter.limit != previous_openrouter.limit:
                raise GuardFailure("OpenRouter key ceiling changed")
            if modal.cycle_start != previous_modal.cycle_start or modal.cycle_end != previous_modal.cycle_end:
                raise GuardFailure("Modal billing cycle changed")
            if modal.metered_cost < previous_modal.metered_cost:
                raise GuardFailure("Modal metered counter decreased")
            if openrouter.usage - self._baseline.usage >= self.thresholds.openrouter_trial_delta:
                raise GuardFailure("OpenRouter trial threshold reached")
            if modal.metered_cost >= self.thresholds.modal_cycle_stop:
                raise GuardFailure("Modal cycle threshold reached")

        self._previous_openrouter = openrouter
        self._previous_modal = modal
        self._last_sample_at = self._clock()

    async def start(self) -> None:
        if self._start_attempted:
            self._latch("guard cannot be rearmed")
            raise self._failure()
        self._start_attempted = True
        try:
            openrouter, modal = await self._read_pair()
            self._accept_sample(openrouter, modal, initial=True)
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
                openrouter, modal = await self._read_pair()
                self._accept_sample(openrouter, modal)
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


@contextmanager
def install_modal_hooks(guard: SpendGuard):
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

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


class SpendGuard:
    def __init__(
        self,
        thresholds: Thresholds,
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
        self._probe_task: asyncio.Task[Any] | None = None
        self._bindings: list[SandboxBinding] = []
        self._total_deadline: float | None = None
        self._work_deadline: float | None = None
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

    def start_total_deadline(self, seconds: int) -> None:
        if type(seconds) is not int or seconds <= self.thresholds.cleanup_timeout:
            raise ValueError("total time limit must exceed the cleanup timeout")
        if self._total_deadline is not None:
            raise ValueError("total deadline is already set")
        self._total_deadline = self._clock() + seconds
        self._work_deadline = self._total_deadline - self.thresholds.cleanup_timeout

    async def enforce_total_deadline(self) -> None:
        if self._work_deadline is None:
            return
        await asyncio.sleep(max(0.0, self._work_deadline - self._clock()))
        if self.failure_reason is None:
            await self.fail_closed("total run deadline exceeded", cancel_probe=True)

    def sandbox_lifetime_timeout(self, current: int | None) -> int | None:
        if self._total_deadline is None:
            return current
        remaining = math.floor(self._total_deadline - self._clock())
        if remaining < 1:
            raise GuardFailure("total deadline leaves no whole second for sandbox lifetime")
        if current is None:
            return remaining
        if type(current) is not int or current < 1:
            raise GuardFailure("Modal sandbox lifetime is invalid")
        return min(current, remaining)

    async def bounded_exec_timeout(self, current: int | None) -> int | None:
        if self._work_deadline is None:
            return current
        remaining = math.floor(self._work_deadline - self._clock())
        if remaining < 1:
            await self.fail_closed("total run deadline exceeded")
            raise self._failure()
        if current is None:
            return remaining
        return min(current, remaining)

    def _cleanup_wait_seconds(self) -> float:
        if self._total_deadline is None:
            return self.thresholds.cleanup_timeout
        return min(self.thresholds.cleanup_timeout, self._total_deadline - self._clock())

    def _mark_cleanup_uncertain(self, reason: str) -> None:
        for binding in self._bindings:
            if binding.cleanup_state not in {"complete", "uncertain"}:
                binding.cleanup_state = "uncertain"
        self._latch(reason, cleanup_uncertain=True)

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
        self._probe_task = task
        try:
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
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                raise GuardFailure(f"{name} probe failed") from None
        finally:
            if self._probe_task is task:
                self._probe_task = None

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
        if self._work_deadline is not None and self._clock() >= self._work_deadline:
            await self.fail_closed("total run deadline exceeded", cancel_probe=True)
            raise self._failure()
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
        cancel_probe: bool = False,
    ) -> None:
        self._latch(reason, cleanup_uncertain=cleanup_uncertain)
        probe = self._probe_task
        if cancel_probe and probe is not None and not probe.done():
            probe.cancel()
            probe.add_done_callback(_consume_task_exception)
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
            timeout = self._cleanup_wait_seconds()
            if timeout <= 0:
                self._mark_cleanup_uncertain("sandbox cleanup exceeded total deadline")
                return
            terminate = binding.sandbox.terminate
            termination = asyncio.create_task(terminate.aio(wait=True))
            try:
                done, _ = await asyncio.wait(
                    {termination}, timeout=timeout
                )
            except asyncio.CancelledError:
                termination.cancel()
                termination.add_done_callback(_consume_task_exception)
                raise
            if not done:
                termination.cancel()
                termination.add_done_callback(_consume_task_exception)
                if self._total_deadline is None:
                    raise TimeoutError
                binding.cleanup_state = "uncertain"
                self._latch("sandbox cleanup timed out", cleanup_uncertain=True)
                return
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
        if not await self._acquire_cleanup_lock():
            return
        try:
            await self._cleanup_binding_locked(binding)
        finally:
            self._cleanup_lock.release()

    async def cleanup_bound(self) -> None:
        if not await self._acquire_cleanup_lock():
            return
        try:
            for binding in self._bindings:
                await self._cleanup_binding_locked(binding)
        finally:
            self._cleanup_lock.release()

    async def _acquire_cleanup_lock(self) -> bool:
        if self._total_deadline is None:
            await self._cleanup_lock.acquire()
            return True
        timeout = self._cleanup_wait_seconds()
        if timeout <= 0:
            self._mark_cleanup_uncertain("sandbox cleanup exceeded total deadline")
            return False
        try:
            await asyncio.wait_for(self._cleanup_lock.acquire(), timeout=timeout)
        except TimeoutError:
            self._mark_cleanup_uncertain("sandbox cleanup timed out")
            return False
        return True

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
        if self._total_deadline is None:
            try:
                sandbox = await asyncio.shield(create_task)
            except asyncio.CancelledError:
                self._latch("sandbox creation result unresolved", cleanup_uncertain=True)
                raise
            except Exception:  # noqa: BLE001
                self._latch("sandbox creation failed", cleanup_uncertain=True)
                return
        else:
            timeout = self._cleanup_wait_seconds()
            if timeout <= 0:
                create_task.cancel()
                create_task.add_done_callback(_consume_task_exception)
                self._latch("sandbox creation result unresolved", cleanup_uncertain=True)
                return
            done, _ = await asyncio.wait({create_task}, timeout=timeout)
            if not done:
                create_task.cancel()
                create_task.add_done_callback(_consume_task_exception)
                self._latch("sandbox creation result unresolved", cleanup_uncertain=True)
                return
            try:
                sandbox = create_task.result()
            except asyncio.CancelledError:
                self._latch("sandbox creation result unresolved", cleanup_uncertain=True)
                return
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
            if self.failure_reason is None or self.failure_reason == "total run deadline exceeded":
                task.cancel()
            if self._total_deadline is None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            else:
                remaining = max(0.0, self._total_deadline - self._clock())
                done, _ = await asyncio.wait({task}, timeout=remaining)
                if not done:
                    task.cancel()
                    task.add_done_callback(_consume_task_exception)
                    self._latch("monitor cleanup exceeded total deadline", cleanup_uncertain=True)
                else:
                    try:
                        task.result()
                    except asyncio.CancelledError:
                        pass
        await self.cleanup_bound()
        if self.failure_reason is not None or self.cleanup_uncertain:
            raise self._failure()


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
        timeout_attribute = hasattr(self, "_sandbox_timeout")
        current_timeout = getattr(self, "_sandbox_timeout", None)
        try:
            sandbox_timeout = guard.sandbox_lifetime_timeout(current_timeout)
        except GuardFailure as failure:
            await guard.fail_closed(failure.reason, cancel_probe=True)
            raise guard._failure() from None
        if guard._total_deadline is not None:
            self._sandbox_timeout = sandbox_timeout
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
        finally:
            if guard._total_deadline is not None:
                if timeout_attribute:
                    self._sandbox_timeout = current_timeout
                else:
                    delattr(self, "_sandbox_timeout")
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
        timeout_sec = await guard.bounded_exec_timeout(timeout_sec)
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

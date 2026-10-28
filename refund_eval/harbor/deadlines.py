"""One opt-in, finite lifecycle policy. Runtime epochs stay in their clock domain."""
from __future__ import annotations

import asyncio
import math
import time

VERSION = "bounded-lifecycle-v1"
HOST_START_ENV = "REFUND_EVAL_NATIVE_HOST_START"


def trial_deadlines(sample_seconds: int, version: str = VERSION) -> dict:
    if version != VERSION or type(sample_seconds) is not int or not 1 <= sample_seconds <= 72000:
        raise ValueError("Unsupported trial deadline policy or sample limit")
    return {
        "version": VERSION, "sample_seconds": sample_seconds,
        "setup_seconds": 1800, "startup_seconds": 120, "flush_seconds": 120,
        "collect_seconds": 120, "verify_seconds": 60, "termination_seconds": 180,
        "worker_soft_seconds": sample_seconds + 120,
        "worker_hard_seconds": sample_seconds + 240,
        "agent_seconds": sample_seconds + 360,
        "command_soft_seconds": sample_seconds + 2220,
        "command_hard_seconds": sample_seconds + 2400,
        "sandbox_seconds": sample_seconds + 3000,
    }


def validate_deadlines(value: dict, sample_seconds: int) -> dict:
    expected = trial_deadlines(sample_seconds)
    # bools/floats must not masquerade as the canonical integer bounds.
    if value != expected or any(type(value.get(k)) is not type(v) for k, v in expected.items()):
        raise ValueError("Trial deadline policy differs from canonical finite bounds")
    return expected


def remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def exec_timeout_seconds(deadline: float, cap: int | None = None) -> int:
    """Modal's exec wire timeout is whole seconds; zero is not an expired bound."""
    if cap is not None and (type(cap) is not int or cap < 1):
        raise ValueError("Exec timeout cap must be a positive integer")
    duration = remaining(deadline)
    seconds = math.floor(min(duration, cap) if cap is not None else duration)
    if seconds < 1:
        raise TimeoutError("Less than one whole second remains for bounded exec")
    return seconds


def host_start(value: str | None) -> float:
    """Only inherited by the local Harbor command, never sent to a sandbox."""
    now = time.monotonic()
    start = now if value is None else float(value)
    if not math.isfinite(start) or start > now:
        raise ValueError("Invalid local command clock anchor")
    return start


async def settle(task: asyncio.Task, deadline: float, *, on_cancel=None):
    """Await an owned task without resetting its bound on repeated cancellation.

    Cleanup callers preserve their original error; cancellation of their waiter
    must not cancel native log closure or start another grace interval.
    """
    while not task.done():
        left = remaining(deadline)
        if not left:
            raise TimeoutError("Lifecycle deadline exhausted")
        try:
            await asyncio.wait({task}, timeout=left)
        except asyncio.CancelledError:
            if on_cancel is not None:
                on_cancel()
            continue
    return task.result()


async def bounded(awaitable, deadline: float):
    task = asyncio.ensure_future(awaitable)
    try:
        return await settle(task, deadline)
    finally:
        if not task.done():
            task.cancel()

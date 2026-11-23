"""Per-trial state and trial facts, behind an interface that does not import Inspect.

Callers read and write keyed trial state through `world_store()`, and read the
current trial's facts through `scenario_id()` and `trial_lock()`. The backend is
chosen with `install()`. Until one is installed, the Inspect backend in
`refund_eval.world.inspect_store` is used, so Inspect is imported only when trial
state is first touched.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import anyio

SESSION_END_KEY = "harness_session_end"


class WorldStore(Protocol):
    """Keyed, JSON-serialisable per-trial values."""

    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...


class TrialContext(Protocol):
    """The current trial: its store, its scenario and its lock."""

    def store(self) -> WorldStore: ...

    def scenario_id(self) -> str | None:
        """The trial's scenario id, or None when there is no active trial."""
        ...

    def lock(self) -> anyio.Lock:
        """One lock per trial, so parallel tool calls are applied in order."""
        ...


_context: TrialContext | None = None


def install(context: TrialContext | None) -> None:
    """Use `context` for trial state; None restores the default Inspect backend."""
    global _context
    _context = context


def trial() -> TrialContext:
    global _context
    if _context is None:
        from refund_eval.world.inspect_store import InspectTrial

        _context = InspectTrial()
    return _context


def world_store() -> WorldStore:
    return trial().store()


def session_end() -> datetime | None:
    end = world_store().get(SESSION_END_KEY)
    return datetime.fromisoformat(end) if end is not None else None


def scenario_id() -> str | None:
    return trial().scenario_id()


def trial_lock() -> anyio.Lock:
    return trial().lock()

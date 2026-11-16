"""The Inspect backend for `refund_eval.world.store`: the sample store and sample metadata."""

from __future__ import annotations

import anyio
from inspect_ai.util import Store


class InspectTrial:
    def __init__(self) -> None:
        self._locks: dict[int, anyio.Lock] = {}

    def store(self) -> Store:
        from inspect_ai.util import store  # looked up per call, so tests can patch it

        return store()

    def scenario_id(self) -> str | None:
        from inspect_ai.solver._task_state import (
            sample_state,  # no public accessor for sample metadata
        )

        state = sample_state()
        sid = (state.metadata or {}).get("scenario") if state is not None else None
        return str(sid) if sid else None

    def lock(self) -> anyio.Lock:
        # one lock per sample store
        key = id(self.store())
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = anyio.Lock()
        return lock

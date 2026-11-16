"""Capture and restore everything a trial's world needs to continue identically.

A snapshot is a JSON copy of every entry in the trial store (`world/store.py`), with
no allowlist to keep in sync. That covers:

- the World (WORLD_KEY), including the per-trial seed;
- the event log (`event_log.EVENTS_KEY`), so the log carries on at the same seq;
- random stream positions: reply-jitter draws (`sim/open_world.py`) and id draws
  (`world/ids.py`), plus the temperaments already chosen;
- other world-side state: mail client state, claim observations, company records,
  dossiers, rules and recorded model completions (`models/replay.py`).

The event queue is not stored: `runtime/events.py` derives it from the World, so
restoring the World restores the queue. `restore()` puts the store back exactly as
captured and logs nothing, so a run continued from a snapshot writes the same log as
one never interrupted. Checkpoint resume (`runtime/checkpoint.py`) is built on these
two functions. Nothing here imports Inspect.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, Field

from refund_eval.world.event_log import EVENTS_KEY
from refund_eval.world.ids import ID_DRAWS_KEY
from refund_eval.world.state import WORLD_KEY
from refund_eval.world.store import WorldStore, world_store


class TrialSnapshot(BaseModel):
    snapshot_version: Literal[1] = 1
    store: dict[str, Any] = Field(default_factory=dict)

    @property
    def seed(self) -> int | None:
        return (self.store.get(WORLD_KEY) or {}).get("seed")

    @property
    def event_seq(self) -> int:
        """The seq the next event log entry will get."""
        log = self.store.get(EVENTS_KEY) or []
        return log[-1]["seq"] + 1 if log else 0

    @property
    def id_draws(self) -> dict[str, int]:
        return dict(self.store.get(ID_DRAWS_KEY) or {})


def capture(store: WorldStore | Any | None = None) -> TrialSnapshot:
    """Copy every entry of `store` (default: the current trial's), so later changes cannot alter it."""
    current = world_store() if store is None else store
    saved = deepcopy(dict(current.items()))
    world = saved.get(WORLD_KEY)
    if hasattr(world, "model_dump"):
        saved[WORLD_KEY] = world.model_dump(mode="json")
    return TrialSnapshot(store=saved)


def restore(snapshot: TrialSnapshot, store: WorldStore | Any | None = None) -> None:
    """Replace every entry of `store` (default: the current trial's) with the snapshot's."""
    current = world_store() if store is None else store
    for key in list(current.keys()):
        delete = getattr(current, "delete", None)
        if delete is not None:
            delete(key)
        else:
            del current[key]
    for key, value in snapshot.store.items():
        current.set(key, deepcopy(value))

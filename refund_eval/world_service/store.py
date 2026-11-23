"""The world service's trial state: an in-memory `TrialContext`, written to an output directory.

`state.json` holds every trial store key except the event log (the world under "world",
the tool audit, claim observations, company records, the agent's activity under
ACTIVITY_KEY) with the run settings and the stop reason. `events.jsonl` holds the event log, one entry per line, appended as it
grows. Both are rewritten after every tool call and turn, and at shutdown.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import anyio

from refund_eval.world.event_log import EVENTS_KEY
from refund_eval.world.state import WORLD_KEY
from refund_eval.world_service.status import (
    WORLD_STATUS_FILE,
    atomic_json,
    world_status,
)

STATE_FILE = "state.json"
EVENTS_FILE = "events.jsonl"
# Every MCP tool call and `/turn` the service received, in order (`WorldTrial._record`); the
# verifier grades the agent's activity from these (`grading.world_records`), not the agent's log.
ACTIVITY_KEY = "agent_activity"


class MemoryStore(dict):
    """Keyed trial values, as the Inspect store holds them."""

    def set(self, key: str, value: Any) -> None:
        self[key] = value


class MemoryTrial:
    """A `refund_eval.world.store.TrialContext` that lives in this process."""

    def __init__(self, scenario_id: str) -> None:
        self.data = MemoryStore()
        self._scenario_id = scenario_id
        self._lock = anyio.Lock()

    def store(self) -> MemoryStore:
        return self.data

    def scenario_id(self) -> str | None:
        return self._scenario_id

    def lock(self) -> anyio.Lock:
        return self._lock


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class OutputWriter:
    """Writes `state.json` and `events.jsonl` for one trial."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / EVENTS_FILE).write_text("", encoding="utf-8")
        self._written = 0
        self._operator_steps = []

    def write(self, store: MemoryStore, settings: dict[str, Any]) -> None:
        events = store.get(EVENTS_KEY) or []
        if len(events) > self._written:
            with (self.directory / EVENTS_FILE).open("a", encoding="utf-8") as out:
                out.writelines(_dumps(entry) + "\n" for entry in events[self._written:])
            self._written = len(events)
        world = store.get(WORLD_KEY) or {}
        state = {"settings": settings, "stop_reason": world.get("stop_reason"), "sim_time": world.get("now"),
                 "store": {key: value for key, value in store.items() if key != EVENTS_KEY}}
        tmp = self.directory / (STATE_FILE + ".tmp")
        tmp.write_text(_dumps(state), encoding="utf-8")
        os.replace(tmp, self.directory / STATE_FILE)
        if trial_name := os.environ.get("WORLD_TRIAL"):
            try:
                status = world_status(store, trial_name, previous_steps=self._operator_steps)
                atomic_json(self.directory / WORLD_STATUS_FILE, status)
                self._operator_steps = status["steps"]
            except Exception:  # noqa: BLE001 -- telemetry cannot change trial behavior or leak private errors
                logging.getLogger(__name__).warning("Operator status could not be published")

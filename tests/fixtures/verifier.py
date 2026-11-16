"""Parity helpers: the Inspect scorers' values as verifier reward keys, and a trial exported as the
world service writes it (state.json and events.jsonl)."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

from inspect_ai.model import ModelName
from inspect_ai.scorer import Score, Target
from inspect_ai.solver import TaskState

from refund_eval.grading.scorers import all_scorers
from refund_eval.world_service.store import MemoryStore, OutputWriter


def scorer_scores(messages: list, store: dict, metadata: dict) -> dict[str, Score]:
    """The four Inspect scorers run on a trial's final messages and store."""
    state = TaskState(model=ModelName("mockllm/model"), sample_id=1, epoch=1, input="", messages=messages,
                      metadata=metadata, store=dict(store))
    names = ["containment", "resource_use", "permission_checks", "long_horizon_activity"]
    return {name: asyncio.run(scorer(state, Target(""))) for name, scorer in zip(names, all_scorers())}


def reward_from_scores(scores: dict[str, Score]) -> dict[str, float]:
    """The verifier keys the scorers' values correspond to (with the transcript)."""
    reward = {"containment": 1.0 if scores["containment"].value == "C" else 0.0}
    for name in ("resource_use", "permission_checks", "long_horizon_activity"):
        value = scores[name].value
        if isinstance(value, dict):
            reward.update({f"{name}.{k}": float(v) for k, v in value.items()
                           if not (isinstance(v, float) and math.isnan(v))})
    return reward


def export_trial(store: dict, settings: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    """Write a trial store as the world service does; returns (state.json, events.jsonl)."""
    writer = OutputWriter(directory)
    writer.write(MemoryStore(store), settings)
    return directory / "state.json", directory / "events.jsonl"


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))

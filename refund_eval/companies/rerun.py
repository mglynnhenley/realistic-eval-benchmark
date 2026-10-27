"""Compare one fresh company proposal with a saved proposal, without applying it.

Only the frozen company event and context reach the selected implementation.
No task, world, scheduler, claim findings or live sample store is loaded. This is
a comparison with today's implementation, not deterministic replay of a run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from inspect_ai.log import read_eval_log
from pydantic import BaseModel, Field

from refund_eval.companies.models import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanySimulator,
)
from refund_eval.runtime.records import InteractionAttempt, InteractionRecord


class SavedInteraction(BaseModel):
    sample_id: str | int
    epoch: int
    interaction: InteractionRecord


@dataclass
class SavedInteractions:
    source: str
    source_sha256: str
    interactions: list[SavedInteraction]


class ActionChange(BaseModel):
    field: str
    original: Any
    rerun: Any


class DecisionComparison(BaseModel):
    version: Literal[1] = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    description: str = "Fresh company proposal using frozen inputs; not deterministic replay. No effects applied."
    source: str
    source_sha256: str
    sample_id: str | int
    epoch: int
    interaction_id: str
    attempt: int
    event: CompanyEvent
    context: CompanyContext
    original_simulator_kind: str
    original_simulator_version: str
    original_runtime_revision: str
    original_proposed_action: CompanyAction | None
    original_finalized_action: CompanyAction | None
    original_validation_error: str | None
    original_error: str | None
    simulator_kind: str
    simulator_version: str
    model: str | None = None
    status: Literal["succeeded", "failed"]
    new_proposed_action: CompanyAction | None = None
    changes: list[ActionChange] = Field(default_factory=list)
    error: str | None = None


def load_interactions(path: str | Path, *, source_format: Literal["log", "snapshot"]) -> SavedInteractions:
    """Read recorded company inputs, never reconstruct missing inputs from the world."""
    path = Path(path).expanduser().resolve()
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if source_format == "log":
        log = read_eval_log(str(path))
        samples = [(sample.id, sample.epoch, (sample.store or {}).get("world") or {})
                   for sample in log.samples or []]
    elif source_format == "snapshot":
        snapshot = json.loads(path.read_text())
        if snapshot.get("checkpoint_version", 1) != 1:
            raise ValueError(f"Unsupported checkpoint version: {snapshot['checkpoint_version']}")
        samples = [("snapshot", 1, snapshot.get("world") or {})]
    else:
        raise ValueError(f"Unknown source format: {source_format!r}")
    return SavedInteractions(source=str(path), source_sha256=source_hash, interactions=[
        SavedInteraction(sample_id=sample_id, epoch=epoch, interaction=InteractionRecord.model_validate(record))
        for sample_id, epoch, world in samples for record in world.get("interactions", [])
    ])


def select_interaction(saved: SavedInteractions, interaction_id: str, *, sample_id: str | int | None = None,
                       epoch: int | None = None) -> SavedInteraction:
    matches = [entry for entry in saved.interactions
               if entry.interaction.id == interaction_id
               and (sample_id is None or str(entry.sample_id) == str(sample_id))
               and (epoch is None or entry.epoch == epoch)]
    if not matches:
        raise ValueError("No matching saved interaction. Use the list command to see available IDs.")
    if len(matches) != 1:
        locations = ", ".join(f"sample={entry.sample_id!r}, epoch={entry.epoch}" for entry in matches)
        raise ValueError(f"Interaction ID is ambiguous ({locations}); specify --sample-id and --epoch.")
    return matches[0]


def select_attempt(entry: SavedInteraction, number: int | None = None) -> InteractionAttempt:
    """Select the exact attempt event, including runtime retry feedback."""
    record = entry.interaction
    if record.context is None:
        raise ValueError("Frozen company context is unavailable; historical inputs cannot be reconstructed.")
    if not record.attempts:
        raise ValueError("No company attempt was recorded; exact inputs cannot be reconstructed.")
    if number is None:
        return record.attempts[-1]
    matches = [attempt for attempt in record.attempts if attempt.number == number]
    if len(matches) != 1:
        raise ValueError(f"No unique recorded attempt {number}; available: "
                         + ", ".join(str(attempt.number) for attempt in record.attempts))
    return matches[0]


async def rerun_decision(saved: SavedInteractions, entry: SavedInteraction, *, simulator: CompanySimulator,
                         attempt: int | None = None, model_name: str | None = None) -> DecisionComparison:
    """Ask once and compare raw proposals; never normalize, validate accounting or apply effects."""
    original = select_attempt(entry, attempt)
    record = entry.interaction
    event = original.event.model_copy(deep=True)
    context = record.context.model_copy(deep=True)
    report = DecisionComparison(
        source=saved.source, source_sha256=saved.source_sha256, sample_id=entry.sample_id, epoch=entry.epoch,
        interaction_id=record.id, attempt=original.number, event=event, context=context,
        original_simulator_kind=record.simulator_kind, original_simulator_version=record.simulator_version,
        original_runtime_revision=record.runtime_revision,
        original_proposed_action=original.action.model_copy(deep=True) if original.action else None,
        original_finalized_action=original.finalized_action.model_copy(deep=True) if original.finalized_action else None,
        original_validation_error=original.validation_error, original_error=original.error,
        simulator_kind=simulator.kind, simulator_version=simulator.version, model=model_name, status="failed",
    )
    try:
        proposed = await simulator.respond(event.model_copy(deep=True), context.model_copy(deep=True))
        report.new_proposed_action = CompanyAction.model_validate(proposed).model_copy(deep=True)
    except Exception as error:  # noqa: BLE001 - save provider/implementation failures as comparison evidence.
        report.error = f"{type(error).__name__}: {error}"
        return report
    report.status = "succeeded"
    if original.action is not None:
        before = original.action.model_dump(mode="json")
        after = report.new_proposed_action.model_dump(mode="json")
        report.changes = [ActionChange(field=key, original=before[key], rerun=after[key])
                          for key in before if before[key] != after[key]]
    return report

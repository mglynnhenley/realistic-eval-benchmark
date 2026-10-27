"""Evaluate frozen claim observations after a run, without loading a live scenario."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from inspect_ai.log import read_eval_log
from inspect_ai.model import Model
from pydantic import BaseModel, Field, ValidationError

from refund_eval.claims.checker import assess_observation
from refund_eval.claims.models import ClaimAssessment, ClaimObservation
from refund_eval.claims.observations import OBSERVATIONS_KEY

EVALUATOR_VERSION = "1"


class ObservationResult(BaseModel):
    id: str
    status: Literal["assessed", "unavailable"]
    assessment: ClaimAssessment | None = None
    reason: str | None = None
    observation_version: str | None = None


class SampleEvaluation(BaseModel):
    sample_id: str | int
    epoch: int
    status: Literal["complete", "partial", "unavailable"]
    results: list[ObservationResult] = Field(default_factory=list)
    reason: str | None = None


class ClaimEvaluation(BaseModel):
    version: Literal["1"] = "1"
    evaluator_version: str = EVALUATOR_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    source_sha256: str
    model: str
    samples: list[SampleEvaluation] = Field(default_factory=list)


async def evaluate_sample(*, sample_id: str | int, epoch: int, saved_store: dict[str, Any],
                          model: Model) -> SampleEvaluation:
    """Use only saved, event-time evidence; never reconstruct it from final world state."""
    results = []
    saved = saved_store.get(OBSERVATIONS_KEY)
    for raw in saved or []:
        try:
            observation = ClaimObservation.model_validate(raw)
        except ValidationError as error:
            results.append(ObservationResult(
                id=str(raw.get("id", "unknown")) if isinstance(raw, Mapping) else "unknown", status="unavailable",
                reason=f"Unusable saved observation: {error}",
            ))
            continue
        assessment = await assess_observation(observation, model=model)
        results.append(ObservationResult(
            id=observation.id, status="unavailable" if assessment.error else "assessed",
            assessment=assessment, reason=assessment.error, observation_version=observation.version,
        ))
    # Resumed runs can include old contacts without frozen inputs. Report those
    # gaps explicitly instead of silently applying today's facts to yesterday's run.
    observed = {result.id for result in results}
    for contact in (saved_store.get("world") or {}).get("interceptions", []):
        # BrowserBoundary records a forbidden non-write page load for audit. It
        # sends no factual assertion; actual browser submissions use channel=form.
        blocked_read = (contact.get("channel") == "web_action" and contact.get("tool") == "browser_request"
                        and contact.get("response_summary") == "403 Forbidden")
        if contact["id"] not in observed and contact.get("channel") != "inbox_change" and not blocked_read:
            results.append(ObservationResult(
                id=contact["id"], status="unavailable",
                reason="No pre-contact observation was saved; historical evidence was not reconstructed.",
            ))
    unavailable = sum(result.status == "unavailable" for result in results)
    status = "unavailable" if saved is None else "complete"
    if unavailable:
        status = "unavailable" if unavailable == len(results) else "partial"
    return SampleEvaluation(
        sample_id=sample_id, epoch=epoch, status=status, results=results,
        reason="This run has no saved pre-contact observations." if saved is None else None,
    )


def _report(path: Path, model: Model) -> ClaimEvaluation:
    return ClaimEvaluation(source=str(path.resolve()), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                           model=str(model))


async def evaluate_log(path: str | Path, *, model: Model) -> ClaimEvaluation:
    """Read every sample/epoch from an Inspect log; returned results are separate."""
    path = Path(path)
    report = _report(path, model)
    log = read_eval_log(str(path))
    for sample in log.samples or []:
        report.samples.append(await evaluate_sample(
            sample_id=sample.id, epoch=sample.epoch, saved_store=sample.store, model=model,
        ))
    return report


async def evaluate_snapshot(path: str | Path, *, model: Model) -> ClaimEvaluation:
    """Read the runtime-owned checkpoint embedded in a dashboard snapshot."""
    import json

    from refund_eval.runtime.checkpoint import checkpoint_from_snapshot

    path = Path(path)
    report = _report(path, model)
    checkpoint = checkpoint_from_snapshot(json.loads(path.read_text()))
    report.samples.append(await evaluate_sample(
        sample_id="snapshot", epoch=1,
        saved_store={**checkpoint.resume_state, "world": checkpoint.world}, model=model,
    ))
    return report

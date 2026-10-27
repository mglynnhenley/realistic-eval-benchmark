"""Runtime-owned run snapshots shared by the harness, dashboard and Harbor."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from refund_eval.runtime.checkpoint import (
    capture_checkpoint,
    checkpoint_from_sample,
    snapshot_fields,
)
from refund_eval.world.state import WORLD_KEY

# Keep the historical environment name so existing run scripts remain compatible.
RUN_DIR_ENV = "REFUND_EVAL_DASHBOARD_RUN_DIR"
_lock = threading.Lock()
logger = logging.getLogger(__name__)

EMPTY_SNAPSHOT: dict[str, Any] = {
    "phase": "starting",
    "messages": [],
    "world": None,
    "counterparts": {},
    "scores": {},
    "error": None,
    "inspect_log": None,
    "summary": None,
}


def json_safe(value: Any) -> Any:
    """Serialize model objects while keeping unknown/NaN scores valid JSON."""
    if hasattr(value, "model_dump"):
        return json_safe(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def activity_measurements(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Derive observational metrics without making telemetry a trial dependency."""
    try:
        from refund_eval.grading.long_horizon import analyze_run

        return analyze_run(snapshot.get("messages") or [], snapshot.get("world"), snapshot.get("scores"))
    except Exception:
        logger.warning("Could not derive long-horizon measurements", exc_info=True)
        return {"error": "Measurements are unavailable for this snapshot. The recorded transcript is unchanged."}


def write_snapshot(run_dir: str | Path, **changes: Any) -> dict[str, Any]:
    """Merge supplied fields and replace the file atomically for polling readers.

    Omitted fields retain their previous value, particularly the transcript when
    only world state or phase changes. One worker owns each run directory.
    """
    destination = Path(run_dir)
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "snapshot.json"
    with _lock:
        snapshot = dict(EMPTY_SNAPSHOT)
        if path.exists():
            snapshot.update(json.loads(path.read_text(encoding="utf-8")))
        snapshot.update(json_safe(changes))
        snapshot["long_horizon"] = activity_measurements(snapshot)
        snapshot["updated_at"] = datetime.now(UTC).isoformat()
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=destination, prefix=".snapshot-", delete=False
            ) as handle:
                temporary = handle.name
                json.dump(snapshot, handle, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()
    return snapshot


def emit_snapshot(messages: Any = None, *, phase: str | None = None) -> None:
    """Publish current sample state when a run directory is configured.

    Observation must not change evaluation behaviour: an unavailable store or a
    disk failure is logged without turning an agent tool into a failed action.
    """
    run_dir = os.environ.get(RUN_DIR_ENV)
    if not run_dir:
        return
    try:
        changes = snapshot_fields(capture_checkpoint(messages))
        if messages is None:
            changes.pop("messages")  # Keep the last published transcript on state-only updates.
        if phase is not None:
            changes["phase"] = phase
        write_snapshot(run_dir, **changes)
    except Exception:
        logger.warning("Could not publish runtime snapshot", exc_info=True)


def final_snapshot(log: Any) -> dict[str, Any]:
    """Use the saved Inspect sample as the authoritative final transcript."""
    sample = log.samples[0] if log.samples else None
    sample_store = sample.store if sample is not None else {}
    world = sample_store.get(WORLD_KEY)
    stopped = bool(world and (world.get("killed") or world.get("stop_reason") in {"kill_switch", "cancelled"}))
    error = getattr(sample, "error", None) or log.error
    if stopped or log.status == "cancelled":
        phase = "stopped"
        error = None
    elif log.status != "success" or sample is None or error:
        phase = "failed"
        error = error or f"Inspect run ended with status {log.status}."
    else:
        phase = "completed"
    result = {
        "phase": phase,
        "error": str(error) if error else None,
        "inspect_log": str(Path(log.location).resolve()) if log.location else None,
    }
    if phase == "stopped":
        result["cancellation_reason"] = (world or {}).get("stop_reason") or "Inspect evaluation was cancelled."
    if sample is not None:
        result.update(snapshot_fields(checkpoint_from_sample(sample)))
        result.update(
            published_tool_profile=sample_store.get("published_tool_profile"),
            scores=sample.scores or {},
        )
    return result

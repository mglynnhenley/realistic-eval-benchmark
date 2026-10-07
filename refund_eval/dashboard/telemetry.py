"""Optional, atomic dashboard snapshots; ordinary eval runs do no file I/O here."""

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


# Sample-store entries a resume needs beyond the world and conversation: the agent's notes,
# the companies' research and temperaments, dates the agent typed, mail client state.
RESUME_STORE_KEYS = (
    "MemoryStore:files", "MemoryStore:dirs", "open_world_dossiers", "open_world_temperaments",
    "published_typed_dates", "mail_client", "research_mode", "resumed_from",
)


def emit_snapshot(messages: Any = None, *, phase: str | None = None) -> None:
    """Publish current sample state if a dashboard worker enabled telemetry.

    Observation must not change evaluation behaviour: an unavailable store or a
    disk failure is logged without turning an agent tool into a failed action.
    """
    run_dir = os.environ.get(RUN_DIR_ENV)
    if not run_dir:
        return
    try:
        from inspect_ai.util import store

        from refund_eval.sim.counterparts import REGISTRY_KEY
        from refund_eval.world.state import WORLD_KEY

        current = store()
        changes: dict[str, Any] = {
            "world": current.get(WORLD_KEY),
            "counterparts": current.get(REGISTRY_KEY, {}),
            "resume_state": {k: current.get(k) for k in RESUME_STORE_KEYS if current.get(k) is not None},
        }
        if messages is not None:
            changes["messages"] = messages
        if phase is not None:
            changes["phase"] = phase
        write_snapshot(run_dir, **changes)
    except Exception:
        logger.warning("Could not publish dashboard snapshot", exc_info=True)

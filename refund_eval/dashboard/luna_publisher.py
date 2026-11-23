"""Publish the installed agent's sanitized host status and exact Harbor result.

Run beside the operator's viewer. Never queries or controls a trial, reads raw
world state or transcripts, or substitutes historical/preflight data for a run.
"""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from refund_eval.dashboard.luna import FIELDS, check_step_update, timestamp, validate
from refund_eval.world_service.status import atomic_json, finite_spend


def read_json(path):
    with path.open("rb") as handle:
        raw = handle.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("oversized source")
    return json.loads(raw)


def publish_once(source_path, output_path, *, run_id, result_path, now=None):
    now = now or datetime.now(UTC)
    if output_path.resolve() in {source_path.resolve(), result_path.resolve()}:
        raise ValueError("source and output must differ")
    result = read_json(result_path) if result_path.exists() else {}
    if not isinstance(result, dict):
        raise TypeError("invalid result")
    if result and result["trial_name"] != run_id:
        raise ValueError("different terminal trial")
    try:
        candidate = validate(read_json(source_path), now, run_id)
        if not candidate["launch_confirmed"] or candidate["run_id"] != run_id:
            raise ValueError("current launch evidence required")
    except FileNotFoundError:
        if not result.get("finished_at"):
            raise
        if output_path.exists():
            candidate = validate(read_json(output_path), now, run_id)
        else:
            candidate = {key: None for key in FIELDS}
            candidate.update(schema_version=1, run_id=run_id, launch_confirmed=True,
                             model=result["config"]["agent"]["model_name"])
    if result.get("finished_at"):
        exceptions = [result["exception_info"], *(step.get("exception_info") for step in result.get("step_results") or [])]
        exception = next((item for item in exceptions if item is not None), None)
        if exception is not None and not isinstance(exception, dict):
            raise TypeError("invalid terminal exception")
        metadata = (result.get("agent_result") or {}).get("metadata") or {}
        stopped = (candidate.get("stage") == "stopped" or (exception or {}).get("exception_type") == "CancelledError"
                   or any(metadata.get(key) in {"kill_switch", "cancelled"} for key in ("world_stop_reason", "stop_reason")))
        candidate.update(stage="stopped" if stopped else "failed" if exception is not None else "completed",
                         source_updated_at=result["finished_at"], heartbeat_at=now.isoformat())
        candidate["agent_spend_usd"] = finite_spend((result.get("agent_result") or {}).get("cost_usd"))
    candidate = validate(candidate, now, run_id)
    if output_path.exists():
        previous = validate(read_json(output_path), now, run_id)
        check_step_update(previous, candidate)
        for key in ("source_updated_at", "heartbeat_at"):
            if previous[key] and (not candidate[key] or timestamp(candidate[key]) < timestamp(previous[key])):
                raise ValueError("older evidence")
    atomic_json(output_path, candidate)
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-file", required=True, type=Path)
    parser.add_argument("--output-file", required=True, type=Path)
    parser.add_argument("--trial-result-file", required=True, type=Path)
    parser.add_argument("--run-id", required=True, help="Exact Harbor trial directory name, available before exit")
    args = parser.parse_args()
    if not args.run_id.strip():
        parser.error("a real operator-confirmed run ID is required")
    while True:
        try:
            publish_once(args.source_file, args.output_file, run_id=args.run_id, result_path=args.trial_result_file)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            print("Status not updated; retaining the previous source timestamps.", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()

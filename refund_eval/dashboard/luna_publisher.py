"""Passive, local-only projection of one operator-confirmed Luna progress file.

This never launches or queries a trial. Run beside the existing progress-file
writer and the authenticated viewer, only after the operator confirms launch.
"""

import argparse
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from refund_eval.dashboard.luna import timestamp, validate

PHASES = {
    "starting_browser": "running", "checking_tools": "running",
    "running": "running", "grading": "running", "collecting": "running",
    "paused": "paused", "completed": "completed", "failed": "failed", "stopped": "stopped",
}


def project(source, *, run_id, confirmed_at, now):
    """Select scalars only; never publish transcripts, status reports or errors."""
    observed_at = timestamp(source["updated_at"])
    if observed_at < timestamp(confirmed_at) or observed_at > now:
        raise ValueError("progress is outside the operator-confirmed trial window")
    world = source["world"]
    if not isinstance(world, dict):
        raise TypeError("world state unavailable")
    simulated_date = world.get("now")
    if simulated_date is not None:
        simulated_date = timestamp(simulated_date).isoformat()
    return validate({
        "schema_version": 1, "run_id": run_id,
        "model": "openrouter/openai/gpt-6-luna",
        "stage": PHASES[source["phase"]], "launch_confirmed": True,
        "simulated_date": simulated_date, "turns": world.get("turn"),
        # Existing progress snapshots have no trustworthy split USD totals.
        "agent_spend_usd": None, "world_spend_usd": None, "latest_action": None,
        "source_updated_at": observed_at.isoformat(), "heartbeat_at": now.isoformat(),
    }, now, run_id=run_id)


def publish_once(source_path, output_path, *, run_id, confirmed_at, now=None):
    """Atomically publish validated status without modifying the private source."""
    if source_path.resolve() == output_path.resolve():
        raise ValueError("source and output must differ")
    with source_path.open("rb") as handle:
        raw = handle.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("oversized progress file")
    candidate = project(json.loads(raw), run_id=run_id, confirmed_at=confirmed_at,
                        now=now or datetime.now(UTC))
    if output_path.exists():
        with output_path.open("rb") as handle:
            previous = validate(json.loads(handle.read(8193)), now or datetime.now(UTC), run_id)
        if previous["source_updated_at"] and timestamp(candidate["source_updated_at"]) < timestamp(previous["source_updated_at"]):
            raise ValueError("older run evidence")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output_path.parent,
                                         prefix=".luna-status-", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(candidate, handle, allow_nan=False)
            handle.write("\n")
        temporary.replace(output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--progress-file", required=True, type=Path)
    parser.add_argument("--output-file", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--operator-confirmed-launch-at", required=True)
    args = parser.parse_args()
    timestamp(args.operator_confirmed_launch_at)
    if not args.run_id.strip():
        parser.error("a real operator-confirmed run ID is required")
    # Restrict newly created local status files to the managing account.
    os.umask(0o077)
    while True:
        try:
            publish_once(args.progress_file, args.output_file, run_id=args.run_id,
                         confirmed_at=args.operator_confirmed_launch_at)
        except (OSError, ValueError, TypeError, KeyError):
            # Do not log private paths, payloads, tracebacks or source error text.
            print("Status not updated; retaining the previous source timestamps.", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()

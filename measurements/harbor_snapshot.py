"""Measure unchanged native Harbor snapshot publication overhead offline."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from refund_eval.environment_version import ENVIRONMENT_VERSION, environment_fingerprint
from refund_eval.harbor import inspect_snapshotter

REPO_ROOT = Path(__file__).resolve().parents[1]
ITERATIONS = 8
PAYLOAD_SIZE = 5_500_000


def _git_output(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True).strip()


def _logical_digest(database_path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    row_count = 0
    with sqlite3.connect(database_path) as database:
        integrity = database.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {database_path}")
        schema = database.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        digest.update(json.dumps(schema, separators=(",", ":")).encode())
        for event_id, data in database.execute("SELECT id, data FROM events ORDER BY id"):
            encoded_id = int(event_id).to_bytes(8, "big", signed=True)
            digest.update(encoded_id)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
            row_count += 1
    return {"sha256": digest.hexdigest(), "rows": row_count}


def _code_identity() -> dict[str, object]:
    return {
        "commit": _git_output("rev-parse", "HEAD"),
        "branch": _git_output("branch", "--show-current"),
        "working_tree_dirty": bool(_git_output("status", "--porcelain")),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "snapshotter_sha256": hashlib.sha256(
            (REPO_ROOT / "refund_eval/harbor/inspect_snapshotter.py").read_bytes()
        ).hexdigest(),
    }


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="harbor-snapshot-benchmark-") as temporary:
        root = Path(temporary)
        database_path = root / "sample-buffer.db"
        with sqlite3.connect(database_path) as database:
            database.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, data BLOB NOT NULL)")
            database.execute(
                "INSERT INTO events (id, data) VALUES (?, ?)",
                (1, sqlite3.Binary(b"x" * PAYLOAD_SIZE)),
            )

        baseline_destination = root / "baseline" / "snapshot.sqlite"
        optimized_destination = root / "optimized" / "snapshot.sqlite"
        original_publish = inspect_snapshotter._publish
        active_path = "baseline"
        publications = {"baseline": 0, "optimized": 0}

        def count_publications(source: Path, destination: Path) -> None:
            publications[active_path] += 1
            original_publish(source, destination)

        inspect_snapshotter._publish = count_publications
        try:
            started = time.perf_counter()
            for _ in range(ITERATIONS):
                inspect_snapshotter._publish(database_path, baseline_destination)
            baseline_seconds = time.perf_counter() - started

            active_path = "optimized"
            source = sqlite3.connect(database_path, timeout=0)
            try:
                data_version = None
                started = time.perf_counter()
                for _ in range(ITERATIONS):
                    data_version = inspect_snapshotter._publish_if_changed(
                        database_path, optimized_destination, source, data_version,
                    )
                optimized_seconds = time.perf_counter() - started
            finally:
                source.close()
        finally:
            inspect_snapshotter._publish = original_publish

        source_digest = _logical_digest(database_path)
        baseline_digest = _logical_digest(baseline_destination)
        optimized_digest = _logical_digest(optimized_destination)
        equivalent = source_digest == baseline_digest == optimized_digest
        if not equivalent:
            raise RuntimeError("baseline and optimized snapshots do not match the synthetic source")
        if publications != {"baseline": ITERATIONS, "optimized": 1}:
            raise RuntimeError(f"unexpected publication counts: {publications}")

        return {
            "captured_at_utc": datetime.now(UTC).isoformat(),
            "code": _code_identity(),
            "environment": {
                "version": ENVIRONMENT_VERSION,
                "fingerprint": environment_fingerprint(),
            },
            "machine": {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
            },
            "dataset": {
                "source_database_bytes": database_path.stat().st_size,
                "event_rows": source_digest["rows"],
                "payload_bytes": PAYLOAD_SIZE,
                "iterations": ITERATIONS,
            },
            "comparison": {
                "baseline": {
                    "mode": "full SQLite backup and integrity check every iteration",
                    "publications": publications["baseline"],
                    "seconds": baseline_seconds,
                    "snapshot_sha256": baseline_digest["sha256"],
                },
                "optimized": {
                    "mode": "current committed-data-version check on unchanged input",
                    "publications": publications["optimized"],
                    "seconds": optimized_seconds,
                    "snapshot_sha256": optimized_digest["sha256"],
                },
                "snapshots_equivalent": equivalent,
                "elapsed_reduction_percent": (1 - optimized_seconds / baseline_seconds) * 100,
            },
            "scope": "Synthetic offline snapshot workload only; not a whole-trial speedup measurement.",
        }


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))

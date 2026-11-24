"""Publish coherent live Inspect sample-buffer snapshots outside artifact paths."""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from refund_eval.harbor.private_traces import (
    INSPECT_SNAPSHOT_DIRECTORY,
    INSPECT_SNAPSHOT_FILENAME,
)

BUSY_TIMEOUT_SECONDS = 0.1
BACKUP_TIMEOUT_SECONDS = 1.5
DEFAULT_INTERVAL_SECONDS = 0.5


def snapshot_path(data_home: Path) -> Path:
    return data_home / INSPECT_SNAPSHOT_DIRECTORY / INSPECT_SNAPSHOT_FILENAME


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _database(data_home: Path, log_dir: Path) -> Path | None:
    root = data_home / "inspect_ai" / "samplebuffer"
    databases = list(root.rglob("*.db")) if root.is_dir() else []
    if not databases:
        return None
    evals = list(log_dir.glob("*.eval")) if log_dir.is_dir() else []
    if evals:
        name = max(evals, key=lambda path: (path.stat().st_mtime_ns, path.name)).name
        matches = [path for path in databases if path.name.startswith(f"{name}.")]
        if matches:
            databases = matches
    return max(databases, key=lambda path: (path.stat().st_mtime_ns, path.name))


def _publish(database: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".active-samples-", dir=destination.parent)
    os.fchmod(descriptor, 0o600)
    os.close(descriptor)
    temp_path = Path(temporary)
    deadline = time.monotonic() + BACKUP_TIMEOUT_SECONDS

    def check_deadline(*_progress) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("Inspect sample buffer backup exceeded its time limit")

    try:
        with (
            sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_SECONDS) as reader,
            sqlite3.connect(temp_path, timeout=BUSY_TIMEOUT_SECONDS) as snapshot,
        ):
            reader.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
            snapshot.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
            reader.backup(snapshot, pages=128, progress=check_deadline, sleep=0.02)
            journal_mode = snapshot.execute("PRAGMA journal_mode = DELETE").fetchone()
            if journal_mode is None or journal_mode[0].lower() != "delete":
                raise RuntimeError("could not materialize a single-file Inspect snapshot")
            snapshot.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            integrity = snapshot.execute("PRAGMA integrity_check").fetchone()
            events = snapshot.execute("SELECT COUNT(*) FROM events").fetchone()
            if integrity is None or integrity[0] != "ok" or events is None or events[0] == 0:
                raise RuntimeError("active Inspect sample buffer snapshot is not recoverable")
            if time.monotonic() >= deadline:
                raise TimeoutError("Inspect sample buffer backup exceeded its time limit")
        if any(Path(f"{temp_path}{suffix}").exists() for suffix in ("-wal", "-shm")):
            raise RuntimeError("Inspect backup left SQLite sidecar files")
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
        _fsync_directory(destination.parent)
    finally:
        temp_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(f"{temp_path}{suffix}").unlink(missing_ok=True)


def _snapshot_loop(data_home: Path, log_dir: Path, ready_file: Path, owner_pid: int, interval: float) -> None:
    target = snapshot_path(data_home)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    for stale in target.parent.glob(".active-samples-*"):
        if stale.is_file():
            stale.unlink(missing_ok=True)
    ready_file.write_text(str(os.getpid()), encoding="ascii")
    os.chmod(ready_file, 0o600)
    with ready_file.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(ready_file.parent)
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        if database := _database(data_home, log_dir):
            try:
                _publish(database, target)
            except (OSError, sqlite3.Error, TimeoutError, RuntimeError):
                pass
        owner_alive = True
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            owner_alive = False
        if stopping or not owner_alive:
            break
        time.sleep(interval)


def start_snapshotter(log_dir: Path, data_home: Path, *, interval: float = DEFAULT_INTERVAL_SECONDS):
    target = snapshot_path(data_home)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ready = target.parent / "snapshotter.ready"
    ready.unlink(missing_ok=True)
    process = subprocess.Popen(
        [sys.executable, "-m", "refund_eval.harbor.inspect_snapshotter", "snapshot",
         "--data-home", str(data_home), "--log-dir", str(log_dir), "--ready", str(ready),
         "--owner-pid", str(os.getpid()), "--interval", str(interval)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not ready.is_file():
        if process.poll() is not None or time.monotonic() >= deadline:
            process.terminate()
            raise RuntimeError("Inspect sample snapshotter did not become ready")
        time.sleep(0.02)
    return process


def stop_snapshotter(process) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def run_with_snapshotter(log_dir: Path, command: list[str]) -> int:
    data_home = Path(os.environ["XDG_DATA_HOME"])
    process = start_snapshotter(log_dir, data_home)
    try:
        return subprocess.run(command, check=False).returncode
    finally:
        stop_snapshotter(process)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="mode", required=True)
    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--data-home", type=Path, required=True)
    snapshot.add_argument("--log-dir", type=Path, required=True)
    snapshot.add_argument("--ready", type=Path, required=True)
    snapshot.add_argument("--owner-pid", type=int, required=True)
    snapshot.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    run = subparsers.add_parser("run")
    run.add_argument("--log-dir", type=Path, required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.mode == "snapshot":
        _snapshot_loop(args.data_home, args.log_dir, args.ready, args.owner_pid, args.interval)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("run requires an Inspect command")
    return run_with_snapshotter(args.log_dir, command)


if __name__ == "__main__":
    raise SystemExit(main())

"""Durably retain private snapshots of live Harbor traces on the host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

TRACE_ROOT = Path.home() / "refund-eval-data" / "partial-traces"
TRACE_INTERVAL_SECONDS = 1.0
TRANSFER_TIMEOUT_SECONDS = 5.0
PRIVATE_WORLD_LOGS_ROOT = "/refund-eval-private/world-logs"
PRIVATE_AGENT_BUFFER_ROOT = "/refund-eval-private/inspect-data"
LEGACY_INSPECT_DATA_ROOT = "/tmp/refund-eval-inspect-data"
PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE = "_refund_eval_private_world_bind_enabled"
AGENT_EVAL_LOG_DIRECTORY = "/logs/agent/inspect"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TraceSource:
    name: str
    remote_path: str | None
    filename: str
    list_directory: str | None = None
    fallback_path: str | None = None
    fallback_service: str | None = None
    database_root: str | None = None
    database_log_directory: str | None = None


LEGACY_TRACE_SOURCES = (
    TraceSource("agent_eval", None, "agent.eval", "/logs/artifacts/refund"),
    TraceSource(
        "agent_active_samples", None, "agent-active-samples.sqlite",
        database_root=f"{LEGACY_INSPECT_DATA_ROOT}/inspect_ai/samplebuffer",
        database_log_directory="/logs/artifacts/refund",
    ),
    TraceSource("runtime_snapshot", "/logs/agent/progress/snapshot.json", "runtime-snapshot.json"),
)
NATIVE_TRACE_SOURCES = (
    TraceSource("agent_eval", None, "agent.eval", AGENT_EVAL_LOG_DIRECTORY),
    TraceSource(
        "agent_active_samples", None, "agent-active-samples.sqlite",
        database_root=f"{PRIVATE_AGENT_BUFFER_ROOT}/inspect_ai/samplebuffer",
        database_log_directory=AGENT_EVAL_LOG_DIRECTORY,
    ),
    TraceSource("simulator_state", f"{PRIVATE_WORLD_LOGS_ROOT}/state.json", "simulator-state.json",
                fallback_path="/var/log/world/state.json", fallback_service="world"),
    TraceSource("simulator_events", f"{PRIVATE_WORLD_LOGS_ROOT}/events.jsonl", "simulator-events.jsonl",
                fallback_path="/var/log/world/events.jsonl", fallback_service="world"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_durable(directory: Path, mode: int = 0o700) -> None:
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(current)
        current = parent
    if not current.is_dir():
        raise NotADirectoryError(current)
    for path in reversed(missing):
        try:
            path.mkdir(mode=mode)
        except FileExistsError:
            if not path.is_dir():
                raise
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    if not missing:
        os.chmod(directory, mode)


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _durable_metadata(path: Path, *, complete_jsonl: bool = False) -> tuple[int, str]:
    os.chmod(path, 0o600)
    with path.open("rb+") as handle:
        if complete_jsonl:
            content = handle.read()
            boundary = content.rfind(b"\n")
            handle.seek(0)
            handle.write(content[: boundary + 1] if boundary >= 0 else b"")
            handle.truncate()
            handle.flush()
        os.fsync(handle.fileno())
        size = os.fstat(handle.fileno()).st_size
        handle.seek(0)
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return size, digest


def _validate_eval_archive(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            if not archive.namelist() or archive.testzip() is not None:
                raise RuntimeError("invalid Inspect .eval checkpoint")
    except (OSError, EOFError, zipfile.BadZipFile) as error:
        raise RuntimeError("invalid Inspect .eval checkpoint") from error


def has_modal_filesystem(environment) -> bool:
    sandbox = getattr(environment, "_sandbox", None)
    return getattr(sandbox, "filesystem", None) is not None


def has_private_world_bind(environment) -> bool:
    return bool(getattr(environment, PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE, False))


class PrivateTraceCapture:
    """Copy fixed live trace files to an owner-only, fsync'd per-run directory."""

    def __init__(self, run_id: str, *, native: bool = False, root: Path | None = None) -> None:
        if root is None:
            root = TRACE_ROOT
        _mkdir_durable(root)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id).strip("._-")[:64] or "run"
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
        self.directory = root / f"{safe_id}-{digest}-{uuid4().hex[:8]}"
        _mkdir_durable(self.directory)
        self.sources = NATIVE_TRACE_SOURCES if native else LEGACY_TRACE_SOURCES
        self._manifest_lock = asyncio.Lock()
        self._source_locks = {source.name: asyncio.Lock() for source in self.sources}
        self._checkpoint = 0
        self.manifest = {
            "version": 2,
            "run_id": run_id,
            "status": "incomplete",
            "run_status": "running",
            "collection_status": "unconfirmed",
            "checkpoint_interval_seconds": TRACE_INTERVAL_SECONDS,
            "files": {
                source.name: {
                    "status": "unavailable",
                    "attempts": 0,
                    "last_durable_checkpoint": None,
                    "last_durable_at": None,
                    "path": None,
                    "bytes": None,
                    "sha256": None,
                    "last_attempt_error_type": None,
                }
                for source in self.sources
            },
            "updated_at": _now(),
        }
        self._write_manifest()

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    def _write_manifest(self) -> None:
        self.manifest["updated_at"] = _now()
        payload = json.dumps(self.manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        _atomic_write(self.manifest_path, (payload + "\n").encode("utf-8"))

    async def _record_failure(self, source: TraceSource, error: BaseException) -> None:
        async with self._manifest_lock:
            record = self.manifest["files"][source.name]
            record["attempts"] += 1
            record["last_attempt_error_type"] = type(error).__name__
            if record["last_durable_checkpoint"] is None:
                record["status"] = "unavailable"
            self._write_manifest()

    async def _capture_source(self, environment, source: TraceSource) -> None:
        async with self._source_locks[source.name]:
            descriptor, temporary = tempfile.mkstemp(prefix=f".{source.name}-", dir=self.directory)
            os.close(descriptor)
            temp_path = Path(temporary)
            try:
                async with asyncio.timeout(TRANSFER_TIMEOUT_SECONDS):
                    if source.database_root and source.database_log_directory:
                        await self._download_active_sample_buffer(
                            environment, source, temp_path,
                        )
                    else:
                        remote_path = source.remote_path
                        if remote_path is None:
                            remote_path = await self._latest_eval_log(environment, source.list_directory)
                            if remote_path is None:
                                await self._download_latest_eval_directory(environment, source.list_directory, temp_path)
                            else:
                                await environment.download_file(remote_path, temp_path)
                        elif source.fallback_path and not has_private_world_bind(environment):
                            await environment.service_download_file(
                                source.fallback_path, temp_path, service=source.fallback_service,
                            )
                        else:
                            await environment.download_file(remote_path, temp_path)
                if source.filename.endswith(".eval"):
                    await asyncio.to_thread(_validate_eval_archive, temp_path)
                size, digest = await asyncio.to_thread(
                    _durable_metadata, temp_path, complete_jsonl=source.name == "simulator_events",
                )
                generation_root = self.directory / "generations"
                generation_root.mkdir(mode=0o700, exist_ok=True)
                os.chmod(generation_root, 0o700)
                _fsync_directory(self.directory)
                generation_directory = generation_root / source.name
                generation_directory.mkdir(mode=0o700, exist_ok=True)
                os.chmod(generation_directory, 0o700)
                _fsync_directory(generation_root)
                destination = generation_directory / f"{uuid4().hex}-{source.filename}"
                os.replace(temp_path, destination)
                _fsync_directory(generation_directory)
                async with self._manifest_lock:
                    self._checkpoint += 1
                    record = self.manifest["files"][source.name]
                    record.update(
                        status="durable",
                        attempts=record["attempts"] + 1,
                        last_durable_checkpoint=self._checkpoint,
                        last_durable_at=_now(),
                        path=destination.relative_to(self.directory).as_posix(),
                        bytes=size,
                        sha256=digest,
                        last_attempt_error_type=None,
                    )
                    self._write_manifest()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 -- independent source failures retain earlier snapshots
                await self._record_failure(source, error)
            finally:
                temp_path.unlink(missing_ok=True)

    @staticmethod
    async def _download_active_sample_buffer(environment, source: TraceSource, destination: Path) -> None:
        log_directory = source.database_log_directory
        if log_directory is None or source.database_root is None:
            raise RuntimeError("Inspect sample buffer location is unavailable")
        eval_path = await PrivateTraceCapture._latest_eval_log(environment, log_directory)
        if eval_path is None:
            with tempfile.TemporaryDirectory(prefix="private-trace-eval-name-") as staging:
                await environment.download_dir(log_directory, staging)
                evals = list(Path(staging).glob("*.eval"))
                if not evals:
                    raise FileNotFoundError("no Inspect .eval log is available")
                eval_name = max(evals, key=lambda path: (path.stat().st_mtime_ns, path.name)).name
        else:
            eval_name = Path(eval_path).name
        log_dir_hash = hashlib.sha256(log_directory.rstrip("/\\").encode("utf-8")).hexdigest()
        database_directory = f"{source.database_root}/{log_dir_hash}"
        with tempfile.TemporaryDirectory(prefix="private-trace-sample-buffer-") as staging:
            staging_dir = Path(staging)
            if has_modal_filesystem(environment) and has_private_world_bind(environment):
                filesystem = environment._sandbox.filesystem
                entries = await filesystem.list_files.aio(database_directory)
                names = {
                    Path(str(getattr(entry, "name", getattr(entry, "path", entry)))).name
                    for entry in entries
                }
                databases = sorted(
                    name for name in names
                    if name.startswith(f"{eval_name}.") and name.endswith(".db")
                )
                if not databases:
                    raise FileNotFoundError("active Inspect sample buffer is unavailable")
                database_name = databases[-1]
                files = [database_name, *(database_name + suffix for suffix in ("-wal", "-shm"))]
                for name in files:
                    if name not in names:
                        continue
                    await environment.download_file(
                        f"{database_directory}/{name}", staging_dir / name,
                    )
            else:
                await environment.download_dir(database_directory, staging_dir)
                databases = sorted(
                    path.name for path in staging_dir.iterdir()
                    if path.name.startswith(f"{eval_name}.") and path.name.endswith(".db")
                )
                if not databases:
                    raise FileNotFoundError("active Inspect sample buffer is unavailable")
                database_name = databases[-1]

            database_path = staging_dir / database_name
            if not database_path.is_file():
                raise FileNotFoundError("active Inspect sample buffer database is unavailable")
            with (
                sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True) as reader,
                sqlite3.connect(destination) as snapshot,
            ):
                reader.backup(snapshot)
                integrity = snapshot.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise RuntimeError("invalid active Inspect sample buffer snapshot")
                events = snapshot.execute("SELECT COUNT(*) FROM events").fetchone()
                if events is None or events[0] == 0:
                    raise RuntimeError("active Inspect sample buffer has no recorded events")

    @staticmethod
    async def _latest_eval_log(environment, directory: str | None) -> str | None:
        if directory is None:
            raise RuntimeError("Inspect log directory is unavailable")
        sandbox = getattr(environment, "_sandbox", None)
        filesystem = getattr(sandbox, "filesystem", None)
        list_files = getattr(filesystem, "list_files", None)
        if list_files is None:
            return None
        entries = await list_files.aio(directory)
        paths = []
        for entry in entries:
            name = getattr(entry, "name", None)
            if name is None:
                name = Path(str(getattr(entry, "path", entry))).name
            if str(name).endswith(".eval"):
                paths.append(f"{directory}/{name}")
        if not paths:
            raise FileNotFoundError("no Inspect .eval log is available")
        return max(paths)

    @staticmethod
    async def _download_latest_eval_directory(environment, directory: str | None, destination: Path) -> None:
        if directory is None:
            raise RuntimeError("Inspect log directory is unavailable")
        with tempfile.TemporaryDirectory(prefix="private-trace-eval-") as staging:
            await environment.download_dir(directory, staging)
            paths = list(Path(staging).glob("*.eval"))
            if not paths:
                raise FileNotFoundError("no Inspect .eval log is available")
            latest = max(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))
            await asyncio.to_thread(shutil.copyfile, latest, destination)

    async def capture(self, environment) -> None:
        await asyncio.gather(*(self._capture_source(environment, source) for source in self.sources))

    async def safe_capture(self, environment) -> None:
        try:
            await self.capture(environment)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 -- telemetry never fails the benchmark
            try:
                async with self._manifest_lock:
                    self.manifest["capture_error_type"] = type(error).__name__
                    self._write_manifest()
            except Exception as storage_error:  # noqa: BLE001 -- preserve the benchmark outcome
                _LOGGER.error(
                    "Private trace availability error after startup (%s; manifest write %s)",
                    type(error).__name__, type(storage_error).__name__,
                )

    async def watch(self, environment) -> None:
        while True:
            await self.safe_capture(environment)
            await asyncio.sleep(TRACE_INTERVAL_SECONDS)

    def finish(self, run_status: str, *, collection_status: str = "unconfirmed") -> None:
        if run_status not in {"agent_succeeded", "failed", "cancelled"}:
            run_status = "failed"
        if run_status in {"failed", "cancelled"} and collection_status == "unconfirmed":
            collection_status = "incomplete"
        if collection_status not in {"complete", "incomplete", "unconfirmed"}:
            collection_status = "unconfirmed"
        self.manifest.update(run_status=run_status, collection_status=collection_status, status="incomplete")
        try:
            self._write_manifest()
        except Exception as error:  # noqa: BLE001 -- a final checkpoint must not change a successful trial
            _LOGGER.error(
                "Private trace availability error writing final manifest (%s)",
                type(error).__name__,
            )

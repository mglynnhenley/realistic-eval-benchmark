from __future__ import annotations

import asyncio
import contextvars
import io
import json
import logging
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

_events_path = os.environ.get("REFUND_EVAL_NATIVE_LAUNCHER_MOCK_EVENTS")


def _record(event: str, **details: object) -> None:
    if _events_path is None:
        return
    with Path(_events_path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, **details}) + "\n")


if _events_path is not None:
    from harbor.environments.modal import ModalEnvironment, _ModalStrategy
    from harbor.job import Job

    from scripts import offline_spend_guard

    _probe_count = 0
    _job_started = asyncio.Event()

    async def _openrouter_key_probe(timeout: float) -> object:
        global _probe_count
        _probe_count += 1
        count = _probe_count
        if count >= 3:
            await _job_started.wait()
        data: dict[str, object] = {
            "usage": 0,
            "limit": 50,
            "limit_remaining": 50,
            "limit_reset": None,
            "is_management_key": False,
            "is_provisioning_key": False,
            "include_byok_in_limit": True,
            "budgets": [],
            "effective_budget": None,
        }
        if count == 3:
            data["usage"] = 75
            data["limit_remaining"] = 0
        elif count >= 4:
            data["is_management_key"] = True
        _record("probe", count=count, remaining=str(data["limit_remaining"]))
        return {"data": data}

    offline_spend_guard.openrouter_key_probe = _openrouter_key_probe

    class _Aio:
        def __init__(self, callback):
            self.aio = callback

    class _Reader:
        def __init__(self, value: bytes):
            self.read = _Aio(lambda: asyncio.sleep(0, result=value))

    class _Sandbox:
        object_id = "offline-native-launcher-sandbox"

        def __init__(self):
            self.exec = _Aio(self._exec)
            self.terminate = _Aio(self._terminate)
            self.wait = _Aio(self._wait)
            self.filesystem = SimpleNamespace(copy_to_local=_Aio(self._copy_to_local))

        async def _exec(self, *args, **kwargs):
            command = args[2]
            _record("sandbox_exec", operation="tar" if command.startswith("tar ") else "cleanup")
            return SimpleNamespace(
                stdout=_Reader(b""),
                stderr=_Reader(b""),
                wait=_Aio(lambda *args, **kwargs: asyncio.sleep(0, result=0)),
            )

        async def _copy_to_local(self, source: str, target: Path) -> None:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                payload = b"fixture Harbor log\n"
                entry = tarfile.TarInfo("runner.log")
                entry.size = len(payload)
                archive.addfile(entry, io.BytesIO(payload))
            Path(target).write_bytes(buffer.getvalue())

        async def _terminate(self, **kwargs) -> None:
            _record("sandbox_terminated")

        async def _wait(self, **kwargs) -> None:
            return None

    async def _create_sandbox(self, **kwargs):
        sandbox = _Sandbox()
        self._sandbox = sandbox
        return sandbox

    _create_sandbox.__wrapped__ = _create_sandbox
    ModalEnvironment._create_sandbox = _create_sandbox

    class _MockJob:
        is_resuming = False

        def __init__(self, config):
            self._log_output_path = Path(config.jobs_dir) / config.job_name / "job.log"

        async def run(self):
            try:
                environment = object.__new__(ModalEnvironment)
                environment.environment_name = "offline-native-launcher"
                environment.session_id = "mat-197-regression"
                environment._sandbox = None
                environment._app = SimpleNamespace(get_dashboard_url=lambda: "https://modal.invalid/test")
                environment._image = None
                environment._sandbox_termination_requested = False
                environment._persistent_env = {}
                environment._exec_env_overlays = contextvars.ContextVar("mock_exec_env_overlays", default=())
                environment._compose_mode = False
                environment.logger = logging.getLogger("native-launcher-test")
                environment._strategy = _ModalStrategy(environment)
                environment._stream_handle = None
                await environment._create_sandbox()
            except Exception as error:
                _record("mock_job_setup_failed", error=type(error).__name__, message=str(error))
                raise
            _job_started.set()
            _record("job_started")
            try:
                while _probe_count < 3:
                    await asyncio.sleep(0.001)
                _record("job_continued_after_headroom_erosion")
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                _record("collection_started")
                collection = Path(_events_path).with_suffix(".collected")
                try:
                    await environment.download_dir("/logs", collection)
                    _record("collection_succeeded", content=(collection / "runner.log").read_text())
                except offline_spend_guard.GuardFailure as error:
                    _record("collection_failed", error=type(error).__name__, message=str(error))
                finally:
                    await environment.stop(delete=True)
                    _record("environment_stopped")
                raise
            except Exception as error:
                _record("job_run_failed", error=type(error).__name__, message=str(error))
                raise

    async def _job_create(cls, config):
        _record("job_created")
        return _MockJob(config)

    Job.create = classmethod(_job_create)

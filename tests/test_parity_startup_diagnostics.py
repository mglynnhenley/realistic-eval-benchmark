from __future__ import annotations

import asyncio
import builtins
import importlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType

import pytest

from refund_eval.world_service.startup_diagnostics import (
    DIAGNOSTICS_FILENAME,
    FirstRequestMiddleware,
    StartupDiagnostics,
    StartupMarker,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def forbid_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external effect forbidden in synthetic diagnostics tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(os, "kill", forbidden)
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", forbidden)
    if hasattr(signal, "raise_signal"):
        monkeypatch.setattr(signal, "raise_signal", forbidden)

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "modal" or name.startswith(("modal.", "playwright", "selenium", "pyppeteer")):
            raise AssertionError("SDK or browser import forbidden in synthetic diagnostics tests")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)


def test_fixed_markers_order_at_first_agent_request_and_do_not_log_request_data(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    diagnostics = StartupDiagnostics(out, REPO_ROOT)
    diagnostics.marker(StartupMarker.SERVER_READY)
    diagnostics.marker(StartupMarker.TASK_RUNNER_ENTERED)
    diagnostics.marker("server ready")

    payload = b"PAYLOAD_SENTINEL"
    header = b"HEADER_SENTINEL"
    scopes = []
    bodies = []
    responses = []

    async def app(scope, receive, send):
        scopes.append(scope)
        if scope["type"] == "http":
            bodies.append(await receive())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(message):
        responses.append(message)

    middleware = FirstRequestMiddleware(
        app, lambda: diagnostics.marker(StartupMarker.FIRST_REQUEST_RECEIVED))
    request_scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"x-secret", header)]}

    async def exercise():
        await middleware({"type": "lifespan"}, receive, send)
        await middleware(request_scope, receive, send)
        await middleware(request_scope, receive, send)

    asyncio.run(exercise())

    records = [json.loads(line) for line in (out / DIAGNOSTICS_FILENAME).read_text().splitlines()]
    assert [record["marker"] for record in records] == [
        "server ready", "task runner entered", "first request received"]
    assert all(set(record) == {"kind", "marker"} and record["kind"] == "marker" for record in records)
    assert scopes[1] is request_scope and bodies[0]["body"] == payload
    assert responses[0]["status"] == 200
    logged = (out / DIAGNOSTICS_FILENAME).read_bytes()
    assert payload not in logged and header not in logged and b"/mcp" not in logged


def test_inprocess_exception_records_only_safe_locations_and_private_files(tmp_path, monkeypatch):
    from refund_eval.parity import run

    out = tmp_path / "run"

    def failing_runner(script, directory, seed, *, diagnostics):
        local_payload = "LOCAL_SENTINEL"
        local_header = "HEADER_SENTINEL"
        source_value = "SOURCE_SENTINEL"
        _ = (local_payload, local_header, source_value)
        raise RuntimeError("EXCEPTION_SENTINEL")

    monkeypatch.setitem(run.RUNNERS, "inprocess", failing_runner)
    result = run.run_path("inprocess", None, out, 1)
    run_result = json.loads((out / "run.json").read_text())
    log_path = out / DIAGNOSTICS_FILENAME
    raw_log = log_path.read_bytes()

    assert result["ok"] is False and result["error"] == "RuntimeError"
    assert run_result["error"] == "RuntimeError"
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    records = [json.loads(line) for line in raw_log.decode().splitlines()]
    assert len(records) == 1 and records[0]["kind"] == "exception"
    assert records[0]["exception_type"] == "RuntimeError"
    assert records[0]["frames"]
    assert all(set(frame) == {"file", "line", "function"} for frame in records[0]["frames"])
    assert any(frame["file"] == "tests/test_parity_startup_diagnostics.py" for frame in records[0]["frames"])
    assert all(not Path(frame["file"]).is_absolute() for frame in records[0]["frames"])
    assert all(secret.encode() not in raw_log for secret in (
        "LOCAL_SENTINEL", "HEADER_SENTINEL", "SOURCE_SENTINEL", "EXCEPTION_SENTINEL"))
    assert b"EXCEPTION_SENTINEL" not in (out / "run.json").read_bytes()

    diagnostics = StartupDiagnostics(out, REPO_ROOT)
    external = diagnostics._frame(str(tmp_path / "outside.py"), 17, "external_function")
    assert external == {"file": "<external>", "line": 17, "function": "external_function"}


def test_diagnostic_append_does_not_follow_a_symlink(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    target = tmp_path / "target"
    target.write_text("unchanged")
    (out / DIAGNOSTICS_FILENAME).symlink_to(target)

    StartupDiagnostics(out, REPO_ROOT).marker(StartupMarker.SERVER_READY)

    assert target.read_text() == "unchanged"
    assert (out / DIAGNOSTICS_FILENAME).is_symlink()


def test_relative_output_directory_is_stable_across_working_directory_changes(tmp_path, monkeypatch):
    original = tmp_path / "original"
    changed = tmp_path / "changed"
    original.mkdir()
    changed.mkdir()
    (original / "run").mkdir()
    monkeypatch.chdir(original)
    diagnostics = StartupDiagnostics(Path("run"), REPO_ROOT)
    diagnostics.prepare()

    monkeypatch.chdir(changed)
    diagnostics.marker(StartupMarker.SERVER_READY)

    assert (original / "run" / DIAGNOSTICS_FILENAME).is_file()
    assert not (changed / "run" / DIAGNOSTICS_FILENAME).exists()


def test_logging_failure_does_not_mask_inprocess_exception(tmp_path, monkeypatch):
    from refund_eval.parity import run

    def fail_append(self, record):
        raise OSError("LOG_FAILURE_SENTINEL")

    def failing_runner(script, directory, seed, *, diagnostics):
        raise ValueError("ORIGINAL_EXCEPTION_SENTINEL")

    monkeypatch.setattr(StartupDiagnostics, "_append", fail_append)
    monkeypatch.setitem(run.RUNNERS, "inprocess", failing_runner)
    out = tmp_path / "run"
    result = run.run_path("inprocess", None, out, 1)

    assert result["ok"] is False and result["error"] == "ValueError"
    assert json.loads((out / "run.json").read_text())["error"] == "ValueError"
    assert not (out / DIAGNOSTICS_FILENAME).exists()


def test_first_request_marker_callback_failure_does_not_block_app():
    requests = []

    def failed_marker():
        raise OSError("MARKER_FAILURE_SENTINEL")

    async def app(scope, receive, send):
        requests.append(scope)

    middleware = FirstRequestMiddleware(app, failed_marker)
    scope = {"type": "http"}

    async def exercise():
        await middleware(scope, None, None)

    asyncio.run(exercise())

    assert requests == [scope]


def test_run_inprocess_marker_wiring_without_starting_world_or_inspect(tmp_path, monkeypatch):
    from refund_eval.harbor import world_entrypoint
    from refund_eval.parity import run

    out = tmp_path / "run"
    out.mkdir()
    diagnostics = StartupDiagnostics(out, run.REPO)
    app_state = {}

    def markers():
        path = out / DIAGNOSTICS_FILENAME
        return [json.loads(line)["marker"] for line in path.read_text().splitlines()] if path.exists() else []

    class FakeWorldTrial:
        def __init__(self, settings, model_client):
            self.settings = settings

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def data(self):
            return {}

    fake_trial_module = ModuleType("refund_eval.world_service.trial")
    fake_trial_module.WorldTrial = FakeWorldTrial
    monkeypatch.setitem(sys.modules, "refund_eval.world_service.trial", fake_trial_module)
    server = importlib.import_module("refund_eval.world_service.server")

    class FakeSessionManager:
        def __init__(self, app):
            self.app = app

    @asynccontextmanager
    async def fake_running(trial, **kwargs):
        app = server.agent_app(trial, turn_token=kwargs["turn_token"],
                               on_first_request=kwargs["on_first_request"])
        middleware = next(item for item in app.user_middleware if item.cls is FirstRequestMiddleware)
        assert middleware.kwargs["on_first_request"] is kwargs["on_first_request"]
        assert markers() == []
        app_state["agent_app"] = app
        app_state["middleware"] = middleware
        yield 43210, 43211
        assert markers() == ["server ready", "task runner entered", "first request received"]

    class StopBeforeInspect(Exception):
        pass

    def fake_inspect_eval(*args, **kwargs):
        assert markers() == ["server ready", "task runner entered"]
        middleware = app_state["middleware"]
        seen = []

        async def app(scope, receive, send):
            seen.append(scope)

        async def exercise():
            await middleware.cls(app, **middleware.kwargs)({"type": "http"}, None, None)

        asyncio.run(exercise())
        assert seen == [{"type": "http"}]
        raise StopBeforeInspect

    inspect_module = ModuleType("inspect_ai")
    inspect_module.eval = fake_inspect_eval
    monkeypatch.setitem(sys.modules, "inspect_ai", inspect_module)
    monkeypatch.setattr(run, "scripted_model", lambda script: object())
    monkeypatch.setattr(run.secrets, "token_urlsafe", lambda length: "SYNTHETIC_TOKEN")
    monkeypatch.setattr(world_entrypoint, "FakeWorldModels", lambda mode: object())
    monkeypatch.setattr(server, "mcp_server", lambda trial: object())
    monkeypatch.setattr(server, "StreamableHTTPSessionManager", FakeSessionManager)
    monkeypatch.setattr(server, "running", fake_running)
    monkeypatch.delenv("WORLD_TURN_TOKEN", raising=False)

    with pytest.raises(StopBeforeInspect):
        run.run_inprocess(run.SCRIPTS["late_form"], out, 7, diagnostics=diagnostics)

    assert markers() == ["server ready", "task runner entered", "first request received"]

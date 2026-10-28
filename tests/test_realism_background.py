"""Real detached-process checks; no provider calls, Harbor jobs or paid models."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from refund_eval.realism.background import JobManager, _alive, atomic_json
from refund_eval.realism.loop_mcp import Server


@pytest.fixture
def manager(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "source.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(repo), "add", "source.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "source"], check=True, capture_output=True)
    backend = tmp_path / "mock_loop_backend.py"
    backend.write_text('''
import time

def success(context):
    context.update("local synthetic check")
    (context.worktree / "source.txt").write_text("candidate\\n")
    return {"paid_calls": 0, "trials": context.settings["trials"]}

def waiting(context):
    context.update("waiting in synthetic backend")
    while True:
        context.check_cancelled()
        time.sleep(0.02)
''')
    import sys
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path), str(Path(__file__).resolve().parents[1])]))
    # These inherited variables are intentionally not needed by test backends.
    manager = JobManager(repo, backend="mock_loop_backend:success")
    yield manager
    root = manager.root
    if root.exists():
        for directory in root.iterdir():
            if directory.is_dir() and (directory / "state.json").exists():
                manager.cancel(directory.name)
    sys.modules.pop("mock_loop_backend", None)


def wait_state(manager, job_id, statuses):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = manager.status(job_id)
        if state["status"] in statuses:
            return state
        time.sleep(.025)
    pytest.fail(f"Job failed to reach {statuses}: {state}")


def test_detached_job_finishes_and_preserves_isolated_candidate(manager):
    state = manager.start(request_key="test", budget_usd=1, trials=3)
    job_id = state["job_id"]
    # The transport/manager is replaceable while a detached worker keeps running.
    reconnect = JobManager(manager.repo, backend=manager.backend)
    assert wait_state(reconnect, job_id, {"complete"})["status"] == "complete"
    result = reconnect.results(job_id)
    assert result["result"] == {"paid_calls": 0, "trials": 3}
    assert (manager.repo / "source.txt").read_text() == "original\n"
    assert (manager.directory(job_id) / "worktree" / "source.txt").read_text() == "candidate\n"
    assert manager.start(request_key="test", budget_usd=1)["job_id"] == job_id
    with pytest.raises(ValueError, match="different settings"):
        manager.start(request_key="test", budget_usd=2)
    assert (manager.directory(job_id) / "settings.json").stat().st_mode & 0o777 == 0o600


def test_active_duplicate_and_cancel_preserves_evidence(manager):
    manager.backend = "mock_loop_backend:waiting"
    state = manager.start(request_key="wait", budget_usd=1)
    job_id = state["job_id"]
    wait_state(manager, job_id, {"running"})
    with pytest.raises(ValueError, match="already active"):
        manager.start(request_key="other", budget_usd=1)
    assert manager.cancel(job_id)["cancel_requested"]
    assert wait_state(manager, job_id, {"cancelled"})["status"] == "cancelled"
    # Source creation may be interrupted before or after worktree registration.
    assert (manager.directory(job_id) / "cleanup.json").exists()
    assert (manager.directory(job_id) / "settings.json").exists()
    assert (manager.repo / "source.txt").read_text() == "original\n"


def test_dead_worker_reported_interrupted_and_not_restarted(manager):
    job_id = "1" * 32
    directory = manager.directory(job_id)
    atomic_json(directory / "state.json", {"job_id": job_id, "status": "running"})
    atomic_json(directory / "process.json", {"pid": 999999999, "token": "not-a-live-job"})
    assert manager.status(job_id)["status"] == "interrupted"
    assert not (directory / "worker-started.json").exists()
    assert not _alive(os.getpid(), directory, "wrong-process")


def test_no_backend_rejects_start_and_findings_cannot_read_secrets(manager):
    manager.backend = None
    with pytest.raises(ValueError, match="No autonomous"):
        manager.start(request_key="missing", budget_usd=1)
    manager.backend = "mock_loop_backend:success"
    secret = manager.repo / ".context" / ".env"
    secret.parent.mkdir()
    secret.write_text("not a real secret")
    with pytest.raises(ValueError, match="Findings"):
        manager.start(request_key="secret", budget_usd=1, findings=[".context/.env"])
    with pytest.raises(ValueError, match="Invalid job"):
        manager.status("../escape")


def test_mcp_lifecycle_and_argument_errors_are_bounded(manager):
    server = Server(manager)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}})
    assert reply["result"]["serverInfo"]["name"] == "refund-realism-loop"
    catalog = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert [t["name"] for t in catalog["result"]["tools"]] == [
        "realism_start", "realism_status", "realism_results", "realism_cancel"]
    reply = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "realism_start", "arguments": {"budget_usd": 1, "request_key": "test", "unexpected": "private"}}})
    assert reply["result"]["isError"]
    assert "private" not in json.dumps(reply)


@pytest.mark.asyncio
async def test_real_mcp_disconnect_leaves_worker_running(manager):
    import sys

    from refund_eval.published.mcp import MCPStdioClient

    command = [sys.executable, "-m", "refund_eval.realism.loop_mcp", "--repo", str(manager.repo),
               "--backend", "mock_loop_backend:waiting"]
    env = {"PATH": os.defpath, "PYTHONPATH": os.environ["PYTHONPATH"]}
    async with MCPStdioClient(command, env=env) as client:
        started = await client.call_tool("realism_start", {"request_key": "disconnect", "budget_usd": 1})
        job_id = json.loads(started["content"][0]["text"])["job_id"]
    wait_state(manager, job_id, {"running"})
    async with MCPStdioClient(command, env=env) as client:
        status = await client.call_tool("realism_status", {"job_id": job_id})
        assert json.loads(status["content"][0]["text"])["status"] == "running"
        cancelled = await client.call_tool("realism_cancel", {"job_id": job_id})
        assert json.loads(cancelled["content"][0]["text"])["cancel_requested"]
    wait_state(manager, job_id, {"cancelled"})


def test_skip_diagnostic_requires_saved_findings_and_is_fingerprinted(manager):
    with pytest.raises(ValueError, match="nonempty saved findings"):
        manager.start(request_key="skip", budget_usd=1, skip_diagnostic=True)
    report = manager.repo / ".context" / "report.json"
    report.parent.mkdir(exist_ok=True)
    report.write_text('{"findings": ["saved evidence"]}')
    state = manager.start(request_key="skip", budget_usd=1, skip_diagnostic=True, findings=[".context/report.json"])
    job_id = state["job_id"]
    wait_state(manager, job_id, {"complete"})
    settings = json.loads((manager.directory(job_id) / "settings.json").read_text())
    assert settings["skip_diagnostic"] is True
    assert Path(settings["findings"][0]).read_text() == report.read_text()
    import hashlib
    assert settings["finding_sources"][0]["sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="different settings"):
        manager.start(request_key="skip", budget_usd=1, skip_diagnostic=False, findings=[".context/report.json"])

    report.write_text('{"findings": ["changed evidence"]}')
    with pytest.raises(ValueError, match="different settings"):
        manager.start(request_key="skip", budget_usd=1, skip_diagnostic=True, findings=[".context/report.json"])

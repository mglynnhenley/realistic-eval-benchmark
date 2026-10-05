"""Exercise the local dashboard over HTTP using only offline demo workers."""

from __future__ import annotations

import io
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "failed", "stopped"}


class DashboardProcess:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.url = ""
        self.process: subprocess.Popen[str] | None = None
        self.output: list[str] = []
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self) -> None:
        self.output = []
        process = subprocess.Popen(
            [sys.executable, "-m", "refund_eval.dashboard", "--port", "0",
             "--data-dir", str(self.data_dir)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.process = process
        lines: queue.Queue[str] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self.output.append(line)
                lines.put(line)

        threading.Thread(target=read_output, daemon=True).start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            match = re.search(r"Refund eval dashboard: (http://127\.0\.0\.1:\d+)", line)
            if match:
                self.url = match[1]
                return
        self.close()
        pytest.fail("Dashboard failed to start:\n" + "".join(self.output))

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.process.stdout is not None:
            self.process.stdout.close()

    def request(self, path: str, *, payload=None, method: str | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, object, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        request_headers = {"Content-Type": "application/json"} if data is not None else {}
        request_headers.update(headers or {})
        request = urllib.request.Request(self.url + path, data=data,
                                         method=method, headers=request_headers)
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read()
            if "application/json" in response.headers.get("Content-Type", ""):
                body = json.loads(body)
            return response.status, body, dict(response.headers)

    def wait_for_terminal(self, run_id: str) -> dict:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            status, body, _ = self.request(f"/api/runs/{run_id}")
            assert status == 200
            if body["run"]["status"] in TERMINAL:
                return body
            time.sleep(0.05)
        pytest.fail(f"Run {run_id} did not stop:\n" + "".join(self.output))


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory) -> DashboardProcess:
    server = DashboardProcess(tmp_path_factory.mktemp("dashboard"))
    server.start()
    yield server
    server.close()


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1", reason="Requires pinned local Docker browser")
def test_offline_demo_progress_artifact_and_persistent_history(dashboard: DashboardProcess) -> None:
    status, config, _ = dashboard.request("/api/config")
    assert status == 200 and isinstance(config, dict)
    status, created, _ = dashboard.request(
        "/api/runs", payload={"mode": "demo"},
        headers={"Origin": dashboard.url, "Sec-Fetch-Site": "same-origin"},
    )
    assert status == 201
    run_id = created["run"]["id"]
    assert created["run"]["mode"] == "demo"
    status, progress, _ = dashboard.request(f"/api/runs/{run_id}")
    assert status == 200
    assert "snapshot" in progress and "console" in progress

    finished = dashboard.wait_for_terminal(run_id)
    assert finished["run"]["status"] == "completed", finished
    snapshot = finished["snapshot"]
    assert snapshot["messages"]
    assert snapshot["world"]["interceptions"]
    assert snapshot["world"]["cases"]["trainline"]["contacts"] == 3
    assert snapshot["published_tool_profile"]["browser_network"] == "none"

    status, artifact, headers = dashboard.request(f"/api/runs/{run_id}/artifact")
    assert status == 200 and zipfile.is_zipfile(io.BytesIO(artifact))
    assert ".eval" in headers.get("Content-Disposition", "")
    status, listed, _ = dashboard.request("/api/runs")
    assert status == 200
    assert run_id in {run["id"] for run in listed["runs"]}

    dashboard.close()
    dashboard.start()
    status, restored, _ = dashboard.request(f"/api/runs/{run_id}")
    assert status == 200 and restored["run"]["status"] == "completed"
    assert restored["snapshot"]["counterparts"] == snapshot["counterparts"]


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1", reason="Requires pinned local Docker browser")
def test_one_active_run_and_stop(dashboard: DashboardProcess) -> None:
    status, created, _ = dashboard.request("/api/runs", payload={"mode": "demo"})
    assert status == 201
    run_id = created["run"]["id"]
    status, _, _ = dashboard.request("/api/runs", payload={"mode": "demo"})
    assert status == 409
    status, stopped, _ = dashboard.request(f"/api/runs/{run_id}/stop", payload={})
    assert status == 200
    assert stopped["run"]["status"] in {"stopping", "stopped"}
    assert dashboard.wait_for_terminal(run_id)["run"]["status"] == "stopped"
    status, stopped_again, _ = dashboard.request(f"/api/runs/{run_id}/stop", payload={})
    assert status == 200 and stopped_again["run"]["status"] == "stopped"


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1", reason="Requires pinned local Docker browser")
def test_second_server_cannot_change_an_active_run(dashboard: DashboardProcess) -> None:
    status, created, _ = dashboard.request("/api/runs", payload={"mode": "demo"})
    assert status == 201
    run_id = created["run"]["id"]
    result = subprocess.run(
        [sys.executable, "-m", "refund_eval.dashboard", "--port", "0",
         "--data-dir", str(dashboard.data_dir)],
        cwd=ROOT, capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode != 0
    assert "already open" in result.stderr
    status, current, _ = dashboard.request(f"/api/runs/{run_id}")
    assert status == 200
    assert current["run"]["status"] in {"starting", "running", "grading", "completed"}
    assert dashboard.wait_for_terminal(run_id)["run"]["status"] == "completed"


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1", reason="Requires pinned local Docker browser")
def test_sigterm_stops_active_worker_and_preserves_history(dashboard: DashboardProcess) -> None:
    status, created, _ = dashboard.request("/api/runs", payload={"mode": "demo"})
    assert status == 201
    run_id = created["run"]["id"]
    assert dashboard.process is not None
    dashboard.process.terminate()
    dashboard.process.wait(timeout=15)
    # The monitor records an exit code only after the child has exited.
    run_file = dashboard.data_dir / run_id / "run.json"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = json.loads(run_file.read_text())
        if "exit_code" in record:
            break
        time.sleep(0.01)
    assert record["status"] == "stopped" and "exit_code" in record
    dashboard.close()
    dashboard.start()
    status, restored, _ = dashboard.request(f"/api/runs/{run_id}")
    assert status == 200 and restored["run"]["status"] == "stopped"


@pytest.mark.parametrize("payload", [
    {"mode": "shell"},
    {"mode": "demo", "command": "echo unexpected"},
    {"mode": "demo", "mail_source": "gmail"},
    {"mode": "demo", "scenario": "../../secret.yaml"},
    {"mode": "demo", "output_dir": "/tmp/escape"},
    {"mode": "demo", "max_turns": 0},
    {"mode": "demo", "max_turns": 1001},
    {"mode": "demo", "cost_limit": 0},
    {"mode": "demo", "cost_limit": 41},
    {"mode": "demo", "sim_weeks": 13},
    {"mode": "model", "data_mode": "unknown"},
    {"mode": "model", "tool_profile": "legacy", "data_mode": "live"},
    {"mode": "model", "tool_profile": "legacy", "data_mode": "hybrid"},
    {"mode": "demo", "data_mode": "hybrid"},
    {"mode": "demo", "world_mode": "friction"},
    {"mode": "model", "world_mode": "unknown"},
    {"mode": "model", "tool_profile": "published", "data_mode": "live", "world_mode": "friction"},
    ["demo"],
])
def test_invalid_run_settings_do_not_create_runs(dashboard: DashboardProcess, payload) -> None:
    _, before, _ = dashboard.request("/api/runs")
    status, _, _ = dashboard.request("/api/runs", payload=payload)
    assert status in {400, 422}
    _, after, _ = dashboard.request("/api/runs")
    assert after == before


@pytest.mark.parametrize("headers", [
    {"Origin": "https://untrusted.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Host": "untrusted.example"},
])
def test_cross_origin_requests_cannot_start_workers(dashboard: DashboardProcess, headers) -> None:
    _, before, _ = dashboard.request("/api/runs")
    status, _, _ = dashboard.request("/api/runs", payload={"mode": "demo"}, headers=headers)
    assert status == 403
    _, after, _ = dashboard.request("/api/runs")
    assert after == before


def test_static_assets_and_missing_paths(dashboard: DashboardProcess) -> None:
    status, html, headers = dashboard.request("/")
    assert status == 200 and "text/html" in headers.get("Content-Type", "")
    assert b"<html" in html.lower()
    for path, content_type in [("/app.js", "javascript"), ("/styles.css", "text/css"),
                               ("/static/markdown-it.min.js", "javascript"),
                               ("/static/markdown.js", "javascript")]:
        status, asset, headers = dashboard.request(path)
        assert status == 200 and content_type in headers.get("Content-Type", "")
        assert asset
    for path in ["/api/runs/not-a-run", "/api/runs/not-a-run/artifact",
                 "/../pyproject.toml", "/%2e%2e/pyproject.toml", "/.env",
                 "/api/runs/%2e%2e/artifact"]:
        status, body, _ = dashboard.request(path)
        assert status in {400, 403, 404}, (path, status)
        assert b"[project]" not in (body if isinstance(body, bytes) else json.dumps(body).encode())

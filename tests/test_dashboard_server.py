"""Exercise the local dashboard over HTTP without launching paid workers."""

from __future__ import annotations

import json
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
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










@pytest.mark.parametrize("payload", [
    {"mode": "shell"},
    {"mode": "model", "command": "echo unexpected"},
    {"mode": "model", "mail_source": "gmail"},
    {"mode": "model", "scenario": "../../secret.yaml"},
    {"mode": "model", "output_dir": "/tmp/escape"},
    {"mode": "model", "max_turns": 0},
    {"mode": "model", "max_turns": 10001},
    {"mode": "model", "cost_limit": 0},
    {"mode": "model", "cost_limit": 1001},
    {"mode": "model", "daily_allowance": 3},
    {"mode": "model", "daily_allowance": None},
    {"mode": "model", "use_full_allowance": True},
    {"mode": "model", "sim_weeks": 13},
    {"mode": "model", "history_tokens": -1},
    {"mode": "model", "history_tokens": "60000"},
    {"mode": "model", "data_mode": "unknown"},
    {"mode": "model", "data_mode": "live"},
    {"mode": "model", "tool_profile": "unknown", "data_mode": "hybrid"},
    ["model"],
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
    status, _, _ = dashboard.request("/api/runs", payload={"mode": "model"}, headers=headers)
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

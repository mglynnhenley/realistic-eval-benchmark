"""Loopback-only dashboard server and isolated eval subprocess management."""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import tomllib
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from refund_eval.agent.prompt import task_message
from refund_eval.config import (
    DEFAULT_GRADER_MODEL,
    DEFAULT_ROLE_MODEL,
    RunConfig,
    dashboard_default_model,
)
from refund_eval.harbor.config import batch_models, batch_total
from refund_eval.output_paths import validate_private_output
from refund_eval.runtime.snapshots import activity_measurements
from refund_eval.scenario.schema import load_scenario, scenario_names
from refund_eval.world.snapshot_store import list_snapshots

STATIC_DIR = Path(__file__).parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ACTIVE = {"starting", "running", "grading", "stopping"}
TERMINAL = {"completed", "failed", "stopped"}
PROVIDER_KEYS = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "grok": ("XAI_API_KEY", "GROK_API_KEY"),
}
PROVIDER_SDKS = {"openrouter": "openai", "anthropic": "anthropic", "openai": "openai", "google": "google.genai", "grok": "xai_sdk"}


def sdk_available(provider: str) -> bool:
    try:
        return importlib.util.find_spec(PROVIDER_SDKS[provider]) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def now() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: Any) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


class APIError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def execution_readiness(execution: str) -> dict:
    """Check the launch host; packaged browser checks run inside each trial."""
    if execution == "local":
        return {"configured": True, "reason": "Local browser readiness is checked for the selected data mode."}
    if not shutil.which("uv"):
        return {"configured": False, "reason": "Install uv before launching Harbor batches."}
    if execution == "modal":
        configured = bool(os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"))
        if not configured:
            try:
                profiles = tomllib.loads((Path.home() / ".modal.toml").read_text())
                selected = os.environ.get("MODAL_PROFILE")
                candidates = [profiles.get(selected, {})] if selected else [
                    value for value in profiles.values() if isinstance(value, dict) and value.get("active")
                ] or list(profiles.values())[:1]
                configured = any(isinstance(value, dict) and value.get("token_id") and value.get("token_secret")
                                 for value in candidates)
            except (OSError, ValueError):
                pass
        return {"configured": bool(configured), "reason": (
            "Modal credentials found; the packaged browser is checked inside the sandbox."
            if configured else "Modal credentials missing. Run scripts/harbor modal token new, or set MODAL_TOKEN_ID and MODAL_TOKEN_SECRET."
        )}
    if execution != "docker":
        return {"configured": False, "reason": f"Unknown run location: {execution}"}
    if not shutil.which("docker"):
        return {"configured": False, "reason": "Install Docker to launch local Harbor sandboxes."}
    try:
        result = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                capture_output=True, timeout=5, check=False)
        configured = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        configured = False
    return {"configured": configured, "reason": (
        "Docker is ready; the browser is packaged in each trial image." if configured
        else "Docker daemon is unavailable. Start Docker and retry."
    )}


def run_presets() -> list[dict]:
    shared = RunConfig(mode="model").model_dump()
    return [{"name": "default", "label": "Default configuration", "settings": shared}]


class RunManager:
    def __init__(self, data_dir: Path):
        self.root = validate_private_output(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._ownership = (self.root / ".server.lock").open("a+")
        try:
            fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as ex:
            self._ownership.close()
            raise RuntimeError("This run directory is already open in another dashboard.") from ex
        self.lock = threading.RLock()
        self.processes: dict[str, subprocess.Popen] = {}
        # A server cannot reattach to a previous server's worker. Keep its history
        # and report the interruption instead of presenting it as live forever.
        for path in self.root.glob("*/run.json"):
            record = read_json(path, {})
            if record.get("status") in ACTIVE:
                snapshot = read_json(path.parent / "snapshot.json", {})
                record["status"] = snapshot.get("phase") if snapshot.get("phase") in TERMINAL else "failed"
                record["error"] = snapshot.get("error") or "Dashboard stopped before this run finished."
                record["updated_at"] = now()
                write_json(path, record)

    def directory(self, run_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", run_id):
            raise APIError(404, "Run not found.")
        directory = self.root / run_id
        if not (directory / "run.json").is_file():
            raise APIError(404, "Run not found.")
        return directory

    def describe(self, run_id: str) -> dict:
        directory = self.directory(run_id)
        record = read_json(directory / "run.json", {})
        snapshot = read_json(directory / "snapshot.json", {})
        world = snapshot.get("world") or {}
        phase = snapshot.get("phase")
        if record.get("status") in ACTIVE and record.get("status") != "stopping":
            if phase in ACTIVE:
                record["status"] = phase
            elif phase in TERMINAL:
                # The worker still has to flush its files and exit; only the
                # monitor commits a terminal status to persistent run history.
                record["status"] = "grading"
        record.update(turns=world.get("turn", 0), stop_reason=world.get("stop_reason"))
        record["error"] = record.get("error") or snapshot.get("error")
        batch = record.get("config", {}).get("execution", "local") != "local" and not record.get("parent_batch_id")
        record["has_artifact"] = not batch and self._artifact(directory, snapshot) is not None
        return record

    def list(self) -> list[dict]:
        with self.lock:
            items = [self.describe(p.parent.name) for p in self.root.glob("*/run.json")
                     if re.fullmatch(r"[a-f0-9]{12}", p.parent.name)]
        return sorted(items, key=lambda r: r.get("created_at", ""), reverse=True)

    def detail(self, run_id: str) -> dict:
        with self.lock:
            directory = self.directory(run_id)
            snapshot = read_json(directory / "snapshot.json", {})
            # Old runs get the same read-only analysis without rewriting their saved records.
            snapshot["long_horizon"] = activity_measurements(snapshot)
            review = read_json(directory / "review.json", {})
            if isinstance(review, dict) and isinstance(review.get("summary"), str):
                snapshot["review_note"] = review["summary"]
            try:
                with (directory / "console.log").open("rb") as file:
                    file.seek(0, 2)
                    file.seek(max(0, file.tell() - 24_000))
                    console = file.read().decode("utf-8", errors="replace")
            except OSError:
                console = ""
            return {"run": self.describe(run_id), "snapshot": snapshot, "console": console}

    def start(self, config: RunConfig) -> dict:
        if config.models is not None and config.execution == "local":
            raise APIError(422, "Model selection requires a Harbor model batch on Modal or Docker.")
        targets = batch_models(config.model_dump())
        total = batch_total(config.model_dump())
        if total > 100:
            raise APIError(422, "A batch can contain at most 100 total trials across all models.")
        if config.execution == "local" and (config.trials != 1 or config.concurrency != 1):
            raise APIError(422, "Choose Harbor on Modal or Docker for multiple trials.")
        if config.execution != "local":
            if config.concurrency > total:
                raise APIError(422, "Concurrency cannot exceed the number of trials.")
            if config.mode == "model" and any(not name.startswith("openrouter/") for name in (
                *targets, config.simulator_model, config.grader_model,
            )):
                raise APIError(422, "Harbor batches currently support OpenRouter for all three model roles.")
            status = execution_readiness(config.execution)
            if not status["configured"]:
                raise APIError(422, status["reason"])
        if config.mode == "model":
            from refund_eval.dashboard.models import unknown_models

            roles = {**{f"target{'' if len(targets) == 1 else f' {n + 1}'}": m for n, m in enumerate(targets)},
                     "simulator": config.simulator_model, "grader": config.grader_model}
            problems = unknown_models(roles)
            if problems:
                raise APIError(422, " ".join(problems))
        inbox_sha256 = None
        if config.inbox_snapshot:
            if config.mode != "model" or config.data_mode not in {"fixture", "hybrid"}:
                raise APIError(422, "A Gmail snapshot needs a model trial with simulated Gmail (fixture or hybrid data).")
            from refund_eval.published.profile import validate_fixture_path
            from refund_eval.world.snapshot_store import file_sha256, snapshot_path

            try:
                path = snapshot_path(config.inbox_snapshot)
                validate_fixture_path(str(path), data_mode=config.data_mode)
            except ValueError as error:
                raise APIError(422, f"Gmail snapshot unavailable: {error}") from error
            inbox_sha256 = file_sha256(path)
        resume_sha256 = None
        if config.resume_log:
            from refund_eval.world.snapshot_store import file_sha256

            if config.mode != "model" or len(targets) != 1 or config.trials != 1:
                raise APIError(422, "Resuming continues one trial: choose one model and one trial.")
            if not Path(config.resume_log).is_file():
                raise APIError(422, f"No trial log at {config.resume_log}.")
            if config.resume_log.endswith(".json"):  # a dashboard snapshot: its run's config names the model
                original = read_json(Path(config.resume_log).parent / "config.json", {}).get("model")
            else:
                from inspect_ai.log import read_eval_log

                original = read_eval_log(config.resume_log, header_only=True).eval.model
            if str(original) != targets[0]:
                raise APIError(422, f"That trial ran {original}; resume it with the same model.")
            resume_sha256 = file_sha256(Path(config.resume_log))
        if config.execution == "local":
            from refund_eval.published.profile import (
                fixture_readiness,
                hybrid_readiness,
            )

            status = fixture_readiness() if config.data_mode == "fixture" else hybrid_readiness()
            if not status["configured"]:
                raise APIError(422, f"Published tools are not configured. {status['reason']}")
        if config.mode == "model":
            for model in (*targets, config.simulator_model, config.grader_model):
                provider = model.split("/", 1)[0]
                if provider == "mockllm":
                    continue
                keys = PROVIDER_KEYS.get(provider)
                if keys is None:
                    raise APIError(422, f"Unsupported model provider: {provider}.")
                if not any(os.environ.get(key) for key in keys):
                    raise APIError(422, f"Set {' or '.join(keys)} in the server environment before running {model}.")
                if not sdk_available(provider):
                    raise APIError(422, f"The {provider} SDK is not installed. Run uv sync --extra providers, then restart the dashboard.")
        with self.lock:
            active_settings = [read_json(self.root / run_id / "config.json", {})
                               for run_id, process in self.processes.items() if process.poll() is None]
            if active_settings:
                raise APIError(409, "A run is already active. Wait for it to finish or stop it first.")
            run_id = uuid.uuid4().hex[:12]
            directory = self.root / run_id
            directory.mkdir()
            settings = config.model_dump()
            settings["browser_runtime"] = "docker" if config.execution == "local" else "subprocess"
            if config.mode == "model":
                settings["model"] = targets[0]
            if inbox_sha256:
                settings["inbox_sha256"] = inbox_sha256
            if resume_sha256:
                settings["resume_sha256"] = resume_sha256
            record = {
                "id": run_id, "label": {"fixture": "Published tools · simulated data",
                      "hybrid": "Simulated Gmail + live web"}[config.data_mode],
                "mode": config.mode, "status": "starting", "created_at": now(), "updated_at": now(),
                "tool_profile": config.tool_profile,
                "data_mode": config.data_mode,
                "scenario": config.scenario,
                "model": targets[0],
                "config": settings, "error": None,
            }
            if config.execution != "local":
                record["label"] = f"{config.execution.title()} batch · {len(targets)} models · {total} Inspect trials"
            if config.inbox_snapshot:
                record["label"] += f" · Gmail snapshot {config.inbox_snapshot}"
            write_json(directory / "run.json", record)
            write_json(directory / "config.json", settings)
            if config.execution != "local":
                write_json(directory / "snapshot.json", {
                    "phase": "starting", "batch": {
                        "total": total, "trials_per_model": config.trials, "concurrency": config.concurrency,
                        "environment": config.execution, "counts": {"queued": total}, "trials": [],
                    },
                })
            env = dict(os.environ)
            env["REFUND_EVAL_KILL_FILE"] = str(directory / "KILL")
            env["PYTHONUNBUFFERED"] = "1"
            command = [sys.executable, "-m", "refund_eval.dashboard.worker", "--run-dir", str(directory)]
            if config.execution != "local":
                command = [str(PROJECT_ROOT / "scripts/harbor"), "python", "-m",
                           "refund_eval.harbor.dashboard", "--run-dir", str(directory)]
            try:
                with (directory / "console.log").open("wb") as console:
                    process = subprocess.Popen(
                        command,
                        cwd=PROJECT_ROOT, env=env, stdout=console, stderr=subprocess.STDOUT, start_new_session=True,
                    )
            except OSError as ex:
                record.update(status="failed", error=str(ex), updated_at=now())
                write_json(directory / "run.json", record)
                raise APIError(500, "Could not start the worker.") from ex
            self.processes[run_id] = process
            threading.Thread(target=self._monitor, args=(run_id, process), daemon=True).start()
            return self.describe(run_id)

    def _monitor(self, run_id: str, process: subprocess.Popen) -> None:
        code = process.wait()
        with self.lock:
            directory = self.directory(run_id)
            record = read_json(directory / "run.json", {})
            snapshot = read_json(directory / "snapshot.json", {})
            stopped = (directory / "KILL").exists() or (snapshot.get("world") or {}).get("killed")
            phase = snapshot.get("phase")
            status = "stopped" if stopped else phase if phase in TERMINAL else "completed" if code == 0 else "failed"
            error = snapshot.get("error")
            if status == "failed" and not error:
                error = f"Worker exited with code {code}. Open the run console for details."
            record.update(status=status, updated_at=now(), error=error, exit_code=code)
            write_json(directory / "run.json", record)
            if record.get("config", {}).get("execution", "local") != "local":
                # A killed/crashed controller cannot finalize its child records.
                # Preserve partial snapshots but release the dashboard's active state.
                for path in self.root.glob("*/run.json"):
                    child = read_json(path, {})
                    if child.get("parent_batch_id") != run_id or child.get("status") not in ACTIVE:
                        continue
                    child_status = "stopped" if stopped else "failed"
                    child_error = "Batch controller exited before this trial's results were finalized."
                    child.update(status=child_status, error=child_error, updated_at=now())
                    write_json(path, child)
                    partial = read_json(path.parent / "snapshot.json", {})
                    partial.update(phase=child_status, error=child_error, updated_at=now())
                    write_json(path.parent / "snapshot.json", partial)
                if phase not in TERMINAL:
                    batch = snapshot.get("batch", {})
                    terminal = "stopped" if stopped else "failed"
                    for counts in [batch.get("counts", {}), *[group["counts"] for group in batch.get("models", [])]]:
                        unfinished = sum(counts.pop(key, 0) for key in (*ACTIVE, "queued"))
                        counts[terminal] = counts.get(terminal, 0) + unfinished
                    for row in batch.get("trials", []):
                        if row.get("status") in ACTIVE:
                            row.update(status=terminal, stage="Controller exited")
                    snapshot.update(phase=status, error=error, updated_at=now())
                    write_json(directory / "snapshot.json", snapshot)

    def stop(self, run_id: str) -> dict:
        with self.lock:
            directory = self.directory(run_id)
            record = read_json(directory / "run.json", {})
            if record.get("parent_batch_id"):
                self.stop(record["parent_batch_id"])
                return self.describe(run_id)
            process = self.processes.get(run_id)
            final_phase = read_json(directory / "snapshot.json", {}).get("phase") in TERMINAL
            if process is not None and process.poll() is None and record.get("status") != "stopping" and not final_phase:
                (directory / "KILL").touch()
                record.update(status="stopping", updated_at=now())
                write_json(directory / "run.json", record)
                cloud = record.get("config", {}).get("execution", "local") != "local"
                threading.Thread(target=self._escalate_stop, args=(process, cloud), daemon=True).start()
            return self.describe(run_id)

    @staticmethod
    def _escalate_stop(process: subprocess.Popen, cloud: bool = False) -> None:
        # Give the tool/turn kill switch a chance to finish the Inspect log.
        delays = ((60, signal.SIGINT), (60, signal.SIGTERM), (60, signal.SIGKILL)) if cloud else (
            (3, signal.SIGINT), (5, signal.SIGTERM), (2, signal.SIGKILL))
        for delay, sig in delays:
            try:
                process.wait(timeout=delay)
                return
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    return

    @staticmethod
    def _artifact(directory: Path, snapshot: dict) -> Path | None:
        value = snapshot.get("inspect_log")
        paths = [Path(value)] if value else []
        paths.extend(sorted(directory.glob("**/*.eval"), reverse=True))
        for path in paths:
            resolved = path.resolve()
            if resolved.is_relative_to(directory.resolve()) and resolved.suffix == ".eval" and resolved.is_file():
                return resolved
        return None

    def artifact(self, run_id: str) -> Path:
        directory = self.directory(run_id)
        record = read_json(directory / "run.json", {})
        if record.get("config", {}).get("execution", "local") != "local" and not record.get("parent_batch_id"):
            raise APIError(404, "Open an individual trial to download its Inspect log.")
        path = self._artifact(directory, read_json(directory / "snapshot.json", {}))
        if path is None:
            raise APIError(404, "The Inspect log is not available yet.")
        return path

    def close(self) -> None:
        for run_id, process in list(self.processes.items()):
            if process.poll() is None:
                self.stop(run_id)
        for process in self.processes.values():
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                pass
        if not self._ownership.closed:
            fcntl.flock(self._ownership, fcntl.LOCK_UN)
            self._ownership.close()




def scenario_details(scenario) -> dict:
    return {**scenario.task.dashboard_metadata(scenario),
            "organisations": [{"key": org.key, "name": org.name, "kind": org.kind} for org in scenario.orgs]}


def public_config() -> dict:
    from refund_eval.published.profile import (
        fixture_readiness,
        hybrid_readiness,
    )

    scenario = load_scenario(RunConfig.model_fields["scenario"].default)
    scenarios = [(name, load_scenario(name)) for name in scenario_names()]
    fixture_status = fixture_readiness()
    hybrid_status = hybrid_readiness()
    return {
        "default_model": dashboard_default_model(),
        "default_role_model": DEFAULT_ROLE_MODEL,
        "default_grader_model": DEFAULT_GRADER_MODEL,
        "run_presets": run_presets(),
        "run_defaults": RunConfig().model_dump(),
        "model_defaults": RunConfig(mode="model").model_dump(),
        "execution_readiness": {name: execution_readiness(name) for name in ("local", "modal", "docker")},
        "providers": [{"name": name, "configured": sdk_available(name) and any(os.environ.get(k) for k in keys),
                       "sdk_available": sdk_available(name), "credentials_available": any(os.environ.get(k) for k in keys)}
                      for name, keys in PROVIDER_KEYS.items()],
        "scenario": {"id": scenario.id, "task_text": task_message(scenario), **scenario_details(scenario)},
        "inbox_snapshots": list_snapshots(),
        "modes": ["model"],
        "scenarios": [{"name": name, "label": item.label or item.id, **scenario_details(item)}
                      for name, item in scenarios],
        "tool_profiles": [
            {"name": "published", "label": "Published tools", **hybrid_status,
             "default_data_mode": "hybrid", "data_modes": [
                 {"name": "hybrid", "label": "Simulated Gmail + live web", **hybrid_status},
                 {"name": "fixture", "label": "Simulated inbox and websites", **fixture_status},
             ]},
        ],
    }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], manager: RunManager):
        self.manager = manager
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def log_message(self, format: str, *args: Any) -> None:
        # No query strings, transcripts or user data in the terminal server log.
        pass

    def _guard(self) -> None:
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host") not in allowed:
            raise APIError(403, "Use the local dashboard address.")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            raise APIError(403, "Cross-origin requests are not allowed.")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise APIError(403, "Cross-site requests are not allowed.")

    def _send(self, status: int, body: bytes, content_type: str = "application/json", filename: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; object-src 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, allow_nan=False).encode())

    def _body(self, max_length: int = 16_384) -> dict:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise APIError(415, "Send application/json.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as ex:
            raise APIError(400, "Invalid content length.") from ex
        if length < 0 or length > max_length:
            raise APIError(413, "Request too large.")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, UnicodeDecodeError) as ex:
            raise APIError(400, "Invalid JSON.") from ex
        if not isinstance(data, dict):
            raise APIError(422, "Expected a JSON object.")
        return data

    def do_GET(self) -> None:
        try:
            self._guard()
            path = urlsplit(self.path).path
            if path == "/api/config":
                self._json(200, public_config())
            elif path == "/api/models":
                from refund_eval.dashboard.models import model_catalog

                self._json(200, model_catalog())
            elif path == "/api/runs":
                self._json(200, {"runs": self.server.manager.list()})
            elif re.fullmatch(r"/api/runs/[a-f0-9]{12}", path):
                self._json(200, self.server.manager.detail(path.rsplit("/", 1)[1]))
            elif re.fullmatch(r"/api/runs/[a-f0-9]{12}/artifact", path):
                artifact = self.server.manager.artifact(path.split("/")[3])
                self._send(200, artifact.read_bytes(), "application/octet-stream", artifact.name)
            elif path in {"/", "/index.html", "/app.js", "/styles.css", "/static/app.js", "/static/styles.css",
                          "/static/markdown-it.min.js", "/static/markdown.js"}:
                name = "index.html" if path == "/" else path.rsplit("/", 1)[1]
                file = STATIC_DIR / name
                if not file.is_file():
                    raise APIError(404, "Dashboard assets not found.")
                types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
                self._send(200, file.read_bytes(), types[file.suffix])
            else:
                raise APIError(404, "Not found.")
        except APIError as ex:
            self._json(ex.status, {"error": ex.message})

    def do_POST(self) -> None:
        try:
            self._guard()
            path = urlsplit(self.path).path
            if path == "/api/runs":
                config = RunConfig.model_validate(self._body())
                self._json(201, {"run": self.server.manager.start(config)})
            elif re.fullmatch(r"/api/runs/[a-f0-9]{12}/stop", path):
                self._body()
                self._json(200, {"run": self.server.manager.stop(path.split("/")[3])})
            else:
                raise APIError(404, "Not found.")
        except APIError as ex:
            self._json(ex.status, {"error": ex.message})
        except ValidationError as ex:
            messages = [f"{'.'.join(str(k) for k in e['loc'])}: {e['msg']}" for e in ex.errors()]
            self._json(422, {"error": "; ".join(messages)})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local refund-eval dashboard.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(".context/dashboard/runs"))
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        manager = RunManager(args.data_dir)
    except (RuntimeError, ValueError) as ex:
        parser.error(str(ex))
    try:
        server = DashboardServer(("127.0.0.1", args.port), manager)
    except OSError:
        manager.close()
        raise
    def terminate(signum: int, frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    print(f"Refund eval dashboard: http://127.0.0.1:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        manager.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

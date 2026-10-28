"""Durable local jobs for the realism MCP server.

The MCP process owns no execution lifetime: jobs run in their own POSIX session.
A backend is an importable Python callable, never an agent CLI. It must implement
``run_job(context)`` and account for *every* model call through its stage ledger.
An interrupted job is never silently restarted because paid stages may exist.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

TERMINAL = {"complete", "failed", "cancelled", "interrupted"}
JOB_ID = re.compile(r"^[a-f0-9]{32}$")


def now() -> str:
    return datetime.now(UTC).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _alive(pid: int, directory: Path, token: str) -> bool:
    """Refuse to signal a reused PID or a different job's process."""
    try:
        command = subprocess.check_output(["ps", "-p", str(pid), "-o", "command="], text=True).strip()
    except subprocess.CalledProcessError:
        return False
    return all(part in command for part in ("refund_eval.realism.background", str(directory), token))


class JobManager:
    def __init__(self, repo: str | Path, *, backend: str | None = None):
        self.repo = Path(repo).resolve()
        self.root = self.repo / ".context" / "realism" / "background"
        self.backend = backend
        if _git(self.repo, "rev-parse", "--show-toplevel") != str(self.repo):
            raise ValueError("repo must be the Git repository root")

    def directory(self, job_id: str) -> Path:
        if not JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid job ID")
        return self.root / job_id

    def start(self, *, request_key: str, budget_usd: float, trials: int = 3, token_budget: int = 1_000_000,
              max_rounds: int = 1, source: str = "HEAD", findings: list[str] | None = None,
              model: str = "openrouter/anthropic/claude-haiku-4.5",
              per_trial_cost: float = 0.5, scan_reserve: float = 1.0, repair_budget: float = 2.0,
              coding_model: str = "openrouter/anthropic/claude-sonnet-4.5",
              review_model: str = "openrouter/anthropic/claude-sonnet-4.5",
              max_turns: int = 10000, time_limit: int = 36000, sim_weeks: float = 3,
              scenario: str = "train_refund_late", judge: str = "openrouter/anthropic/claude-opus-5.5",
              skip_diagnostic: bool = False) -> dict:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_key):
            raise ValueError("request_key must contain 1-80 letters, digits, underscores or hyphens")
        if type(skip_diagnostic) is not bool or (skip_diagnostic and not findings):
            raise ValueError("skip_diagnostic requires a boolean and nonempty saved findings")
        if not self.backend:
            raise ValueError("No autonomous Python backend configured; job was not started")
        if not math.isfinite(budget_usd) or budget_usd <= 0:
            raise ValueError("budget_usd must be finite and positive")
        if any(not math.isfinite(value) or value <= 0 for value in (per_trial_cost, scan_reserve, repair_budget, sim_weeks)):
            raise ValueError("Stage reservations and per-trial cost must be finite and positive")
        if any(type(value) is not int or value <= 0 for value in (trials, token_budget, max_rounds, max_turns, time_limit)):
            raise ValueError("trials, token_budget and max_rounds must be positive integers")
        module, name = self.backend.rsplit(":", 1)
        if not callable(getattr(importlib.import_module(module), name)):
            raise TypeError("Configured backend is not callable")
        frozen_findings = []
        for item in findings or []:
            path = (self.repo / item).resolve()
            if not path.is_relative_to(self.repo / ".context") or not path.is_file() or path.name.startswith(".env"):
                raise ValueError("Findings must be existing report files under repository .context")
            if path.stat().st_size > 2_000_000:
                raise ValueError("Finding exceeds 2 MB")
            data = path.read_bytes()
            frozen_findings.append((path, data, hashlib.sha256(data).hexdigest()))
        # Only committed source can be frozen; a mutable working tree is never the job source.
        commit = _git(self.repo, "rev-parse", "--verify", "--end-of-options", f"{source}^{{commit}}")
        request = {"budget_usd": budget_usd, "trials": trials, "token_budget": token_budget,
                   "max_rounds": max_rounds, "source_commit": commit, "model": model,
                   "backend": self.backend, "findings": findings or [],
                   "finding_sources": [{"path": str(path.relative_to(self.repo)), "sha256": digest}
                                       for path, _, digest in frozen_findings], "per_trial_cost": per_trial_cost,
                   "scan_reserve": scan_reserve, "repair_budget": repair_budget,
                   "coding_model": coding_model, "review_model": review_model,
                   "max_turns": max_turns, "time_limit": time_limit, "sim_weeks": sim_weeks,
                   "scenario": scenario, "judge": judge, "skip_diagnostic": skip_diagnostic}
        fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        job_id, token = uuid.uuid4().hex, uuid.uuid4().hex
        directory = self.directory(job_id)
        with locked(self.root / "manager.lock"):
            key_path = self.root / "requests" / f"{request_key}.json"
            if key_path.exists():
                previous = json.loads(key_path.read_text())
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("request_key already names a job with different settings")
                return self.status(previous["job_id"])
            active_path = self.root / "active.json"
            if active_path.exists():
                active = self.status(json.loads(active_path.read_text())["job_id"])
                if active["status"] not in TERMINAL:
                    raise ValueError(f"Job {active['job_id']} is already active")
            directory.mkdir(mode=0o700)
            settings = {**request, "repo": str(self.repo), "job_id": job_id,
                        "created_at": now(), "token": token, "request_key": request_key}
            copied = []
            for index, (path, data, _) in enumerate(frozen_findings):
                # Persist exactly the bytes hashed before starting the job.
                destination = directory / "findings" / f"{index}-{path.name}"
                destination.parent.mkdir(mode=0o700, exist_ok=True)
                destination.write_bytes(data)
                os.chmod(destination, 0o600)
                copied.append(str(destination))
            settings["findings"] = copied
            atomic_json(directory / "settings.json", settings)
            atomic_json(directory / "state.json", {"job_id": job_id, "status": "starting", "created_at": now()})
            # Durable request/start ownership precedes spawn: a lost response cannot duplicate work.
            atomic_json(active_path, {"job_id": job_id})
            atomic_json(key_path, {"job_id": job_id, "fingerprint": fingerprint})
            with (directory / "worker.log").open("ab") as output:
                os.chmod(directory / "worker.log", 0o600)
                # Reparent the worker before replying. Some MCP clients kill the
                # entire transport descendant tree, including separate sessions.
                bootstrap = (
                    "import os,sys; pid=os.fork(); "
                    "(os.waitpid(pid,0),os._exit(0)) if pid else None; "
                    "os.setsid(); pid=os.fork(); os._exit(0) if pid else None; "
                    "os.execv(sys.executable,[sys.executable,'-m','refund_eval.realism.background',"
                    "'--worker',sys.argv[1],'--token',sys.argv[2]])"
                )
                try:
                    subprocess.run([sys.executable, "-c", bootstrap, str(directory), token],
                                   cwd=self.repo, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                   start_new_session=True, close_fds=True, check=True, timeout=10)
                except (OSError, subprocess.SubprocessError):
                    # A detached spawn may have succeeded even when its bootstrap
                    # failed. Keep ownership; never invite a duplicate paid job.
                    atomic_json(directory / "state.json", {"job_id": job_id, "status": "startup_unknown",
                                                           "detail": "Inspect saved startup markers; do not retry"})
            deadline = time.monotonic() + 5
            while not (directory / "process.json").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            if not (directory / "process.json").exists():
                atomic_json(directory / "state.json", {"job_id": job_id, "status": "startup_unknown",
                                                       "detail": "Detached worker did not confirm startup; inspect artifacts, do not retry paid stages"})
        return self.status(job_id)

    def status(self, job_id: str) -> dict:
        directory = self.directory(job_id)
        state = json.loads((directory / "state.json").read_text())
        process_path = directory / "process.json"
        if not process_path.exists() and (directory / "worker-started.json").exists():
            process_path = directory / "worker-started.json"
        if process_path.exists() and state["status"] not in TERMINAL:
            process = json.loads(process_path.read_text())
            if not _alive(process["pid"], directory, process["token"]):
                # Never retry an interrupted job: its outstanding paid reservations remain held.
                state = {**state, "status": "interrupted", "detail": "Worker exited; inspect artifacts and reconcile paid stages before starting again"}
        ledger_path = directory / "spend.json"
        if ledger_path.exists():
            from refund_eval.realism.stages import summary
            state["spend"] = summary(json.loads(ledger_path.read_text()))
        return {**state, "artifacts": str(directory)}

    def results(self, job_id: str) -> dict:
        directory = self.directory(job_id)
        result = {"state": self.status(job_id)}
        path = directory / "result.json"
        if path.exists():
            result["result"] = json.loads(path.read_text())
        else:
            # Verified repair evidence may precede a failed measurement stage.
            provisional = directory / "autonomous-result.json"
            if provisional.exists():
                result["provisional_result"] = json.loads(provisional.read_text())
        return result

    def cancel(self, job_id: str) -> dict:
        directory = self.directory(job_id)
        state = self.status(job_id)
        if state["status"] in TERMINAL:
            return state
        atomic_json(directory / "cancel.json", {"requested_at": now()})
        process_path = directory / "process.json"
        if process_path.exists():
            process = json.loads(process_path.read_text())
            if _alive(process["pid"], directory, process["token"]):
                os.kill(process["pid"], signal.SIGTERM)
        return {**state, "cancel_requested": True}


class JobContext:
    """Backend contract: persistent paths, cancellation, state, accounted model stages.

    Backend must use ``command`` for subprocesses and reserve every paid operation
    in ``ledger`` before starting it. Unknown usage retains its full reservation.
    Candidate changes stay in ``worktree``; the source checkout is never modified.
    """
    def __init__(self, directory: Path):
        self.directory = directory
        self.settings = json.loads((directory / "settings.json").read_text())
        self.repo = Path(self.settings["repo"])
        self.worktree = directory / "worktree"
        self.ledger = directory / "spend.json"
        self.child: subprocess.Popen | None = None
        self.cancelled = False

    def update(self, phase: str, **detail: Any) -> None:
        atomic_json(self.directory / "state.json", {"job_id": self.settings["job_id"], "status": "running",
                                                   "phase": phase, "updated_at": now(), **detail})

    def check_cancelled(self) -> None:
        if self.cancelled or (self.directory / "cancel.json").exists():
            raise InterruptedError("Job cancelled")

    def _stop_child(self) -> None:
        if self.child is None:
            return
        from refund_eval.published.mcp import descendant_pids, kill_processes
        descendants = descendant_pids(self.child.pid)
        try:
            os.killpg(self.child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(self.child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_processes(descendants)
        self.child.wait()

    def command(self, argv: list[str], *, name: str, timeout: int = 600,
                cwd: str | Path | None = None, env: dict[str, str] | None = None,
                allow_cancelled: bool = False) -> dict:
        """Run a bounded local command, preserving output privately; never a shell."""
        if not allow_cancelled:
            self.check_cancelled()
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name) or not argv:
            raise ValueError("Invalid command name or arguments")
        workdir = Path(cwd or self.worktree).resolve()
        if not workdir.is_relative_to(self.directory.resolve()):
            raise ValueError("Command cwd must be this job's isolated directory")
        with (self.directory / f"{name}.log").open("wb") as output:
            os.chmod(output.name, 0o600)
            self.child = subprocess.Popen(argv, cwd=workdir, env=env, stdin=subprocess.DEVNULL,
                                          stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = self.child.wait(timeout=timeout)
            finally:
                self._stop_child()
                self.child = None
        if not allow_cancelled:
            self.check_cancelled()
        return {"returncode": code, "log": str(self.directory / f"{name}.log")}


def run_worker(directory: Path, token: str) -> int:
    context = JobContext(directory)
    # Load authentication in the worker only. Never copy .env into candidate or
    # control source, print its values, or supply it to model-authored test code.
    from dotenv import load_dotenv
    load_dotenv(context.repo / ".env", override=False)
    if context.settings["token"] != token:
        raise ValueError("Worker token does not match")
    # Never start a second worker against this job's paid ledger.
    marker = directory / "worker-started.json"
    try:
        with marker.open("x") as stream:
            json.dump({"pid": os.getpid(), "token": token, "started_at": now()}, stream)
        os.chmod(marker, 0o600)
    except FileExistsError:
        raise RuntimeError("Job worker already started; recover its outputs without retrying") from None
    atomic_json(directory / "process.json", {"pid": os.getpid(), "token": token, "started_at": now()})

    def cancelled(*_):
        context.cancelled = True
        # command() cleans up its own process group in finally, including grandchildren.
        raise InterruptedError("Job cancelled")

    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    status, detail, outcome = "failed", "Worker did not complete", None
    try:
        context.check_cancelled()
        context.update("creating isolated source")
        subprocess.run(["git", "-C", str(context.repo), "worktree", "add", "--detach", str(context.worktree),
                        context.settings["source_commit"]], check=True, capture_output=True)
        context.check_cancelled()
        module, name = context.settings["backend"].rsplit(":", 1)
        result = getattr(importlib.import_module(module), name)(context)
        context.check_cancelled()
        if not isinstance(result, dict):
            raise TypeError("Backend result must be a JSON object")
        atomic_json(directory / "result.json", result)
        status, detail, outcome = "complete", "Result and candidate worktree saved", result.get("status")
    except InterruptedError:
        status, detail = "cancelled", "Cancellation requested; outstanding spend reservations remain held"
        # Preserve even uncommitted candidate evidence and all paid accounting.
    except Exception as error:  # noqa: BLE001 -- durably report every backend failure without retrying paid work
        # Error class only: provider exception text can contain credentials/request bodies.
        detail = f"Backend failed ({type(error).__name__}); saved paid stages must be reconciled, never auto-retried"
    finally:
        if status != "complete":
            # Model/Modal cleanup belongs to the backend: local process termination
            # alone does not imply its remote sandboxes have stopped.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            def cleanup_timeout(*_):
                raise TimeoutError("Backend cleanup exceeded 45 seconds")
            signal.signal(signal.SIGALRM, cleanup_timeout)
            signal.setitimer(signal.ITIMER_REAL, 45)
            try:
                module, _ = context.settings["backend"].rsplit(":", 1)
                cleanup = getattr(importlib.import_module(module), "cleanup_job", None)
                cleanup_result = cleanup(context) if cleanup else {"status": "not_supported", "remote_state": "unknown"}
                atomic_json(directory / "cleanup.json", cleanup_result)
            except Exception as error:  # noqa: BLE001 -- cleanup failure must leave durable terminal state
                atomic_json(directory / "cleanup.json", {"status": "failed", "error_type": type(error).__name__,
                                                         "remote_state": "unknown"})
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
        atomic_json(directory / "state.json", {"job_id": context.settings["job_id"], "status": status,
                                               "detail": detail, "outcome": outcome, "finished_at": now()})
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    raise SystemExit(run_worker(args.worker, args.token))

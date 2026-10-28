"""Run exactly one native Inspect trial inside a Harbor environment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import time
from pathlib import Path

from inspect_ai import eval, eval_async

from refund_eval.config import DEFAULT_GRADER_MODEL, DEFAULT_ROLE_MODEL
from refund_eval.harbor.artifacts import finite_json
from refund_eval.harbor.config import SNAPSHOT_IN_IMAGE, TrialRequest
from refund_eval.published.provider import published_model_args
from refund_eval.scenario.schema import load_scenario
from refund_eval.task import impossible_refund
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH
from refund_eval.world.state import WORLD_KEY


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(finite_json(value), indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def _resume_args(settings) -> dict:
    """The packaged trial log to continue from, checked against its pinned hash."""
    if not settings.resume_sha256:
        return {}
    from refund_eval.harbor.config import RESUME_IN_IMAGE, RESUME_SNAPSHOT_IN_IMAGE
    from refund_eval.world.snapshot_store import file_sha256

    root = Path(__file__).resolve().parents[2]
    path = root / RESUME_SNAPSHOT_IN_IMAGE if (root / RESUME_SNAPSHOT_IN_IMAGE).is_file() else root / RESUME_IN_IMAGE
    if not path.is_file() or file_sha256(path) != settings.resume_sha256:
        raise ValueError("The packaged trial log to resume is missing or does not match this batch")
    return {"resume_log": str(path)}


def _initialize(request: TrialRequest, output: Path) -> dict:
    if request.instruction.strip() != load_scenario(request.settings.scenario).task_text.strip():
        raise ValueError("Harbor instruction must match the benchmark scenario exactly")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Trial output directory must be empty; use a separate directory per attempt")
    write_json(output / "request.json", request.model_dump(mode="json"))
    from refund_eval.realism.probe import sandbox_facts

    write_json(output / "probe.json", sandbox_facts())  # what this sandbox reveals; read by `realism open --probe`
    settings = request.settings
    inbox = DEFAULT_FIXTURE_PATH
    if settings.inbox_sha256:
        from refund_eval.world.snapshot_store import file_sha256

        inbox = Path(__file__).resolve().parents[2] / SNAPSHOT_IN_IMAGE
        if not inbox.is_file() or file_sha256(inbox) != settings.inbox_sha256:
            raise ValueError("The packaged Gmail snapshot is missing or does not match this batch")
    return {
        "task_args": {
            "trials": 1, "mail_source": f"fixture:{inbox}",
            "max_turns": settings.max_turns, "history_tokens": settings.history_tokens,
            "sim_weeks": settings.sim_weeks,
            "cost_limit": settings.cost_limit, "time_limit": settings.time_limit,
            "token_budget": settings.token_budget, "budget_notice": settings.budget_notice,
            "research_mode": settings.research_mode,
            "tool_profile": settings.tool_profile, "published_data": settings.data_mode,
            "browser_runtime": settings.browser_runtime, "artifact_dir": str(output),
            "scenario": settings.scenario,
            **_resume_args(settings),
        },
        "model": request.model,
        "model_args": {**(published_model_args(request.model) if settings.tool_profile == "published" else {}),
                    **({"reasoning_enabled": True} if settings.reasoning else {})},
        "model_roles": {
            "simulator": settings.simulator_model or DEFAULT_ROLE_MODEL,
            "grader": settings.grader_model or DEFAULT_GRADER_MODEL,
        },
        "log_dir": str(output), "log_format": "eval", "display": "none", "max_samples": 1,
        "metadata": {"harbor_trial_id": request.trial_id,
                     **({"trial_deadlines": settings.trial_deadlines} if settings.trial_deadlines else {})},
    }


def _export(request: TrialRequest, output: Path, log) -> dict:
    settings = request.settings
    if log.status != "success" or not log.samples or log.samples[0].error:
        raise RuntimeError(f"Inspect trial failed; see {log.location}: {log.error}")
    sample = log.samples[0]
    from refund_eval.runtime.snapshots import final_snapshot, json_safe

    snapshot = json_safe(final_snapshot(log))
    write_json(output / "snapshot.json", snapshot)
    world = sample.store[WORLD_KEY]
    write_json(output / "world.json", world)
    scores = {
        name: score.model_dump(mode="json") for name, score in (sample.scores or {}).items()
    }
    write_json(output / "scores.json", scores)
    write_json(output / "transcript.json", [m.model_dump(mode="json") for m in sample.messages])
    usage = {
        "models": {k: v.model_dump(mode="json") for k, v in sample.model_usage.items()},
        "roles": {k: v.model_dump(mode="json") for k, v in sample.role_usage.items()},
        "note": "Role usage separates simulator calls from grader calls (the simulator's financial consistency checker); "
                "the trial cost limit covers every model call in the trial.",
    }
    write_json(output / "usage.json", usage)
    result = {
        "schema_version": 4, "trial_id": request.trial_id,
        "tool_profile": settings.tool_profile, "data_mode": settings.data_mode,
        "browser_runtime": settings.browser_runtime,
        "browser_boundary": sample.store.get("published_tool_profile", {}).get("browser_boundary"),
        "status": log.status,
        "inspect_log": Path(log.location).name,
        "stop_reason": world["stop_reason"], "turns": world["turn"],
        "scores_present": bool(sample.scores),
        "passed": bool(sample.scores) and world["stop_reason"] != "kill_switch",
    }
    if settings.trial_deadlines is not None:
        result["trial_deadlines"] = settings.trial_deadlines
    # The completion marker is only published after every artifact is written.
    write_json(output / "result.tmp", result)
    (output / "result.tmp").replace(output / "result.json")
    return result



def run_trial(request: TrialRequest, output: Path) -> dict:
    """Legacy synchronous contract, retained for old requests."""
    [log] = eval(impossible_refund, **_initialize(request, output))
    return _export(request, output, log)


def _identity(request: TrialRequest) -> dict:
    pid = os.getpid()
    return {"pid": pid, "start_token": Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19],
            "trial_id": request.trial_id,
            "request_sha256": hashlib.sha256(request.model_dump_json().encode()).hexdigest()}


def signal_owned_worker(identity: Path, request_path: Path, signum: int) -> bool:
    """Signal only the still-matching PID of this exact request, never pkill."""
    request = TrialRequest.model_validate_json(request_path.read_text())
    saved = json.loads(identity.read_text())
    pid = saved["pid"]
    if (type(pid) is not int or pid <= 1 or saved.get("trial_id") != request.trial_id
            or saved.get("request_sha256") != hashlib.sha256(request.model_dump_json().encode()).hexdigest()):
        raise ValueError("Worker identity does not match request")
    try:
        token = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if token != saved.get("start_token") or b"refund_eval.harbor.worker" not in command:
            raise ValueError("Worker PID was reused or is not the owned worker")
        os.kill(pid, signum)
        return True
    except FileNotFoundError:
        return False


def _diagnostics(request: TrialRequest, output: Path, log, error: BaseException | None) -> None:
    """Partial native usage is evidence, never a complete bill or success marker."""
    failures = []
    if log is None:
        from inspect_ai.log import read_eval_log

        candidates = list(output.glob("*.eval"))
        if len(candidates) == 1:
            try:
                log = read_eval_log(str(candidates[0]))
            except (OSError, ValueError, TypeError) as exc:
                failures.append({"artifact": "native_log", "error_type": type(exc).__name__})
    if log is not None and log.samples:
        sample = log.samples[0]
        values = {
            "usage.partial.json": {"complete": False, "cost_usd": None,
                "models": {k: v.model_dump(mode="json") for k, v in (sample.model_usage or {}).items()},
                "roles": {k: v.model_dump(mode="json") for k, v in (sample.role_usage or {}).items()}},
            "transcript.partial.json": [m.model_dump(mode="json") for m in sample.messages],
            "world.partial.json": sample.store.get(WORLD_KEY),
        }
        for name, value in values.items():
            try:
                write_json(output / name, value)
            except (OSError, ValueError, TypeError) as exc:
                failures.append({"artifact": name, "error_type": type(exc).__name__})
    artifacts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in output.iterdir() if path.is_file() and path.name != "terminal.json"}
    terminal = {"version": 1, "trial_id": request.trial_id, "trial_deadlines": request.settings.trial_deadlines,
                "status": getattr(log, "status", "unavailable"), "success_marker": False,
                "error_type": type(error).__name__ if error else None, "cost_usd": None,
                "usage_complete": False, "artifacts": artifacts, "export_errors": failures}
    write_json(output / "terminal.tmp", terminal)
    (output / "terminal.tmp").replace(output / "terminal.json")


async def run_trial_async(request: TrialRequest, output: Path, *, worker_seconds: float | None = None,
                          hard_exit: bool = False) -> dict:
    """One evaluation with an idempotent stop and a fixed log-close deadline."""
    from refund_eval.harbor.deadlines import remaining, settle, validate_deadlines

    bounds = validate_deadlines(request.settings.trial_deadlines, request.settings.time_limit)
    start = time.monotonic()
    allowance = bounds["worker_hard_seconds"] if worker_seconds is None else worker_seconds
    if not 0 < allowance <= bounds["worker_hard_seconds"]:
        raise ValueError("Invalid remaining worker allowance")
    hard = start + allowance
    soft = hard - bounds["flush_seconds"]
    if soft <= start:
        raise TimeoutError("No worker execution budget remains")
    kwargs = _initialize(request, output)
    kwargs.pop("display")  # eval_async has no synchronous display argument.
    write_json(output / "worker-identity.json", _identity(request))
    evaluation = asyncio.create_task(eval_async(impossible_refund, **kwargs))
    loop = asyncio.get_running_loop()
    stopped = False
    stop_due = hard
    stopped_event = asyncio.Event()

    def stop():
        nonlocal stopped, stop_due
        if not stopped:
            stopped = True
            stop_due = min(hard, time.monotonic() + bounds["flush_seconds"])
            stopped_event.set()
            if hard_exit:
                # Tighten the process timer once; never extend the final hard bound.
                signal.setitimer(signal.ITIMER_REAL, max(.000001, remaining(stop_due)))
            evaluation.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)
    log, error = None, None
    stop_waiter = asyncio.create_task(stopped_event.wait())
    try:
        done, _ = await asyncio.wait({evaluation, stop_waiter}, timeout=remaining(soft),
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            error = TimeoutError("Worker execution deadline reached")
            stop()
        logs = await settle(evaluation, stop_due if stopped else hard)
        if len(logs) != 1:
            raise RuntimeError("Expected one native Inspect log")
        [log] = logs
        if error is not None or stopped:
            raise error or asyncio.CancelledError()
        return _export(request, output, log)
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- preserve primary error and collect best-effort evidence
        error = error or exc
        stop()
        if not evaluation.done():
            try:
                logs = await settle(evaluation, stop_due)
                if len(logs) == 1:
                    [log] = logs
            except (Exception, asyncio.CancelledError):  # noqa: BLE001 -- cancellation close may fail; retain original error
                log = None
        try:
            _diagnostics(request, output, log, error)
        except Exception:
            import logging

            logging.getLogger(__name__).exception("Could not export terminal diagnostics")
        raise error
    finally:
        stop_waiter.cancel()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/logs/artifacts/refund"))
    parser.add_argument("--worker-seconds", type=float)
    parser.add_argument("--signal-owned", choices=["TERM", "KILL"])
    args = parser.parse_args()
    if args.signal_owned:
        signal_owned_worker(args.output / "worker-identity.json", args.request,
                            signal.SIGTERM if args.signal_owned == "TERM" else signal.SIGKILL)
        return 0
    request = TrialRequest.model_validate_json(args.request.read_text())
    if request.settings.trial_deadlines is None:
        if args.worker_seconds is not None:
            parser.error("Worker allowance requires an explicit lifecycle policy")
        run_trial(request, args.output)
    else:
        import math

        allowance = args.worker_seconds if args.worker_seconds is not None else request.settings.trial_deadlines["worker_hard_seconds"]
        if not math.isfinite(allowance) or not 0 < allowance <= request.settings.trial_deadlines["worker_hard_seconds"]:
            parser.error("Invalid finite remaining worker allowance")
        # Covers task initialization, native log close, export and asyncio shutdown.
        # This clock is created inside this sandbox; no host epoch crosses over.
        signal.signal(signal.SIGALRM, lambda *_: os._exit(124))
        signal.setitimer(signal.ITIMER_REAL, allowance)
        try:
            asyncio.run(run_trial_async(request, args.output, worker_seconds=allowance, hard_exit=True))
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

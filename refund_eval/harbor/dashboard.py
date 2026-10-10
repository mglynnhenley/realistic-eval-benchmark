"""Dashboard batch controller, run in the isolated Harbor environment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path

from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.trial.hooks import TrialEvent

from refund_eval.config import resolve_cost_limits
from refund_eval.harbor.__main__ import prepare
from refund_eval.harbor.config import TrialSettings, batch_models, batch_total

logger = logging.getLogger(__name__)


def read(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, allow_nan=False, indent=2) + "\n")
    temporary.replace(path)


def now() -> str:
    return datetime.now(UTC).isoformat()


class BatchProgress:
    def __init__(self, directory: Path, job_dir: Path):
        self.directory = directory
        self.job_dir = job_dir
        self.record = read(directory / "run.json")
        self.config = read(directory / "config.json")
        self.models = batch_models(self.config)
        self.total = batch_total(self.config)
        self.rows: dict[str, dict] = {}
        self.phase = "running"
        self.error = None

    async def event(self, event) -> None:
        stages = {
            TrialEvent.START: "Preparing sandbox", TrialEvent.ENVIRONMENT_START: "Building and starting sandbox",
            TrialEvent.AGENT_START: "Running Inspect", TrialEvent.AGENT_END: "Collecting results",
            TrialEvent.VERIFICATION_START: "Verifying artifacts", TrialEvent.CANCEL: "Stopping sandbox",
            TrialEvent.END: "Finished",
        }
        name = event.trial_name
        if name not in self.rows:
            run_id = hashlib.sha256(f"{self.directory.name}/{name}".encode()).hexdigest()[:12]
            child = self.directory.parent / run_id
            child.mkdir(exist_ok=True)
            model = event.config.agent.model_name
            number = sum(row["model"] == model for row in self.rows.values()) + 1
            cap = resolve_cost_limits(self.config["cost_limit"], self.models,
                                      scenario=self.config.get("scenario", "train_refund_pressure"))[model]
            child_config = {**self.config, "model": model, "models": [model], "cost_limit": cap}
            record = {**self.record, "model": model, "config": child_config, "id": run_id, "parent_batch_id": self.directory.name,
                      "label": f"Trial {number} · {model.removeprefix('openrouter/')}",
                      "status": "starting", "created_at": now(), "updated_at": now()}
            write(child / "run.json", record)
            write(child / "config.json", child_config)
            self.rows[name] = {"id": run_id, "name": name, "label": record["label"], "model": model, "status": "starting"}
        self.rows[name]["stage"] = stages[event.event]
        if event.event == TrialEvent.END:
            self.rows[name]["ended"] = True
        self.refresh()

    def refresh(self, final: bool = False) -> None:
        for name, row in self.rows.items():
            child = self.directory.parent / row["id"]
            trial = self.job_dir / name
            artifacts = trial / "artifacts/logs/artifacts/refund"
            snapshot = read(artifacts / "snapshot.json") or read(trial / "agent/progress.json", {})
            outcome = read(artifacts / "result.json", {})
            result = read(trial / "result.json", {})
            error = result.get("exception_info")
            ended = row.get("ended") or final
            if ended:
                if outcome.get("passed") and not error:
                    status = "completed"
                elif self.phase == "stopping" or (error or {}).get("exception_type") == "CancelledError":
                    status = "stopped"
                else:
                    status = "failed"
            else:
                status = "stopping" if self.phase == "stopping" else (
                    "grading" if row.get("stage") == "Verifying artifacts" else
                    "running" if snapshot or row.get("stage") == "Running Inspect" else "starting")
            worker_stages = {"starting_browser": "Starting browser", "checking_tools": "Checking tools",
                             "running": "Running Inspect", "grading": "Grading", "collecting": "Collecting results"}
            if not ended and self.phase != "stopping" and snapshot.get("phase") in worker_stages:
                row["stage"] = worker_stages[snapshot["phase"]]
            row.update(status=status, turns=(snapshot.get("world") or {}).get("turn", 0),
                       tool_profile=self.config.get("tool_profile", "published"),
                       browser_runtime=self.config.get("browser_runtime", "subprocess"),
                       data_mode=self.config.get("data_mode", "fixture"))
            record = read(child / "run.json", {})
            record.update(status=status, updated_at=now())
            if status == "failed":
                record["error"] = ((error or {}).get("exception_message") or self.error
                                   or "Trial ended without verified results. See the batch console.")
            write(child / "run.json", record)
            logs = sorted(artifacts.glob("*.eval")) if ended else []
            snapshot["inspect_log"] = None
            if logs:
                target = child / logs[-1].name
                if not target.exists():
                    shutil.copyfile(logs[-1], target)
                snapshot["inspect_log"] = str(target)
            if ended and (artifacts / "browser").is_dir():
                shutil.copytree(artifacts / "browser", child / "browser", dirs_exist_ok=True)
            snapshot.update(phase=status, error=record.get("error"), updated_at=now())
            write(child / "snapshot.json", snapshot)
            console = trial / "agent/worker.log"
            if ended and console.is_file() and not (child / "console.log").exists():
                shutil.copyfile(console, child / "console.log")
        counts = {status: sum(row["status"] == status for row in self.rows.values())
                  for status in ("starting", "running", "grading", "completed", "failed", "stopped", "stopping")}
        waiting = self.total - len(self.rows)
        counts["queued"] = 0 if final else waiting
        if final:
            counts["stopped" if self.phase == "stopping" else "failed"] += waiting
        phase = self.phase
        if final:
            phase = "stopped" if phase == "stopping" else "failed" if counts["failed"] or self.error else "completed"
        model_progress = []
        for model in self.models:
            rows = [row for row in self.rows.values() if row["model"] == model]
            model_counts = {status: sum(row["status"] == status for row in rows) for status in counts}
            remaining = self.config["trials"] - len(rows)
            model_counts["queued"] = 0 if final else remaining
            if final:
                model_counts["stopped" if self.phase == "stopping" else "failed"] += remaining
            model_progress.append({"model": model, "total": self.config["trials"], "counts": model_counts})
        write(self.directory / "snapshot.json", {
            "phase": phase, "error": self.error, "updated_at": now(),
            "batch": {"total": self.total, "trials_per_model": self.config["trials"], "models": model_progress, "concurrency": self.config["concurrency"],
                      "environment": self.config["execution"], "counts": counts,
                      "trials": list(self.rows.values())},
        })


async def run_batch(directory: Path) -> int:
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    progress = None
    task = None
    try:
        config = read(directory / "config.json")
        settings = TrialSettings(
            max_turns=config["max_turns"],
            # Batch configs written before the setting existed get the current default,
            # as local dashboard runs do.
            history_tokens=config.get("history_tokens", TrialSettings.model_fields["history_tokens"].default),
            **({"token_budget": config["token_budget"]} if "token_budget" in config else {}),
            tool_profile=config.get("tool_profile", "published"),
            data_mode=config.get("data_mode", "fixture"),
            browser_runtime=config.get("browser_runtime", "subprocess"),
            cost_limit=config["cost_limit"], sim_weeks=config["sim_weeks"],
            time_limit=config.get("time_limit") or 1800,
            **({"research_mode": config["research_mode"]} if "research_mode" in config else {}),
            simulator_model=config["simulator_model"],
            grader_model=config["grader_model"],
            scenario=config.get("scenario", TrialSettings.model_fields["scenario"].default),
            inbox_sha256=config.get("inbox_sha256"),
            resume_sha256=config.get("resume_sha256"),
        )
        inbox = None
        if config.get("inbox_snapshot"):
            from refund_eval.world.snapshot_store import snapshot_path

            inbox = snapshot_path(config["inbox_snapshot"])
        resume = Path(config["resume_log"]) if config.get("resume_log") else None
        path = prepare(directory / "harbor", settings=settings, inbox=inbox, resume=resume,
                       models=batch_models(config),
                       environment=config["execution"], trials=config["trials"], concurrency=config["concurrency"])
        job_config = JobConfig.model_validate_json(path.read_text())
        progress = BatchProgress(directory, job_config.jobs_dir / job_config.job_name)
        progress.refresh()
        if (directory / "KILL").exists() or stopped.is_set():
            progress.phase = "stopping"
            return 0
        job = await Job.create(job_config)
        for event in TrialEvent:
            job.add_hook(event, progress.event)
        task = asyncio.create_task(job.run())
        while not task.done():
            if stopped.is_set() or (directory / "KILL").exists():
                progress.phase = "stopping"
                progress.refresh()
                task.cancel()
                # Harbor's cancellation path collects artifacts and deletes environments.
                # Do not cancel it repeatedly while that cleanup is in progress.
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                break
            progress.refresh()
            await asyncio.wait({task}, timeout=1)
        if not task.cancelled():
            await task
    except Exception as error:
        logger.exception("Harbor batch failed")
        if progress:
            progress.error = str(error)
        else:
            write(directory / "snapshot.json", {"phase": "failed", "error": str(error)})
        return 1
    finally:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Harbor cleanup failed")
        if progress:
            progress.refresh(final=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    return 1 if read(directory / "snapshot.json", {}).get("phase") == "failed" else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    return asyncio.run(run_batch(parser.parse_args().run_dir.resolve()))


if __name__ == "__main__":
    raise SystemExit(main())

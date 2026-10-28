"""Run one dashboard trial in a separate process, publishing local snapshots."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from inspect_ai import eval
from inspect_ai.log import read_eval_log

from refund_eval.config import BUDGET_NOTICES, RunConfig, resolve_cost_limits
from refund_eval.killswitch import KILL_FILE_ENV, kill_requested
from refund_eval.published.provider import published_model_args
from refund_eval.runtime.checkpoint import RunCheckpoint
from refund_eval.runtime.snapshots import (
    EMPTY_SNAPSHOT,
    RUN_DIR_ENV,
    final_snapshot,
    write_snapshot,
)
from refund_eval.task import impossible_refund
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH

logger = logging.getLogger(__name__)


def load_config(run_dir: Path) -> dict[str, Any]:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("mode") != "model":
        raise ValueError("Run mode must be 'model'.")
    if config["mode"] == "model":
        # A model run must say which experiment it is: never back-fill paid role
        # models, or reinterpret an older record as published hybrid.
        missing = [name for name in ("tool_profile", "data_mode", "model", "simulator_model", "grader_model")
                   if name not in config]
        if missing:
            raise ValueError(f"{', '.join(missing)} must be specified for a model run.")
    # Merge only fixed defaults; default_factory fields (the role models) stay explicit.
    validated = RunConfig.model_validate({name: value for name, value in config.items()
                                          if name in RunConfig.model_fields})
    defaults = {name: value for name, value in validated.model_dump().items()
                if RunConfig.model_fields[name].default_factory is None}
    config = {**defaults, **config}
    if config["tool_profile"] != "published":
        raise ValueError("tool_profile must be published.")
    # Execution records from the dashboard carry explicit data mode and runtime.
    if config["data_mode"] not in {"fixture", "hybrid"}:
        raise ValueError("data_mode must be 'fixture' or 'hybrid'.")
    turns = config["max_turns"]
    if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 10000:
        raise ValueError("max_turns must be an integer between 1 and 10000.")
    window = config["history_tokens"]
    if isinstance(window, bool) or not isinstance(window, int) or not 0 <= window <= 1_000_000:
        raise ValueError("history_tokens must be an integer between 0 and 1000000.")
    if config["budget_notice"] not in BUDGET_NOTICES:
        raise ValueError(f"budget_notice must be one of: {', '.join(BUDGET_NOTICES)}.")
    for name in ("sim_weeks",):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number.")
    if config["mode"] == "model":
        for name in ("model", "simulator_model", "grader_model"):
            if not isinstance(config.get(name), str) or not config[name].strip():
                raise ValueError(f"{name} must be specified for a model run.")
    targets = config.get("models") or [config["model"]]
    caps = resolve_cost_limits(config["cost_limit"], targets, scenario=config["scenario"])
    config["cost_limit"] = caps[config["model"]]
    return config


@contextmanager
def worker_environment(run_dir: Path):
    previous = {name: os.environ.get(name) for name in (RUN_DIR_ENV, KILL_FILE_ENV)}
    os.environ[RUN_DIR_ENV] = str(run_dir)
    os.environ.setdefault(KILL_FILE_ENV, str(run_dir / "KILL"))
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def recover_snapshot(destination: Path) -> dict[str, Any]:
    """Recover Inspect's artifact, falling back to the last live snapshot."""
    recovered: dict[str, Any] = {}
    try:
        recovered.update(json.loads((destination / "snapshot.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        logger.warning("Could not recover the live snapshot", exc_info=True)
    logs = sorted(destination.rglob("*.eval"), key=lambda path: path.stat().st_mtime)
    if logs:
        recovered["inspect_log"] = str(logs[-1])
        try:
            saved = final_snapshot(read_eval_log(str(logs[-1])))
            # Recover a checkpoint as one unit: an incomplete final sample must
            # not replace the live tools/observations with empty state while
            # keeping an older world and transcript beside it.
            if ((recovered.get("world") or recovered.get("messages"))
                    and (not saved.get("world") or not saved.get("messages"))):
                for name in RunCheckpoint.model_fields:
                    saved.pop(name, None)
            recovered.update(saved)
        except Exception:
            logger.warning("Could not recover the Inspect log", exc_info=True)
    return recovered


def run_worker(run_dir: str | Path) -> int:
    destination = Path(run_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    write_snapshot(destination, **EMPTY_SNAPSHOT)
    with worker_environment(destination):
        try:
            config = load_config(destination)
            profile_args = {}
            if config.get("inbox_snapshot"):
                from refund_eval.world.snapshot_store import (
                    file_sha256,
                    snapshot_path,
                )

                inbox = snapshot_path(config["inbox_snapshot"])
                if not inbox.is_file() or file_sha256(inbox) != config.get("inbox_sha256"):
                    raise ValueError("The Gmail snapshot changed or was removed after this run was configured.")
                profile_args["fixture_path"] = str(inbox)
            if config["research_mode"] != "auto":
                profile_args["research_mode"] = config["research_mode"]
            from refund_eval.published.profile import (
                fixture_readiness,
                hybrid_readiness,
            )

            profile_args.update(tool_profile="published", published_data=config["data_mode"],
                                artifact_dir=str(destination))
            status = fixture_readiness() if config["data_mode"] == "fixture" else hybrid_readiness()
            if not status["configured"]:
                raise ValueError(f"Published tools are not configured. {status['reason']}")
            logs = eval(
                impossible_refund,
                task_args={
                    "trials": 1,
                    "mail_source": f"fixture:{DEFAULT_FIXTURE_PATH}",
                    "max_turns": config["max_turns"],
                    "history_tokens": config["history_tokens"],
                    "cost_limit": config["cost_limit"],
                    **({"token_budget": config["token_budget"]} if config["token_budget"] is not None else {}),
                    "budget_notice": config["budget_notice"],
                    "sim_weeks": config["sim_weeks"],
                    "scenario": config["scenario"],
                    **({"resume_log": config["resume_log"]} if config.get("resume_log") else {}),
                    **({"time_limit": config["time_limit"]} if "time_limit" in config else {}),
                    **profile_args,
                },
                model=config["model"],
                model_args=published_model_args(config["model"]) if config["tool_profile"] == "published" else {},
                model_roles={"simulator": config["simulator_model"], "grader": config["grader_model"]},
                log_dir=str(destination / "logs"), log_format="eval", display="none",
                metadata={"dashboard": True},
            )
            if not logs:
                recovered = recover_snapshot(destination)
                world = recovered.get("world") or {}
                if (kill_requested() or recovered.get("phase") == "stopped" or world.get("killed")
                        or world.get("stop_reason") in {"kill_switch", "cancelled"}):
                    recovered.update(phase="stopped", error=None)
                    recovered.setdefault("cancellation_reason", world.get("stop_reason") or
                                         "Stop requested before Inspect returned a result.")
                    write_snapshot(destination, **recovered)
                    return 0
                raise RuntimeError("Inspect returned no evaluation result without a recorded cancellation.")
            [log] = logs
            snapshot = write_snapshot(destination, **final_snapshot(log))
            return 1 if snapshot["phase"] == "failed" else 0
        except Exception as error:
            logger.exception("Dashboard run failed")
            # Recover any final artifact even when task creation or eval raised.
            recovered = recover_snapshot(destination)
            recovered.update(phase="stopped" if kill_requested() else "failed", error=str(error))
            write_snapshot(destination, **recovered)
            return 0 if recovered["phase"] == "stopped" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="Run directory containing config.json.")
    return run_worker(parser.parse_args(argv).run_dir)


if __name__ == "__main__":
    raise SystemExit(main())

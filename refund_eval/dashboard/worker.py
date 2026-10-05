"""Run one dashboard trial in a separate process, publishing local snapshots."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from inspect_ai import eval
from inspect_ai.log import read_eval_log

from refund_eval.dashboard.telemetry import EMPTY_SNAPSHOT, RUN_DIR_ENV, write_snapshot
from refund_eval.demo import run_demo
from refund_eval.killswitch import KILL_FILE_ENV, kill_requested
from refund_eval.sim.counterparts import REGISTRY_KEY
from refund_eval.task import impossible_refund
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH
from refund_eval.world.state import WORLD_KEY

logger = logging.getLogger(__name__)


def load_config(run_dir: Path) -> dict[str, Any]:
    config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    if not isinstance(config, dict) or config.get("mode") not in {"demo", "model"}:
        raise ValueError("Run mode must be 'demo' or 'model'.")
    config = {"max_turns": 30, "cost_limit": 5.0, "sim_weeks": 1.0, "tool_profile": "legacy",
              "world_mode": "baseline", **config}
    if config["tool_profile"] not in {"legacy", "published"}:
        raise ValueError("tool_profile must be 'legacy' or 'published'.")
    # Existing published run files predate simulated data support and mean live.
    config.setdefault("data_mode", "live" if config["tool_profile"] == "published" else "fixture")
    if config["data_mode"] not in {"fixture", "hybrid", "live"}:
        raise ValueError("data_mode must be 'fixture', 'hybrid' or 'live'.")
    if config["data_mode"] in {"live", "hybrid"} and config["tool_profile"] != "published":
        raise ValueError(f"{config['data_mode'].title()} data requires the published tool profile.")
    if config["world_mode"] not in {"baseline", "friction"}:
        raise ValueError("world_mode must be 'baseline' or 'friction'.")
    if config["world_mode"] == "friction" and (config["mode"] != "model" or config["data_mode"] == "live"):
        raise ValueError("Realistic friction requires a model trial with simulated Gmail (fixture or hybrid data).")
    if config["mode"] == "demo" and config["tool_profile"] != "legacy":
        raise ValueError("The offline demo uses legacy tools.")
    if "published_config" in config and not isinstance(config["published_config"], str):
        raise ValueError("published_config must be a file path.")
    turns = config["max_turns"]
    if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= 1000:
        raise ValueError("max_turns must be an integer between 1 and 1000.")
    for name in ("cost_limit", "sim_weeks"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number.")
    if config["mode"] == "model":
        for name in ("model", "simulator_model", "grader_model"):
            if not isinstance(config.get(name), str) or not config[name].strip():
                raise ValueError(f"{name} must be specified for a model run.")
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


def final_snapshot(log: Any, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """Use the saved Inspect sample as the authoritative final transcript."""
    sample = log.samples[0] if log.samples else None
    sample_store = sample.store if sample is not None else {}
    world = sample_store.get(WORLD_KEY)
    stopped = bool(world and (world.get("killed") or world.get("stop_reason") == "kill_switch"))
    error = getattr(sample, "error", None) or log.error
    if stopped or log.status == "cancelled":
        phase = "stopped"
    elif log.status != "success" or sample is None or error:
        phase = "failed"
        error = error or f"Inspect run ended with status {log.status}."
    elif summary is not None and not summary["passed"]:
        phase = "failed"
        failed = ", ".join(name for name, passed in summary["checks"].items() if not passed)
        error = f"Demo checks failed: {failed}"
    else:
        phase = "completed"
    result = {
        "phase": phase,
        "error": str(error) if error else None,
        "inspect_log": str(Path(log.location).resolve()) if log.location else None,
        "summary": summary,
    }
    if sample is not None:
        result.update(
            messages=sample.messages,
            world=world,
            counterparts=sample_store.get(REGISTRY_KEY, {}),
            published_tool_profile=sample_store.get("published_tool_profile"),
            scores=sample.scores or {},
        )
    result["grading_warnings"] = [
        f"{name}: {metadata.get('grader_error') or metadata.get('extraction_error')}"
        for name, score in (sample.scores or {}).items()
        if (metadata := score.metadata or {}).get("grader_error") or metadata.get("extraction_error")
    ] if sample is not None else []
    return result


def run_worker(run_dir: str | Path) -> int:
    destination = Path(run_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    write_snapshot(destination, **EMPTY_SNAPSHOT)
    with worker_environment(destination):
        try:
            config = load_config(destination)
            rubric_path = destination / "rubrics.json"
            rubric_args = {}
            if rubric_path.exists():
                from refund_eval.grading.behaviours import BehaviourSnapshot

                rubric_args["rubric_set"] = BehaviourSnapshot.model_validate_json(rubric_path.read_text()).model_dump(mode="json")
            summary = None
            if config["mode"] == "demo":
                summary = run_demo(
                    destination / "logs", step_delay=0.2, max_turns=config["max_turns"],
                    cost_limit=config["cost_limit"], sim_weeks=config["sim_weeks"],
                    time_limit=config.get("time_limit"),
                    **rubric_args,
                )
                log = read_eval_log(summary["inspect_log"])
            else:
                profile_args = {}
                experiment = None
                if config.get("experiment_manifest"):
                    from refund_eval.experiments.protocol import experiment_settings

                    experiment = experiment_settings(config["experiment_manifest"], config)
                    profile_args.update(scenario=experiment["scenario_path"], fixture_path=experiment["inbox_path"])
                    if experiment.get("startup_context", {}).get("mode") == "matched_thread_prefetch":
                        profile_args["initial_mail_thread_ids"] = experiment["startup_context"]["thread_ids"]
                if config["world_mode"] != "baseline":
                    profile_args["world_mode"] = config["world_mode"]
                if config["tool_profile"] == "published":
                    from refund_eval.published.profile import (
                        DEFAULT_CONFIG,
                        fixture_readiness,
                        hybrid_readiness,
                        readiness,
                    )

                    profile_args.update(tool_profile="published", published_data=config["data_mode"],
                                        artifact_dir=str(destination))
                    if config["data_mode"] == "fixture":
                        status = fixture_readiness()
                    elif config["data_mode"] == "hybrid":
                        status = hybrid_readiness()
                    else:
                        published_path = config.get("published_config") or os.environ.get(
                            "REFUND_EVAL_PUBLISHED_CONFIG", DEFAULT_CONFIG
                        )
                        status = readiness(published_path)
                        profile_args["published_config"] = published_path
                    if not status["configured"]:
                        raise ValueError(f"Published tools are not configured. {status['reason']}")
                model_names = [config[key] for key in ("model", "simulator_model", "grader_model")]
                budget_scope = nullcontext()
                budget_configuration = None
                if config.get("all_role_cost_limit") is not None:
                    from refund_eval.experiments.budget import (
                        all_role_budget,
                        configure_budget_models,
                    )

                    frozen = json.loads(Path(config["experiment_budget_file"]).read_text())
                    budget_configuration = configure_budget_models(model_names, frozen=frozen)
                    budget_scope = all_role_budget(
                        config["all_role_cost_limit"], model_names, destination / "budget.json",
                        configuration=budget_configuration,
                    )
                elif experiment and not all(name.startswith("mockllm/") for name in model_names):
                    raise ValueError("Paid comparison trials require an all-role budget.")
                with budget_scope:
                    [log] = eval(
                        impossible_refund,
                        task_args={
                            "trials": 1,
                            "mail_source": f"fixture:{DEFAULT_FIXTURE_PATH}",
                            "max_turns": config["max_turns"],
                            "cost_limit": config["cost_limit"],
                            "sim_weeks": config["sim_weeks"],
                            **({"time_limit": config["time_limit"]} if "time_limit" in config else {}),
                            **rubric_args,
                            **profile_args,
                        },
                        model=config["model"],
                        model_roles={"simulator": config["simulator_model"], "grader": config["grader_model"]},
                        log_dir=str(destination / "logs"), log_format="eval", display="none",
                        metadata={"dashboard": True, "world_mode": config["world_mode"],
                                  **({"experiment": experiment} if experiment else {}),
                                  **({"all_role_budget": budget_configuration,
                                      "all_role_cost_limit": config["all_role_cost_limit"]}
                                     if budget_configuration else {})},
                    )
            snapshot = write_snapshot(destination, **final_snapshot(log, summary))
            return 1 if snapshot["phase"] == "failed" else 0
        except Exception as error:
            logger.exception("Dashboard run failed")
            # Recover any final artifact even when task creation or eval raised.
            logs = sorted(destination.rglob("*.eval"), key=lambda path: path.stat().st_mtime)
            recovered: dict[str, Any] = {}
            if logs:
                recovered["inspect_log"] = str(logs[-1])
                try:
                    recovered.update(final_snapshot(read_eval_log(str(logs[-1]))))
                except Exception:
                    logger.warning("Could not recover the Inspect log", exc_info=True)
            recovered.update(phase="stopped" if kill_requested() else "failed", error=str(error))
            write_snapshot(destination, **recovered)
            return 0 if recovered["phase"] == "stopped" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="Run directory containing config.json.")
    return run_worker(parser.parse_args(argv).run_dir)


if __name__ == "__main__":
    raise SystemExit(main())

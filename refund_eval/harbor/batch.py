"""Prepare a private native Harbor batch from a RunConfig JSON file. Launches nothing."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import tomllib
from pathlib import Path

import yaml

from refund_eval.config import RunConfig, resolve_cost_limits
from refund_eval.harbor.adapter import AGENT_IMPORT_PATH, validate_and_index, write_task
from refund_eval.output_paths import validate_private_output


def prepare(out: Path, config: RunConfig) -> Path:
    targets = config.models or [config.model]
    if config.execution not in {"docker", "modal"}:
        raise ValueError("Native batches require execution='docker' or execution='modal'")
    if config.inbox_snapshot or config.resume_log:
        raise ValueError("Native batch preparation does not support an inbox snapshot or a resume log")
    total = config.trials * len(targets)
    if total > 100 or config.concurrency > total:
        raise ValueError("A batch allows at most 100 trials and concurrency cannot exceed the trial count")
    caps = resolve_cost_limits(config.cost_limit, targets, scenario=config.scenario)
    out = validate_private_output(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"Native batches require an empty output directory: {out}")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)

    task = write_task(
        out, scenario=config.scenario, data_mode=config.data_mode, sim_weeks=config.sim_weeks,
        max_turns=config.max_turns, research_mode=config.research_mode, simulator_model=config.simulator_model,
        grader_model=config.grader_model, agent_timeout=1.1 * (config.time_limit or 36_000),
        verifier_timeout=600, cpus=2, memory_mb=4096, world_seed="" if config.seed is None else str(config.seed),
    )
    options = config.model_dump(include={"token_budget", "history_tokens", "budget_notice", "time_limit"},
                                exclude_none=True)
    job = {
        "job_name": f"refund-native-{config.scenario}",
        "jobs_dir": str(out / "jobs"),
        "n_attempts": config.trials,
        "n_concurrent_trials": config.concurrency,
        "retry": {"max_retries": 0},
        "environment": {"type": config.execution, "delete": True},
        "agents": [
            {"import_path": AGENT_IMPORT_PATH, "model_name": model, "kwargs": {**options, "cost_limit": caps[model]},
             "env": {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"}}
            for model in targets
        ],
        "tasks": [{"path": str(task)}],
    }
    task_digest = validate_and_index(out, task, job)
    job_path = out / "job.yaml"
    job_bytes = yaml.safe_dump(job, sort_keys=False).encode()
    job_path.write_bytes(job_bytes)
    metadata = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))["metadata"]
    record = out / "run.json"
    record.write_text(json.dumps({
        "schema_version": 1,
        "config": config.model_dump(mode="json"),
        "task": {"path": task.name, "digest": task_digest, "version": metadata["task_version"],
                 "environment_version": metadata["environment_version"], "environment_fingerprint": metadata["environment_fingerprint"],
                 "source_sha256": metadata["source_sha256"], "rules_sha256": metadata["rules_sha256"]},
        "job": {"path": job_path.name, "sha256": hashlib.sha256(job_bytes).hexdigest(), "max_retries": 0},
        "seed_policy": metadata["seed_policy"],
        "total_trials": total,
        "target_cost_limits_usd": caps,
    }, indent=2) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="RunConfig JSON with Docker or Modal execution")
    parser.add_argument("--out", type=Path, required=True, help="Empty, Git-ignored or external output directory")
    args = parser.parse_args(argv)
    try:
        config = RunConfig.model_validate_json(args.config.read_bytes())
        record = prepare(args.out, config)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Prepared {record}")
    command = ["scripts/harbor", "harbor", "run", "-c", str(record.with_name("job.yaml"))]
    print(f"Paid launch (target caps only; world models and compute cost extra): {shlex.join(command)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

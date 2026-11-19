"""Prepare a private native Harbor batch from a RunConfig JSON file. Launches nothing."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import tomllib
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from refund_eval.config import CostCap, RunConfig, resolve_cost_limits
from refund_eval.harbor.adapter import (
    AGENT_IMPORT_PATH,
    read_verified_agent_wheel,
    validate_and_index,
    write_task,
)
from refund_eval.output_paths import validate_private_output


class NativeBatchConfig(RunConfig):
    world_cost_limit: CostCap
    agent_wheel: Path
    agent_wheel_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    time_limit: int = Field(default=36_000, ge=1, le=72000, strict=True)
    trial_spend_cap_usd: CostCap | None = None
    key_spend_cap_usd: CostCap | None = None

    @model_validator(mode="after")
    def _validate_spend_caps(self) -> NativeBatchConfig:
        if (self.trial_spend_cap_usd is None) != (self.key_spend_cap_usd is None):
            raise ValueError("Set trial_spend_cap_usd and key_spend_cap_usd together")
        if self.trial_spend_cap_usd is None:
            return self
        missing = {"max_turns", "time_limit"} - self.model_fields_set
        if missing:
            raise ValueError(f"Spend caps need an explicit {' and '.join(sorted(missing))}")
        return self


def prepare(out: Path, config: NativeBatchConfig) -> Path:
    verified_wheel = read_verified_agent_wheel(config.agent_wheel, config.agent_wheel_sha256)
    if config.identity_lock is not None:
        raise ValueError(
            "Native Harbor does not support exact browser artifact locks; "
            "the generated sidecar task records its own source and rules identities."
        )
    targets = config.models or [config.model]
    if config.execution not in {"docker", "modal"}:
        raise ValueError("Native batches require execution='docker' or execution='modal'")
    if config.inbox_snapshot or config.resume_log:
        raise ValueError("Native batch preparation does not support an inbox snapshot or a resume log")
    total = config.trials * len(targets)
    if total > 100 or config.concurrency > total:
        raise ValueError("A batch allows at most 100 trials and concurrency cannot exceed the trial count")
    caps = resolve_cost_limits(config.cost_limit, targets, scenario=config.scenario)
    limits = None
    if config.trial_spend_cap_usd is not None:
        trial_cap = Decimal(str(config.trial_spend_cap_usd))
        world_cap = Decimal(str(config.world_cost_limit))
        over = [model for model in targets if Decimal(str(caps[model])) + world_cap > trial_cap]
        if over:
            raise ValueError(f"Target cap plus world allowance exceeds trial_spend_cap_usd for: {', '.join(over)}")
        if total * trial_cap > Decimal(str(config.key_spend_cap_usd)):
            raise ValueError(f"{total} attempts x trial_spend_cap_usd {config.trial_spend_cap_usd} "
                             f"exceeds key_spend_cap_usd {config.key_spend_cap_usd}")
        limits = {"max_turns": config.max_turns, "time_limit_seconds": config.time_limit,
                  "trial_spend_cap_usd": config.trial_spend_cap_usd,
                  "key_spend_cap_usd": config.key_spend_cap_usd}
    out = validate_private_output(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"Native batches require an empty output directory: {out}")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)

    task = write_task(
        out, scenario=config.scenario, data_mode=config.data_mode, sim_weeks=config.sim_weeks,
        max_turns=config.max_turns, research_mode=config.research_mode, simulator_model=config.simulator_model,
        grader_model=config.grader_model, world_cost_limit=config.world_cost_limit, agent_timeout=1.1 * config.time_limit,
        verifier_timeout=600, cpus=2, memory_mb=4096, world_seed="" if config.seed is None else str(config.seed),
        agent_wheel=config.agent_wheel, agent_wheel_sha256=config.agent_wheel_sha256,
        verified_wheel=verified_wheel,
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
                 "source_sha256": metadata["source_sha256"], "rules_sha256": metadata["rules_sha256"],
                 "agent_wheel_filename": metadata["agent_wheel_filename"],
                 "agent_wheel_sha256": metadata["agent_wheel_sha256"]},
        "job": {"path": job_path.name, "sha256": hashlib.sha256(job_bytes).hexdigest(), "max_retries": 0},
        "seed_policy": metadata["seed_policy"],
        "total_trials": total,
        "target_cost_limits_usd": caps,
        "world_cost_limit_usd_per_trial": config.world_cost_limit,
        **({"limits": limits} if limits is not None else {}),
    }, indent=2) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="RunConfig JSON with Docker or Modal execution")
    parser.add_argument("--out", type=Path, required=True, help="Empty, Git-ignored or external output directory")
    args = parser.parse_args(argv)
    try:
        config = NativeBatchConfig.model_validate_json(args.config.read_bytes())
        record = prepare(args.out, config)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(f"Prepared {record}")
    command = ["scripts/harbor", "harbor", "run", "-c", str(record.with_name("job.yaml"))]
    print(f"Paid launch (separate target and world model budgets; compute costs extra): {shlex.join(command)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

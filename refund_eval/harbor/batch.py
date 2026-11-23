"""Prepare a private native Harbor batch from a RunConfig JSON file. Launches nothing."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from refund_eval.config import CostCap, RunConfig, resolve_cost_limits
from refund_eval.harbor.adapter import (
    AGENT_IMPORT_PATH,
    read_verified_agent_wheel,
    validate_and_index,
    verify_split_budget_task_sources,
    verify_split_budget_wheel,
    write_task,
)
from refund_eval.output_paths import validate_private_output


class NativeBatchConfig(RunConfig):
    world_cost_limit: CostCap
    agent_wheel: Path
    agent_wheel_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    time_limit: int = Field(default=36_000, ge=1, le=72000, strict=True)
    total_time_limit_seconds: int | None = Field(default=None, ge=1, strict=True)
    openrouter_key_mode: Literal["shared", "split", "shared_roles"] = "shared"
    agent_max_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    shared_allowance_usd: CostCap | None = None
    trial_spend_cap_usd: CostCap | None = None
    key_spend_cap_usd: CostCap | None = None

    @model_validator(mode="after")
    def _validate_spend_caps(self) -> NativeBatchConfig:
        if (self.trial_spend_cap_usd is None) != (self.key_spend_cap_usd is None):
            raise ValueError("Set trial_spend_cap_usd and key_spend_cap_usd together")
        if self.openrouter_key_mode in {"split", "shared_roles"}:
            mode = self.openrouter_key_mode
            if self.scenario != "train_refund_late":
                raise ValueError(f"{mode} mode only supports train_refund_late")
            if self.execution != "modal":
                raise ValueError(f"{mode} mode requires Modal execution")
            if self.agent_max_output_tokens is None:
                raise ValueError(f"{mode} mode requires agent_max_output_tokens")
            targets = self.models or [self.model]
            if self.trials != 1 or self.concurrency != 1 or len(targets) != 1:
                raise ValueError(f"{mode} mode requires one target attempt and concurrency one")
            if self.trial_spend_cap_usd is not None:
                raise ValueError(f"{mode} mode does not support combined planning caps")
            roles = [targets[0], self.simulator_model, self.grader_model]
            if any(not isinstance(model, str) or not model.startswith("openrouter/") for model in roles):
                raise ValueError(f"{mode} mode requires OpenRouter models for all roles")
            agent = Decimal(str(resolve_cost_limits(self.cost_limit, targets, scenario=self.scenario)[targets[0]]))
            world = Decimal(str(self.world_cost_limit))
            if agent <= 0 or agent > 50 or world <= 0 or world > 100:
                raise ValueError(f"{mode} role caps exceed supported $50/$100 ceilings")
            if mode == "shared_roles":
                if self.shared_allowance_usd is None:
                    raise ValueError("shared_roles mode requires shared_allowance_usd")
                allowance = Decimal(str(self.shared_allowance_usd))
                if allowance <= 0 or allowance > min(Decimal(150), agent + world):
                    raise ValueError("shared_allowance_usd must be positive and no greater than the role caps or $150")
            elif "shared_allowance_usd" in self.model_fields_set:
                raise ValueError("shared_allowance_usd is only valid in shared_roles mode")
        elif self.agent_max_output_tokens is not None:
            raise ValueError("agent_max_output_tokens is only valid in strict role-budget modes")
        elif "shared_allowance_usd" in self.model_fields_set:
            raise ValueError("shared_allowance_usd is only valid in shared_roles mode")
        if self.trial_spend_cap_usd is None:
            return self
        missing = {"max_turns", "time_limit"} - self.model_fields_set
        if missing:
            raise ValueError(f"Spend caps need an explicit {' and '.join(sorted(missing))}")
        return self


def prepare(out: Path, config: NativeBatchConfig) -> Path:
    if config.total_time_limit_seconds is not None:
        raise ValueError("strict_total_deadline_unsupported_by_modal")
    verified_wheel = read_verified_agent_wheel(config.agent_wheel, config.agent_wheel_sha256)
    if config.openrouter_key_mode in {"split", "shared_roles"}:
        verify_split_budget_wheel(config.agent_wheel)
    targets = config.models or [config.model]
    if config.execution not in {"docker", "modal"}:
        raise ValueError("Native batches require execution='docker' or execution='modal'")
    if config.inbox_snapshot or config.resume_log:
        raise ValueError("Native batch preparation does not support an inbox snapshot or a resume log")
    total = config.trials * len(targets)
    if total > 100 or config.concurrency > total:
        raise ValueError("A batch allows at most 100 trials and concurrency cannot exceed the trial count")
    caps = resolve_cost_limits(config.cost_limit, targets, scenario=config.scenario)
    shared_role_budget_config = None
    if config.openrouter_key_mode == "shared_roles":
        shared_role_budget_config = json.dumps({
            "schema_version": 1,
            "shared_allowance_usd": str(config.shared_allowance_usd),
            "role_limits_usd": {"target": str(caps[targets[0]]), "world": str(config.world_cost_limit)},
            "scenario": config.scenario,
            "models": {"target": targets[0], "simulator": config.simulator_model, "grader": config.grader_model},
        }, sort_keys=True, separators=(",", ":"))
    worker_time_limit = config.time_limit
    agent_timeout = 1.1 * config.time_limit
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
        limits = {"max_turns": config.max_turns, "time_limit_seconds": worker_time_limit,
                  "trial_spend_cap_usd": config.trial_spend_cap_usd,
                  "key_spend_cap_usd": config.key_spend_cap_usd}
    out = validate_private_output(out)
    if out.exists() and any(out.iterdir()):
        raise FileExistsError(f"Native batches require an empty output directory: {out}")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)

    task = write_task(
        out, scenario=config.scenario, data_mode=config.data_mode, sim_weeks=config.sim_weeks,
        max_turns=config.max_turns, research_mode=config.research_mode, simulator_model=config.simulator_model,
        grader_model=config.grader_model, world_cost_limit=config.world_cost_limit, agent_timeout=agent_timeout,
        verifier_timeout=600, cpus=2, memory_mb=4096, world_seed="" if config.seed is None else str(config.seed),
        agent_wheel=config.agent_wheel, agent_wheel_sha256=config.agent_wheel_sha256,
        verified_wheel=verified_wheel, openrouter_key_mode=config.openrouter_key_mode,
        shared_allowance_usd=config.shared_allowance_usd, shared_role_budget_config=shared_role_budget_config,
    )
    if config.openrouter_key_mode in {"split", "shared_roles"}:
        verify_split_budget_task_sources(task)
    options = config.model_dump(include={"token_budget", "history_tokens", "budget_notice"}, exclude_none=True)
    options["time_limit"] = worker_time_limit
    job = {
        "job_name": f"refund-native-{config.scenario}",
        "jobs_dir": str(out / "jobs"),
        "n_attempts": config.trials,
        "n_concurrent_trials": config.concurrency,
        "retry": {"max_retries": 0},
        "environment": {"type": config.execution, "delete": True},
        "agents": [
            {"import_path": AGENT_IMPORT_PATH, "model_name": model, "kwargs": {**options, "cost_limit": caps[model]},
             "env": ({"OPENROUTER_API_KEY": "${OPENROUTER_AGENT_API_KEY}",
                      "REFUND_EVAL_AGENT_DOLLAR_CAP": str(caps[model]),
                      "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS": str(config.agent_max_output_tokens)}
                     if config.openrouter_key_mode == "split" else
                     {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
                      "REFUND_EVAL_AGENT_DOLLAR_CAP": str(caps[model]),
                      "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS": str(config.agent_max_output_tokens),
                      "REFUND_EVAL_SHARED_ROLE_MODE": "shared_roles",
                      "REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG": shared_role_budget_config}
                     if config.openrouter_key_mode == "shared_roles" else
                     {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"})}
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
    effective_parameters = {
        "models": targets,
        "simulator_model": config.simulator_model,
        "grader_model": config.grader_model,
        "sim_weeks": config.sim_weeks,
        "max_turns": config.max_turns,
        "agent_cost_limits_usd": caps,
        "world_cost_limit_usd": config.world_cost_limit,
        "worker_time_limit_seconds": worker_time_limit,
        "requested_total_time_limit_seconds": None,
        "effective_enforced_total_time_limit_seconds": None,
        "total_time_limit_enforcement": "not_requested",
    }
    if config.openrouter_key_mode == "split":
        effective_parameters.update({
            "openrouter_key_mode": "split",
            "agent_max_output_tokens": config.agent_max_output_tokens,
            "agent_key_ceiling_usd": 50,
            "world_key_ceiling_usd": 100,
        })
    elif config.openrouter_key_mode == "shared_roles":
        effective_parameters.update({
            "openrouter_key_mode": "shared_roles",
            "agent_max_output_tokens": config.agent_max_output_tokens,
            "shared_allowance_usd": str(config.shared_allowance_usd),
            "role_caps_usd": {"target": str(caps[targets[0]]), "world": str(config.world_cost_limit)},
            "shared_allowance_is_operator_attested": True,
        })
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
        "effective_parameters": effective_parameters,
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
    if config.openrouter_key_mode in {"split", "shared_roles"}:
        manifest = record.with_name("laura-setup.json")
        job_name = f"laura-native-{record.parent.name}"
        commands = (
            ["scripts/harbor", "python", "-m", "scripts.laura_trial", "save", "--config",
             str(record.with_name("job.yaml")), "--manifest", str(manifest)],
            ["scripts/harbor", "python", "-m", "scripts.laura_trial", "verify", "--manifest", str(manifest)],
            ["scripts/harbor", "python", "-m", "scripts.laura_trial", "run", "--manifest", str(manifest),
             "--job-name", job_name],
        )
        if config.openrouter_key_mode == "split":
            print("Guarded split launch recipe; set OPENROUTER_AGENT_API_KEY and OPENROUTER_WORLD_API_KEY first:")
        else:
            print("Guarded shared_roles launch recipe; set the ordinary OPENROUTER_API_KEY and verify its available allocation")
        for command in commands:
            print(shlex.join(command))
    else:
        command = ["scripts/harbor", "harbor", "run", "-c", str(record.with_name("job.yaml"))]
        print(f"Paid launch (separate target and world model budgets; compute costs extra): {shlex.join(command)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

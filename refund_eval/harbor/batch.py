"""Prepare a private native Harbor batch from a RunConfig JSON file. Launches nothing."""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from refund_eval.config import CostCap, RunConfig, resolve_cost_limits
from refund_eval.harbor.adapter import (
    AGENT_IMPORT_PATH,
    read_verified_agent_wheel,
    validate_and_index,
    verify_split_budget_task_sources,
    verify_split_budget_wheel,
    verify_suite_task_wheel,
    write_task,
)
from refund_eval.models.shared_role_spend_ledger import (
    SHARED_CAPS_HEADROOM_BOUND_ENV,
    WORLD_ROLE_CAP_CEILING_USD,
)
from refund_eval.output_paths import validate_private_output
from refund_eval.scenario.schema import scenario_names

GUARDED_SCENARIOS = frozenset({"daycare_reimbursement", "train_refund_late"})


class NativeSuiteEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    task_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    scenario: str
    task_text: str = Field(min_length=1, max_length=4000)
    sim_weeks: float | None = Field(default=None, gt=0)


class NativeBatchConfig(RunConfig):
    world_cost_limit: CostCap
    agent_wheel: Path
    agent_wheel_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    time_limit: int = Field(default=36_000, ge=1, le=72000, strict=True)
    total_time_limit_seconds: int | None = Field(default=None, ge=1, strict=True)
    openrouter_key_mode: Literal["shared", "split", "shared_roles", "shared_caps"] = "shared"
    agent_max_output_tokens: int | None = Field(default=None, ge=1, strict=True)
    usage_reporting_mode: Literal["strict", "usage_only"] = "strict"
    disable_application_price_cutoffs: bool = False
    disable_verifier: bool = False
    requested_deadline: date | None = None
    modal_region: str | None = Field(default="uk", min_length=1, pattern=r"^[a-z0-9-]+$")
    shared_allowance_usd: Annotated[float, Field(gt=0, strict=True, allow_inf_nan=False)] | None = None
    trial_spend_cap_usd: CostCap | None = None
    key_spend_cap_usd: CostCap | None = None
    suite: list[NativeSuiteEntry] | None = Field(default=None, min_length=10, max_length=10)

    @model_validator(mode="after")
    def _validate_spend_caps(self) -> NativeBatchConfig:
        if self.usage_reporting_mode == "usage_only" and self.openrouter_key_mode != "shared_caps":
            raise ValueError("usage_only reporting requires shared_caps outer key monitoring")
        if self.disable_application_price_cutoffs and (
            self.usage_reporting_mode != "usage_only" or self.openrouter_key_mode != "shared_caps"
        ):
            raise ValueError("disabling application price cutoffs requires usage_only shared_caps mode")
        if self.requested_deadline is not None and self.openrouter_key_mode not in {"shared_roles", "shared_caps"}:
            raise ValueError("requested_deadline is only valid in a guarded shared-key mode")
        if self.suite is not None:
            task_ids = [entry.task_id for entry in self.suite]
            task_texts = [entry.task_text.strip() for entry in self.suite]
            scenarios = {entry.scenario for entry in self.suite}
            if len(set(task_ids)) != len(task_ids):
                raise ValueError("Native suite task_id values must be unique")
            if any(not task_text for task_text in task_texts):
                raise ValueError("Native suite task_text values must not be blank")
            if len(set(task_texts)) != len(task_texts):
                raise ValueError("Native suite task_text values must be unique")
            unknown = scenarios - set(scenario_names())
            if unknown:
                raise ValueError(f"Unknown suite scenarios: {', '.join(sorted(unknown))}")
            if self.scenario not in scenarios:
                raise ValueError("Native suite must include the configured scenario")
            if self.openrouter_key_mode in {"split", "shared_roles", "shared_caps"}:
                raise ValueError("Native task suites do not support strict role-budget key modes")
            if "train_refund_late" in scenarios and "cost_limit" not in self.model_fields_set:
                raise ValueError("A suite containing train_refund_late requires an explicit cost_limit")
            if "train_refund_late" in scenarios and self.research_mode not in {"saved", "auto"}:
                raise ValueError(
                    "train_refund_late suite entries require research_mode 'saved' or 'auto' (saved dossiers)"
                )
            targets = self.models or [self.model]
            for scenario in scenarios:
                resolve_cost_limits(self.cost_limit, targets, scenario=scenario)
        if (self.trial_spend_cap_usd is None) != (self.key_spend_cap_usd is None):
            raise ValueError("Set trial_spend_cap_usd and key_spend_cap_usd together")
        if self.openrouter_key_mode in {"split", "shared_roles", "shared_caps"}:
            mode = self.openrouter_key_mode
            if self.scenario not in GUARDED_SCENARIOS:
                raise ValueError(
                    f"{mode} mode supports only guarded scenarios: {', '.join(sorted(GUARDED_SCENARIOS))}"
                )
            if self.scenario == "daycare_reimbursement" and "cost_limit" not in self.model_fields_set:
                raise ValueError("daycare_reimbursement strict role mode requires an explicit cost_limit")
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
            if mode == "split":
                if agent <= 0 or agent > 50 or world <= 0 or world > 100:
                    raise ValueError(f"{mode} role caps exceed supported $50/$100 ceilings")
            else:
                if agent <= 0 or world <= 0 or world > WORLD_ROLE_CAP_CEILING_USD:
                    raise ValueError(f"{mode} world cap exceeds supported ${WORLD_ROLE_CAP_CEILING_USD} ceiling")
            if mode == "shared_caps" and world != WORLD_ROLE_CAP_CEILING_USD:
                raise ValueError(f"shared_caps mode requires a ${WORLD_ROLE_CAP_CEILING_USD} world role cap")
            if mode == "shared_roles":
                if self.shared_allowance_usd is None:
                    raise ValueError("shared_roles mode requires shared_allowance_usd")
                allowance = Decimal(str(self.shared_allowance_usd))
                if allowance <= 0 or allowance > agent + world:
                    raise ValueError("shared_allowance_usd must be positive and no greater than the role caps")
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
    if config.suite is not None:
        verify_suite_task_wheel(verified_wheel, {entry.scenario for entry in config.suite})
    if config.openrouter_key_mode in {"split", "shared_roles", "shared_caps"}:
        verify_split_budget_wheel(config.agent_wheel)
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
    task_specs = ([
        {"task_id": entry.task_id, "scenario": entry.scenario, "task_text": entry.task_text,
         "sim_weeks": entry.sim_weeks if entry.sim_weeks is not None else config.sim_weeks,
         "suite_task": True}
        for entry in config.suite
    ] if config.suite is not None else [{
        "task_id": config.scenario, "scenario": config.scenario, "task_text": None,
        "sim_weeks": config.sim_weeks, "suite_task": False,
    }])
    total = config.trials * len(targets) * len(task_specs)
    if total > 100 or config.concurrency > total:
        raise ValueError("A batch allows at most 100 trials and concurrency cannot exceed the trial count")
    caps_by_scenario = {
        scenario: resolve_cost_limits(config.cost_limit, targets, scenario=scenario)
        for scenario in {spec["scenario"] for spec in task_specs}
    }
    caps = caps_by_scenario[config.scenario]
    shared_role_budget_config = None
    shared_allowance = None
    if config.openrouter_key_mode in {"shared_roles", "shared_caps"}:
        shared_allowance = (
            config.shared_allowance_usd
            if config.openrouter_key_mode == "shared_roles"
            else Decimal(format((Decimal(str(caps[targets[0]])) + Decimal(str(config.world_cost_limit))).normalize(), "f"))
        )
        shared_role_budget_config = json.dumps({
            "schema_version": 1,
            "shared_allowance_usd": str(shared_allowance),
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

    tasks = [write_task(
        out, scenario=spec["scenario"], task_id=spec["task_id"], task_text=spec["task_text"],
        data_mode=config.data_mode, sim_weeks=spec["sim_weeks"], max_turns=config.max_turns,
        research_mode=config.research_mode, simulator_model=config.simulator_model,
        grader_model=config.grader_model, world_cost_limit=config.world_cost_limit, agent_timeout=agent_timeout,
        verifier_timeout=600, cpus=2, memory_mb=4096, world_seed="" if config.seed is None else str(config.seed),
        agent_wheel=config.agent_wheel, agent_wheel_sha256=config.agent_wheel_sha256,
        verified_wheel=verified_wheel, openrouter_key_mode=config.openrouter_key_mode,
        usage_reporting_mode=config.usage_reporting_mode,
        disable_application_price_cutoffs=config.disable_application_price_cutoffs,
        shared_allowance_usd=shared_allowance, shared_role_budget_config=shared_role_budget_config,
        requested_deadline=config.requested_deadline,
        suite_task=spec["suite_task"],
    ) for spec in task_specs]
    if config.openrouter_key_mode in {"split", "shared_roles", "shared_caps"}:
        for task in tasks:
            verify_split_budget_task_sources(task)
    options = config.model_dump(
        include={"token_budget", "history_tokens", "budget_notice", "requested_deadline"}, exclude_none=True,
    )
    options["time_limit"] = worker_time_limit
    environment: dict[str, object] = {"type": config.execution, "delete": True}
    if config.execution == "modal" and config.modal_region is not None:
        environment["kwargs"] = {"region": config.modal_region}
    job = {
        "job_name": (f"refund-native-{config.scenario}" if config.suite is None else "refund-native-long-horizon-10"),
        "jobs_dir": str(out / "jobs"),
        "n_attempts": config.trials,
        "n_concurrent_trials": config.concurrency,
        "retry": {"max_retries": 0},
        "environment": environment,
        "agents": [
            {"import_path": AGENT_IMPORT_PATH, "model_name": model,
             "kwargs": {**options, "cost_limit": caps[model],
                        **({"forward_task_instruction": True} if config.suite is not None else {})},
             "env": ({"OPENROUTER_API_KEY": "${OPENROUTER_AGENT_API_KEY}",
                      "REFUND_EVAL_AGENT_DOLLAR_CAP": str(caps[model]),
                      "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS": str(config.agent_max_output_tokens),
                      "REFUND_EVAL_USAGE_REPORTING_MODE": config.usage_reporting_mode}
                     if config.openrouter_key_mode == "split" else
                     {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}",
                      "REFUND_EVAL_AGENT_DOLLAR_CAP": str(caps[model]),
                      "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS": str(config.agent_max_output_tokens),
                      "REFUND_EVAL_USAGE_REPORTING_MODE": config.usage_reporting_mode,
                      "REFUND_EVAL_SHARED_ROLE_MODE": config.openrouter_key_mode,
                      "REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG": shared_role_budget_config,
                      **({SHARED_CAPS_HEADROOM_BOUND_ENV: "false"}
                         if config.openrouter_key_mode == "shared_caps" else {})}
                     if config.openrouter_key_mode in {"shared_roles", "shared_caps"} else
                     {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"})}
            for model in targets
        ],
        "tasks": [{"path": str(task)} for task in tasks],
    }
    if config.disable_verifier:
        job["verifier"] = {"disable": True}
    task_digest = validate_and_index(out, tasks[0], job)
    from harbor.publisher.packager import Packager

    task_records = []
    for task in tasks:
        metadata = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))["metadata"]
        digest, _ = Packager.compute_content_hash(task)
        task_records.append({
            "path": task.name,
            "digest": f"sha256:{digest}",
            "version": metadata["task_version"],
            "environment_version": metadata["environment_version"],
            "environment_fingerprint": metadata["environment_fingerprint"],
            "source_sha256": metadata["source_sha256"],
            "rules_sha256": metadata["rules_sha256"],
            "agent_wheel_filename": metadata["agent_wheel_filename"],
            "agent_wheel_sha256": metadata["agent_wheel_sha256"],
            **({"task_id": metadata["task_id"], "scenario": metadata["scenario"]}
               if config.suite is not None else {}),
        })
    if task_records[0]["digest"] != task_digest:
        raise ValueError("Native task digest changed during Harbor indexing")
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
        "usage_reporting_mode": config.usage_reporting_mode,
        **({"modal_region": config.modal_region} if config.execution == "modal" else {}),
    }
    if config.usage_reporting_mode == "usage_only":
        effective_parameters["cost_limit_enforcement"] = "response_reported_observed_not_pre_call"
    if config.disable_application_price_cutoffs:
        effective_parameters.update({
            "agent_cost_limits_usd": None,
            "world_cost_limit_usd": None,
            "cost_limit_enforcement": "application_price_cutoffs_disabled",
            "effective_application_price_cutoffs_usd": {"agent": None, "world": None},
            "shared_caps_guard_role_limits_usd": {
                "agent": caps[targets[0]], "world": config.world_cost_limit,
            },
        })
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
        if config.requested_deadline is not None:
            effective_parameters["requested_deadline"] = config.requested_deadline.isoformat()
    elif config.openrouter_key_mode == "shared_caps":
        effective_parameters.update({
            "openrouter_key_mode": "shared_caps",
            "agent_max_output_tokens": config.agent_max_output_tokens,
            "shared_aggregate_ceiling_usd": str(shared_allowance),
            "shared_allowance_source": "verified_key_headroom",
            "runtime_metadata_confirmed": False,
        })
        if not config.disable_application_price_cutoffs:
            effective_parameters["role_caps_usd"] = {
                "target": str(caps[targets[0]]), "world": str(config.world_cost_limit),
            }
        if config.requested_deadline is not None:
            effective_parameters["requested_deadline"] = config.requested_deadline.isoformat()
    record_data = {
        "schema_version": 1,
        "config": config.model_dump(mode="json"),
        **({"task": task_records[0]} if config.suite is None else {"tasks": task_records}),
        "job": {"path": job_path.name, "sha256": hashlib.sha256(job_bytes).hexdigest(), "max_retries": 0},
        "seed_policy": tomllib.loads((tasks[0] / "task.toml").read_text(encoding="utf-8"))["metadata"]["seed_policy"],
        "total_trials": total,
        "target_cost_limits_usd": None if config.disable_application_price_cutoffs else caps,
        "world_cost_limit_usd_per_trial": None if config.disable_application_price_cutoffs else config.world_cost_limit,
        "effective_parameters": effective_parameters,
        **({"shared_caps_guard_role_limits_usd": {
            "agent": caps[targets[0]], "world": config.world_cost_limit,
        }} if config.disable_application_price_cutoffs else {}),
        **({"limits": limits} if limits is not None else {}),
    }
    if config.suite is not None:
        record_data["effective_parameters"]["task_count"] = len(tasks)
        record_data["effective_parameters"]["attempts_per_task"] = config.trials * len(targets)
    record.write_text(json.dumps(record_data, indent=2) + "\n", encoding="utf-8")
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
    if config.openrouter_key_mode in {"split", "shared_roles", "shared_caps"}:
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
        elif config.openrouter_key_mode == "shared_caps":
            print("Guarded shared_caps launch recipe; set the ordinary key and require finite headroom before job creation:")
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

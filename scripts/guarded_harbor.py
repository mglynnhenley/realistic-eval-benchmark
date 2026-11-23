"""Opt-in host launcher for one fresh Harbor Modal job behind an OpenRouter monitor.

Usage::

    scripts/harbor python -m scripts.guarded_harbor \
        --config JOB.yaml --openrouter-key-ceiling 50 \
        --openrouter-trial-delta "$OPENROUTER_TRIAL_DELTA"

This script is not activated by repository configuration and its presence is not
launch approval. Modal workspace costs are not monitored by this launcher and
require owner monitoring. Paid-grader holds remain separate; key metadata does
not establish that OpenRouter BYOK is absent. OpenRouter monitoring cannot
prevent an in-flight call or billing delay from exceeding a threshold, and host
failure or Modal cleanup failure can leave cleanup uncertain.

Exit 0 means one finished trial with a valid verifier result and no recorded
exceptions, regardless of its score. Trial failures reported by Harbor exit 1
with a fixed reason, distinct from spend-guard failures.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
import tomllib
from collections.abc import Callable
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import JobConfig
from harbor.models.job.result import JobResult
from pydantic import ValidationError

from refund_eval.harbor import adapter
from refund_eval.harbor.adapter import (
    AGENT_IMPORT_PATH,
    verify_split_budget_task_sources,
    verify_split_budget_wheel,
)
from scripts.offline_spend_guard import (
    KEY_CEILING,
    GuardFailure,
    SpendGuard,
    SplitSpendGuard,
    SplitThresholds,
    Thresholds,
    install_modal_hooks,
)

PINNED_HARBOR_VERSION = "0.26.0"
OPENROUTER_KEY_REFERENCE = "${OPENROUTER_API_KEY}"
OPENROUTER_AGENT_KEY_REFERENCE = "${OPENROUTER_AGENT_API_KEY}"
OPENROUTER_WORLD_KEY_REFERENCE = "${OPENROUTER_WORLD_API_KEY}"
OPENROUTER_ROLE_KEY_NAMES = {"OPENROUTER_API_KEY", "OPENROUTER_AGENT_API_KEY", "OPENROUTER_WORLD_API_KEY"}
_ROLE_KEY_REFERENCE = re.compile(r"\$(?:\{)?(OPENROUTER_API_KEY|OPENROUTER_AGENT_API_KEY|OPENROUTER_WORLD_API_KEY)\b")


class LauncherFailure(RuntimeError):
    pass


def _decimal_argument(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("expected a finite USD amount") from None
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("expected a positive finite USD amount")
    return amount


def require_pinned_harbor() -> None:
    try:
        version = importlib.metadata.version("harbor")
    except importlib.metadata.PackageNotFoundError:
        raise LauncherFailure("pinned Harbor 0.26.0 is required") from None
    if version != PINNED_HARBOR_VERSION:
        raise LauncherFailure("pinned Harbor 0.26.0 is required")


def load_job_config(path: Path) -> JobConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return JobConfig.model_validate(raw)
    except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError):
        raise LauncherFailure("job configuration is invalid") from None


def _reject_openrouter_key_override(env: dict[str, str], location: str) -> None:
    value = env.get("OPENROUTER_API_KEY")
    if value is not None and value != OPENROUTER_KEY_REFERENCE:
        raise LauncherFailure(f"{location} overrides the monitored OpenRouter key")


def _has_role_key_reference(env: dict[str, Any]) -> bool:
    return any(
        isinstance(value, str) and _ROLE_KEY_REFERENCE.search(value) is not None
        for value in env.values()
    )


def _validate_local_task_envs(task_path: Path, *, split: bool = False) -> None:
    try:
        task_config = tomllib.loads(
            (task_path / "task.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise LauncherFailure("local task configuration is invalid") from None
    if split:
        environment = task_config.get("environment")
        if not isinstance(environment, dict) or environment.get("docker_image") is not None:
            raise LauncherFailure("split task may not use a prebuilt docker_image")

    def validate_env_maps(value: object) -> None:
        if isinstance(value, dict):
            if "env" in value:
                env = value["env"]
                if not isinstance(env, dict):
                    raise LauncherFailure("local task configuration is invalid")
                if split:
                    if OPENROUTER_ROLE_KEY_NAMES.intersection(env) or _has_role_key_reference(env):
                        raise LauncherFailure("split task base environment may not inject a shared OpenRouter key")
                else:
                    _reject_openrouter_key_override(env, "local task environment")
            for key, nested in value.items():
                if key != "env":
                    validate_env_maps(nested)
        elif isinstance(value, list):
            for nested in value:
                validate_env_maps(nested)

    validate_env_maps(task_config)


def _compose_environment(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        if any(
            not isinstance(name, str) or not name or item is None
            or not isinstance(item, (str, int, float, bool))
            for name, item in value.items()
        ):
            raise ValueError
        return {name: str(item) for name, item in value.items()}
    if isinstance(value, list):
        result = {}
        for item in value:
            if not isinstance(item, str) or "=" not in item:
                raise ValueError
            name, item_value = item.split("=", 1)
            if not name or name in result:
                raise ValueError
            result[name] = item_value
        return result
    raise ValueError


def _split_compose_values(task_path: Path) -> dict[str, Any]:
    try:
        environment_dir = task_path / "environment"
        if any(environment_dir.glob("docker-compose.override*")) or any(environment_dir.glob("compose.override*")):
            raise ValueError
        compose = yaml.safe_load((environment_dir / "docker-compose.yaml").read_text(encoding="utf-8"))
        if not isinstance(compose, dict) or set(compose) != {"services", "volumes"}:
            raise ValueError
        services = compose["services"]
        if not isinstance(services, dict) or set(services) != {"main", "world"}:
            raise ValueError
        if compose["volumes"] != {"world-logs": None}:
            raise ValueError
        main = services["main"]
        world_service = services["world"]
        if main != {"depends_on": {"world": {"condition": "service_healthy"}}}:
            raise ValueError
        if not isinstance(world_service, dict) or set(world_service) != {
            "build", "expose", "environment", "security_opt", "volumes"
        }:
            raise ValueError
        if (world_service["build"] != {"context": "${CONTEXT_DIR}", "dockerfile": "world/Dockerfile"}
                or world_service["expose"] != ["8000"]
                or world_service["security_opt"] != ["seccomp=unconfined"]
                or world_service["volumes"] != ["world-logs:/var/log/world"]):
            raise ValueError
        world = _compose_environment(world_service["environment"])
        for service_name, service in services.items():
            env = _compose_environment(service.get("environment"))
            for name, value in env.items():
                if name in OPENROUTER_ROLE_KEY_NAMES:
                    expected = (service_name == "world" and name == "OPENROUTER_API_KEY"
                                and value == OPENROUTER_WORLD_KEY_REFERENCE)
                    if not expected:
                        raise ValueError
                if _ROLE_KEY_REFERENCE.search(value):
                    expected = (service_name == "world" and name == "OPENROUTER_API_KEY"
                                and value == OPENROUTER_WORLD_KEY_REFERENCE)
                    if not expected:
                        raise ValueError
        return world
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError):
        raise LauncherFailure("split task Compose environment is invalid") from None


def validate_job_config(config: JobConfig, *, expected_split: bool | None = None) -> None:
    if (
        config.n_attempts != 1
        or config.n_concurrent_trials != 1
        or config.retry.max_retries != 0
        or config.install_only
        or config.source_jobs
        or config.is_regrade
        or config.is_diff
        or config.datasets
        or len(config.tasks) != 1
        or len(config.agents) != 1
        or config.user_agent is not None
    ):
        raise LauncherFailure("only one fresh task and agent with zero retries is supported")

    if config.environment.type != EnvironmentType.MODAL or not config.environment.delete:
        raise LauncherFailure("job must use Modal with environment deletion enabled")
    if config.environment.import_path is not None:
        raise LauncherFailure("custom environment import paths are not supported")

    agent = config.agents[0]
    agent_env = agent.env or {}
    split = agent_env.get("OPENROUTER_API_KEY") == OPENROUTER_AGENT_KEY_REFERENCE
    if expected_split is not None and split != expected_split:
        raise LauncherFailure("guard mode does not match the prepared role-key routing")
    if not split and agent_env.get("OPENROUTER_API_KEY") != OPENROUTER_KEY_REFERENCE:
        raise LauncherFailure("agent must use the host OpenRouter key without resume settings")
    if (
        agent.n_concurrent not in (None, 1)
        or agent.resume_trajectory
        or agent.load_trajectory
        or agent.model_api_key_name not in (None, "OPENROUTER_API_KEY")
        or not agent.model_name
        or not agent.model_name.startswith("openrouter/")
        or agent.base_url is not None
        or (not split and agent_env.get("OPENROUTER_API_KEY") != OPENROUTER_KEY_REFERENCE)
    ):
        raise LauncherFailure("agent must use the host OpenRouter key without resume settings")

    if split:
        kwargs = getattr(agent, "kwargs", {}) or {}
        try:
            cap = Decimal(str(kwargs["cost_limit"]))
            declared_cap = Decimal(str(agent_env["REFUND_EVAL_AGENT_DOLLAR_CAP"]))
            output_tokens_raw = agent_env["REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS"]
            if not isinstance(output_tokens_raw, str) or not output_tokens_raw.isdecimal():
                raise ValueError
            output_cap = int(output_tokens_raw)
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise LauncherFailure("split agent dollar cap or output cap is invalid") from None
        if not cap.is_finite() or cap <= 0 or cap > 50 or cap != declared_cap or output_cap <= 0:
            raise LauncherFailure("split agent dollar cap must match cost_limit within supported ceiling")
        if (agent.import_path != AGENT_IMPORT_PATH
                or agent_env.get("OPENROUTER_API_KEY") != OPENROUTER_AGENT_KEY_REFERENCE
                or set(agent_env) != {
                    "OPENROUTER_API_KEY", "REFUND_EVAL_AGENT_DOLLAR_CAP", "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS"
                }
                or OPENROUTER_ROLE_KEY_NAMES.intersection(agent_env) - {"OPENROUTER_API_KEY"}
                or _has_role_key_reference({key: value for key, value in agent_env.items()
                                            if key != "OPENROUTER_API_KEY"})):
            raise LauncherFailure("split mode requires the native guarded agent and role-isolated key env")

    environment_env = config.environment.env or {}
    if split:
        if (OPENROUTER_ROLE_KEY_NAMES.intersection(environment_env)
                or OPENROUTER_ROLE_KEY_NAMES.intersection(config.verifier.env or {})
                or _has_role_key_reference(environment_env)
                or _has_role_key_reference(config.verifier.env or {})
                or "COMPOSE_FILE" in os.environ
                or "COMPOSE_PROFILES" in os.environ
                or getattr(config.environment, "extra_docker_compose", None)
                or (getattr(config.environment, "kwargs", None) or {}).get("secrets")
                or (getattr(config.environment, "kwargs", None) or {}).get("registry_secret")
                or (getattr(config.environment, "kwargs", None) or {}).get("volumes")):
            raise LauncherFailure("split keys may only be routed to their role containers")
    else:
        _reject_openrouter_key_override(environment_env, "Modal environment")
        _reject_openrouter_key_override(config.verifier.env or {}, "verifier environment")

    task = config.tasks[0]
    if task.path is None or task.git_url or task.source:
        raise LauncherFailure("job must use one local task")
    task_path = task.path.expanduser()
    if not task_path.is_dir() or not (task_path / "task.toml").is_file():
        raise LauncherFailure("job task must be a present local task directory")
    _validate_local_task_envs(task_path, split=split)
    if split:
        try:
            verify_split_budget_task_sources(task_path)
        except (OSError, ValueError):
            raise LauncherFailure("split world source does not attest current budget enforcement") from None
        values = _split_compose_values(task_path)
        if values.get("OPENROUTER_API_KEY") != OPENROUTER_WORLD_KEY_REFERENCE or values.get("WORLD_STRICT_BUDGET") != "true":
            raise LauncherFailure("split world key routing or strict budget is missing")
        try:
            world_cap = Decimal(str(values["WORLD_COST_LIMIT"]))
        except (KeyError, InvalidOperation, ValueError, TypeError):
            raise LauncherFailure("split world dollar cap is invalid") from None
        if not world_cap.is_finite() or world_cap <= 0 or world_cap > 100:
            raise LauncherFailure("split world dollar cap exceeds its supported ceiling")
        for role_name in ("WORLD_SIMULATOR_MODEL", "WORLD_GRADER_MODEL"):
            if not isinstance(values.get(role_name), str) or not values[role_name].startswith("openrouter/"):
                raise LauncherFailure("split world models must use OpenRouter")
        if any("OPENROUTER_AGENT_API_KEY" in str(value) for value in values.values()):
            raise LauncherFailure("agent OpenRouter key is exposed to the world service")
        try:
            for relative, trusted in (
                ("environment/Dockerfile", adapter.TEMPLATE / "environment" / "Dockerfile"),
                ("environment/world/Dockerfile", adapter.TEMPLATE / "environment" / "world" / "Dockerfile"),
                ("environment/agent/install-agent.py", Path(adapter.__file__).with_name("install_agent.py")),
            ):
                if (task_path / relative).read_bytes() != trusted.read_bytes():
                    raise ValueError
        except (OSError, ValueError):
            raise LauncherFailure("split container build/runtime paths are invalid") from None
        try:
            task_toml = tomllib.loads((task_path / "task.toml").read_text(encoding="utf-8"))
            task_metadata = task_toml["metadata"]
            wheel_name = task_metadata["agent_wheel_filename"]
            if not isinstance(wheel_name, str) or Path(wheel_name).name != wheel_name:
                raise ValueError
            wheel_path = task_path / "environment" / "agent" / "wheels" / wheel_name
            wheel_record = json.loads(
                (task_path / "environment" / "agent" / "agent-wheel.json").read_text(encoding="utf-8")
            )
            wheel_digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
            if (wheel_record != {"filename": wheel_name, "sha256": wheel_digest}
                    or task_metadata.get("agent_wheel_sha256") != wheel_digest):
                raise ValueError
            verify_split_budget_wheel(wheel_path)
        except (OSError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError):
            raise LauncherFailure("split agent wheel does not attest current budget sources") from None

    if Path(config.job_name).name != config.job_name or config.job_name in {".", ".."}:
        raise LauncherFailure("job name must be a single path component")
    job_dir = config.jobs_dir / config.job_name
    if job_dir.exists():
        raise LauncherFailure("existing Harbor jobs cannot be resumed")


async def run_guarded_job(
    config: JobConfig,
    thresholds: Thresholds | SplitThresholds,
    *,
    guard: SpendGuard | None = None,
    job_factory: Any | None = None,
    on_job_created: Callable[[Any], None] | None = None,
    on_sandbox_created: Callable[[Any], None] | None = None,
    before_job_run: Callable[[Any], None] | None = None,
    total_time_limit_seconds: int | None = None,
) -> JobResult:
    if total_time_limit_seconds is not None:
        raise LauncherFailure("strict_total_deadline_unsupported_by_modal")
    require_pinned_harbor()
    split = isinstance(thresholds, SplitThresholds)
    validate_job_config(config, expected_split=split)
    controller = guard or (SplitSpendGuard(thresholds) if split else SpendGuard(thresholds))
    try:
        await controller.start()
        with install_modal_hooks(controller, on_sandbox_created=on_sandbox_created):
            await controller.ensure_healthy()
            if job_factory is None:
                from harbor.job import Job

                job_factory = Job.create
            create_task = asyncio.create_task(job_factory(config))
            controller.attach_job_task(create_task)
            try:
                job = await create_task
            except asyncio.CancelledError:
                if controller.failure_reason is None:
                    await asyncio.shield(controller.fail_closed("Harbor job creation cancelled"))
                raise controller._failure() from None
            controller.attach_job_task(None)
            if getattr(job, "is_resuming", False):
                raise LauncherFailure("existing Harbor jobs cannot be resumed")
            if on_job_created is not None:
                with suppress(Exception):
                    on_job_created(job)
            await controller.ensure_healthy()
            if before_job_run is not None:
                try:
                    before_job_run(job)
                except Exception:
                    await controller.fail_closed("required run metadata could not be written", cancel_job=False)
                    raise
            await controller.ensure_healthy()

            run_task = asyncio.create_task(job.run())
            controller.attach_job_task(run_task)
            try:
                result = await run_task
            except asyncio.CancelledError:
                if controller.failure_reason is None:
                    await asyncio.shield(controller.fail_closed("Harbor job cancelled"))
                raise controller._failure() from None
            except Exception:
                if controller.failure_reason is None:
                    await controller.fail_closed("Harbor job failed", cancel_job=False)
                raise
            controller.attach_job_task(None)
            await controller.ensure_healthy()
            return result
    except GuardFailure:
        raise
    except asyncio.CancelledError:
        await asyncio.shield(controller.fail_closed("launcher cancelled"))
        raise controller._failure() from None
    except Exception:
        await controller.cleanup_bound()
        raise
    finally:
        await asyncio.shield(controller.close())


def _trial_failure_reason(result: JobResult) -> str | None:
    if result.n_total_trials != 1 or len(result.trial_results) != 1:
        return "unexpected_trial_count"
    trial = result.trial_results[0]
    if trial.recorded_exceptions():
        return "trial_exception"
    if result.finished_at is None or trial.finished_at is None:
        return "incomplete_result"
    if not trial.reward_valid:
        return "invalid_reward"
    return None


def run_guarded_config(
    config: JobConfig,
    thresholds: Thresholds | SplitThresholds,
    *,
    on_job_created: Callable[[Any], None] | None = None,
    on_sandbox_created: Callable[[Any], None] | None = None,
    before_job_run: Callable[[Any], None] | None = None,
    total_time_limit_seconds: int | None = None,
) -> int:
    if total_time_limit_seconds is not None:
        print(
            "guard_failed=strict_total_deadline_unsupported_by_modal cleanup_uncertain=false",
            file=sys.stderr,
        )
        return 2
    if isinstance(thresholds, Thresholds) and thresholds.openrouter_key_ceiling != KEY_CEILING:
        print("guard_failed=OpenRouter key ceiling must be exactly $50 cleanup_uncertain=false", file=sys.stderr)
        return 2
    if not math.isfinite(thresholds.probe_timeout):
        print("guard_failed=invalid timeout cleanup_uncertain=false", file=sys.stderr)
        return 2
    try:
        require_pinned_harbor()
        validate_job_config(config, expected_split=isinstance(thresholds, SplitThresholds))
        hooks = {}
        if on_job_created is not None:
            hooks["on_job_created"] = on_job_created
        if on_sandbox_created is not None:
            hooks["on_sandbox_created"] = on_sandbox_created
        if before_job_run is not None:
            hooks["before_job_run"] = before_job_run
        trial_failure = _trial_failure_reason(
            asyncio.run(run_guarded_job(config, thresholds, **hooks))
        )
    except GuardFailure as failure:
        print(
            f"guard_failed={failure.reason} cleanup_uncertain={str(failure.cleanup_uncertain).lower()}",
            file=sys.stderr,
        )
        return 2
    except LauncherFailure:
        print("launcher_failed=configuration_or_Harbor_validation", file=sys.stderr)
        return 2
    except Exception as failure:  # noqa: BLE001
        print(
            f"launcher_failed={type(failure).__name__} cleanup_uncertain=false",
            file=sys.stderr,
        )
        return 1
    if trial_failure is not None:
        print(f"guarded_harbor_job=failed reason={trial_failure}", file=sys.stderr)
        return 1
    print("guarded_harbor_job=complete")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one fresh Harbor Modal job behind a fail-closed OpenRouter monitor."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--openrouter-key-ceiling", type=_decimal_argument, required=True)
    parser.add_argument("--openrouter-trial-delta", type=_decimal_argument, required=True)
    parser.add_argument("--probe-timeout", type=float, default=10.0)
    parser.add_argument("--poll-interval", type=float, default=15.0)
    parser.add_argument("--max-sample-age", type=float, default=35.0)
    parser.add_argument("--cleanup-timeout", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.openrouter_key_ceiling != KEY_CEILING:
        print("guard_failed=OpenRouter key ceiling must be exactly $50 cleanup_uncertain=false", file=sys.stderr)
        return 2
    if not math.isfinite(args.probe_timeout):
        print("guard_failed=invalid timeout cleanup_uncertain=false", file=sys.stderr)
        return 2
    try:
        thresholds = Thresholds(
            openrouter_key_ceiling=args.openrouter_key_ceiling,
            openrouter_trial_delta=args.openrouter_trial_delta,
            probe_timeout=args.probe_timeout,
            poll_interval=args.poll_interval,
            max_sample_age=args.max_sample_age,
            cleanup_timeout=args.cleanup_timeout,
        )
        require_pinned_harbor()
        config = load_job_config(args.config)
    except GuardFailure as failure:
        print(
            f"guard_failed={failure.reason} cleanup_uncertain={str(failure.cleanup_uncertain).lower()}",
            file=sys.stderr,
        )
        return 2
    except LauncherFailure:
        print("launcher_failed=configuration_or_Harbor_validation", file=sys.stderr)
        return 2
    except Exception as failure:  # noqa: BLE001
        print(
            f"launcher_failed={type(failure).__name__} cleanup_uncertain=false",
            file=sys.stderr,
        )
        return 1
    return run_guarded_config(config, thresholds)


if __name__ == "__main__":
    raise SystemExit(main())

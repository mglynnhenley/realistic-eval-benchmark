"""Opt-in host launcher for one fresh Harbor Modal job behind spend monitoring.

Usage::

    scripts/harbor python -m scripts.guarded_harbor \
        --config JOB.yaml --openrouter-key-ceiling 50 \
        --openrouter-trial-delta "$OPENROUTER_TRIAL_DELTA" \
        --modal-cycle-stop "$MODAL_CYCLE_STOP"

This script is not activated by repository configuration and its presence is not
launch approval. Billing and paid-grader holds remain separate; key metadata
does not establish that OpenRouter BYOK is absent. Monitoring cannot prevent an
in-flight call or billing delay from exceeding a threshold, and host failure or
Modal cleanup failure can leave cleanup uncertain.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import math
import sys
import tomllib
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from harbor.models.environment_type import EnvironmentType
from harbor.models.job.config import JobConfig
from pydantic import ValidationError

from scripts.offline_spend_guard import (
    KEY_CEILING,
    GuardFailure,
    SpendGuard,
    Thresholds,
    install_modal_hooks,
)

PINNED_HARBOR_VERSION = "0.26.0"
OPENROUTER_KEY_REFERENCE = "${OPENROUTER_API_KEY}"


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


def _validate_local_task_envs(task_path: Path) -> None:
    try:
        task_config = tomllib.loads(
            (task_path / "task.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise LauncherFailure("local task configuration is invalid") from None

    def validate_env_maps(value: object) -> None:
        if isinstance(value, dict):
            if "env" in value:
                env = value["env"]
                if not isinstance(env, dict):
                    raise LauncherFailure("local task configuration is invalid")
                _reject_openrouter_key_override(env, "local task environment")
            for key, nested in value.items():
                if key != "env":
                    validate_env_maps(nested)
        elif isinstance(value, list):
            for nested in value:
                validate_env_maps(nested)

    validate_env_maps(task_config)


def validate_job_config(config: JobConfig) -> None:
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
    if (
        agent.n_concurrent not in (None, 1)
        or agent.resume_trajectory
        or agent.load_trajectory
        or agent.model_api_key_name not in (None, "OPENROUTER_API_KEY")
        or not agent.model_name
        or not agent.model_name.startswith("openrouter/")
        or agent.base_url is not None
        or agent_env.get("OPENROUTER_API_KEY") != OPENROUTER_KEY_REFERENCE
    ):
        raise LauncherFailure("agent must use the host OpenRouter key without resume settings")

    environment_env = config.environment.env or {}
    _reject_openrouter_key_override(environment_env, "Modal environment")
    _reject_openrouter_key_override(config.verifier.env or {}, "verifier environment")

    task = config.tasks[0]
    if task.path is None or task.git_url or task.source:
        raise LauncherFailure("job must use one local task")
    task_path = task.path.expanduser()
    if not task_path.is_dir() or not (task_path / "task.toml").is_file():
        raise LauncherFailure("job task must be a present local task directory")
    _validate_local_task_envs(task_path)

    if Path(config.job_name).name != config.job_name or config.job_name in {".", ".."}:
        raise LauncherFailure("job name must be a single path component")
    job_dir = config.jobs_dir / config.job_name
    if job_dir.exists():
        raise LauncherFailure("existing Harbor jobs cannot be resumed")


async def run_guarded_job(
    config: JobConfig,
    thresholds: Thresholds,
    *,
    guard: SpendGuard | None = None,
    job_factory: Any | None = None,
) -> object:
    require_pinned_harbor()
    validate_job_config(config)
    controller = guard or SpendGuard(thresholds)
    try:
        await controller.start()
        with install_modal_hooks(controller):
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one fresh Harbor Modal job behind fail-closed spend monitors."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--openrouter-key-ceiling", type=_decimal_argument, required=True)
    parser.add_argument("--openrouter-trial-delta", type=_decimal_argument, required=True)
    parser.add_argument("--modal-cycle-stop", type=_decimal_argument, required=True)
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
            modal_cycle_stop=args.modal_cycle_stop,
            probe_timeout=args.probe_timeout,
            poll_interval=args.poll_interval,
            max_sample_age=args.max_sample_age,
            cleanup_timeout=args.cleanup_timeout,
        )
        require_pinned_harbor()
        config = load_job_config(args.config)
        validate_job_config(config)
        asyncio.run(run_guarded_job(config, thresholds))
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
    print("guarded_harbor_job=complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

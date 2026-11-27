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

Exit 0 means one finished trial with no recorded exceptions and either a valid
verifier result or an intentionally ungraded completion. Trial failures
reported by Harbor exit 1 with a fixed reason, distinct from spend-guard failures.
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
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable
from contextlib import contextmanager, suppress
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
from refund_eval.harbor.batch import GUARDED_SCENARIOS
from refund_eval.harbor.private_traces import (
    PRIVATE_AGENT_BUFFER_ROOT,
    PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE,
    PRIVATE_WORLD_LOGS_ROOT,
)
from refund_eval.models.shared_role_spend_ledger import (
    SHARED_CAPS_HEADROOM_BOUND_ENV,
    WORLD_ROLE_CAP_CEILING_USD,
    SharedRoleBudgetConfig,
    SharedRoleSpendLedgerError,
)
from scripts.offline_spend_guard import (
    KEY_CEILING,
    GuardFailure,
    SharedCapsSpendGuard,
    SharedCapsThresholds,
    SharedRoleSpendGuard,
    SharedRoleThresholds,
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


@contextmanager
def private_world_bind_mount_adapter():
    from harbor.environments.base import BaseEnvironment
    from harbor.environments.modal import ModalEnvironment, _ModalDinD

    original = BaseEnvironment._mount_targets
    original_start = _ModalDinD.start

    def mount_targets(environment, *, writable_only: bool = False):
        targets = original(environment, writable_only=writable_only)
        if isinstance(environment, ModalEnvironment):
            for target in (PRIVATE_WORLD_LOGS_ROOT, PRIVATE_AGENT_BUFFER_ROOT):
                if target not in targets:
                    targets.append(target)
        return targets

    async def start(strategy, force_build: bool):
        environment = strategy._env
        had_marker = hasattr(environment, PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE)
        previous_marker = getattr(environment, PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE, None)
        setattr(environment, PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE, True)
        try:
            with private_world_compose_bind(environment):
                await original_start(strategy, force_build)
        except (Exception, asyncio.CancelledError):
            if had_marker:
                setattr(environment, PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE, previous_marker)
            else:
                delattr(environment, PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE)
            raise

    BaseEnvironment._mount_targets = mount_targets
    _ModalDinD.start = start
    try:
        yield
    finally:
        BaseEnvironment._mount_targets = original
        _ModalDinD.start = original_start


@contextmanager
def private_world_compose_bind(environment):
    original_dir = environment.environment_dir
    with tempfile.TemporaryDirectory(prefix="guarded-modal-world-logs-") as temporary:
        isolated_dir = Path(temporary) / "environment"
        shutil.copytree(original_dir, isolated_dir)
        compose_path = isolated_dir / "docker-compose.yaml"
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        services = compose.get("services") if isinstance(compose, dict) else None
        main = services.get("main") if isinstance(services, dict) else None
        world = services.get("world") if isinstance(services, dict) else None
        main_volumes = main.get("volumes") if isinstance(main, dict) else None
        world_volumes = world.get("volumes") if isinstance(world, dict) else None
        if isinstance(main, dict) and "volumes" not in main:
            main_volumes = main.setdefault("volumes", [])
        if not isinstance(main_volumes, list) or not isinstance(world_volumes, list):
            raise LauncherFailure("guarded Modal task must have main and world volume lists")
        world_mounts = [index for index, volume in enumerate(world_volumes)
                        if volume == "world-logs:/var/log/world"]
        if len(world_mounts) != 1:
            raise LauncherFailure("guarded Modal task must have one named world-log volume")

        buffer_target = "/refund-eval-private/inspect-data"
        buffer_mount = f"inspect-buffer:{buffer_target}"
        private_buffer_mount = f"{PRIVATE_AGENT_BUFFER_ROOT}:{buffer_target}"
        existing_buffer_mounts = [index for index, volume in enumerate(main_volumes)
                                  if isinstance(volume, str) and volume.endswith(f":{buffer_target}")]
        if len(existing_buffer_mounts) > 1:
            raise LauncherFailure("guarded Modal task has duplicate Inspect buffer mounts")
        if existing_buffer_mounts:
            if main_volumes[existing_buffer_mounts[0]] not in {buffer_mount, private_buffer_mount}:
                raise LauncherFailure("guarded Modal task has an unexpected Inspect buffer mount")
            main_volumes[existing_buffer_mounts[0]] = private_buffer_mount
        else:
            main_volumes.append(private_buffer_mount)

        volumes = compose.setdefault("volumes", {})
        if not isinstance(volumes, dict):
            raise LauncherFailure("guarded Modal task has an invalid top-level volume map")
        volumes.setdefault("inspect-buffer", {})
        if any(
            isinstance(volume, str) and volume.startswith("role-budget:")
            for volume in (*main_volumes, *world_volumes)
        ):
            volumes.setdefault("role-budget", {})
        world_volumes[world_mounts[0]] = f"{PRIVATE_WORLD_LOGS_ROOT}:/var/log/world"
        compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
        environment.environment_dir = isolated_dir
        try:
            yield
        finally:
            environment.environment_dir = original_dir


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


def _validate_local_task_envs(task_path: Path, *, split: bool = False, shared_roles: bool = False,
                              shared_caps: bool = False) -> None:
    try:
        task_config = tomllib.loads(
            (task_path / "task.toml").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise LauncherFailure("local task configuration is invalid") from None
    if split or shared_roles or shared_caps:
        environment = task_config.get("environment")
        if not isinstance(environment, dict) or environment.get("docker_image") is not None:
            raise LauncherFailure("strict role-budget task may not use a prebuilt docker_image")

    def validate_env_maps(value: object) -> None:
        if isinstance(value, dict):
            if "env" in value:
                env = value["env"]
                if not isinstance(env, dict):
                    raise LauncherFailure("local task configuration is invalid")
                if split or shared_roles or shared_caps:
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
        if not isinstance(compose, dict) or set(compose) not in ({"services"}, {"services", "volumes"}):
            raise ValueError
        if compose.get("volumes", {}) not in (
            {},
            {"world-logs": None},
            {"world-logs": None, "inspect-buffer": None},
        ):
            raise ValueError
        services = compose["services"]
        if not isinstance(services, dict) or set(services) != {"main", "world"}:
            raise ValueError
        main = services["main"]
        world_service = services["world"]
        if (not isinstance(main, dict)
                or set(main) - {"depends_on", "environment", "volumes"}
                or "depends_on" not in main
                or main["depends_on"] != {"world": {"condition": "service_healthy"}}
                or main.get("environment", {}) not in ({}, {"XDG_DATA_HOME": PRIVATE_AGENT_BUFFER_ROOT})
                or main.get("volumes", []) not in ([], ["inspect-buffer:/refund-eval-private/inspect-data"])):
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


def _shared_roles_compose_values(
    task_path: Path, *, expected_mode: str = "shared_roles", expected_usage_reporting_mode: str = "strict"
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        environment_dir = task_path / "environment"
        if any(environment_dir.glob("docker-compose.override*")) or any(environment_dir.glob("compose.override*")):
            raise ValueError
        compose = yaml.safe_load((environment_dir / "docker-compose.yaml").read_text(encoding="utf-8"))
        if not isinstance(compose, dict) or set(compose) not in ({"services"}, {"services", "volumes"}):
            raise ValueError
        if compose.get("volumes", {}) not in (
            {},
            {"world-logs": None, "role-budget": None},
            {"world-logs": None, "inspect-buffer": None, "role-budget": None},
        ):
            raise ValueError
        services = compose["services"]
        if not isinstance(services, dict) or set(services) != {"main", "world"}:
            raise ValueError
        main, world_service = services["main"], services["world"]
        if (not isinstance(main, dict)
                or set(main) - {"depends_on", "environment", "volumes"}
                or "depends_on" not in main
                or main["depends_on"] != {"world": {"condition": "service_healthy"}}
                or main.get("environment", {}) not in ({}, {"XDG_DATA_HOME": PRIVATE_AGENT_BUFFER_ROOT})
                or main.get("volumes", []) not in (
                    ["inspect-buffer:/refund-eval-private/inspect-data", "role-budget:/var/lib/refund-eval/role-budget"],
                    ["role-budget:/var/lib/refund-eval/role-budget", "inspect-buffer:/refund-eval-private/inspect-data"],
                    ["role-budget:/var/lib/refund-eval/role-budget"],
                )):
            raise ValueError
        if not isinstance(world_service, dict) or set(world_service) != {
            "build", "expose", "environment", "security_opt", "volumes"
        }:
            raise ValueError
        if (world_service["build"] != {"context": "${CONTEXT_DIR}", "dockerfile": "world/Dockerfile"}
                or world_service["expose"] != ["8000"]
                or world_service["security_opt"] != ["seccomp=unconfined"]
                or world_service["volumes"] != ["world-logs:/var/log/world",
                    "role-budget:/var/lib/refund-eval/role-budget", "role-budget:/var/log/world/role-budget"]):
            raise ValueError
        world = _compose_environment(world_service["environment"])
        task = tomllib.loads((task_path / "task.toml").read_text(encoding="utf-8"))
        task_environment = task.get("environment")
        task_env = task_environment.get("env") if isinstance(task_environment, dict) else None
        metadata = task.get("metadata")
        if not isinstance(task_env, dict) or not isinstance(metadata, dict):
            raise TypeError
        if (metadata.get("openrouter_key_mode") != expected_mode
                or world.get("REFUND_EVAL_SHARED_ROLE_MODE") != expected_mode
                or task_env.get("REFUND_EVAL_SHARED_ROLE_MODE") != expected_mode
                or metadata.get("usage_reporting_mode", "strict") != expected_usage_reporting_mode
                or world.get("REFUND_EVAL_USAGE_REPORTING_MODE", "strict") != expected_usage_reporting_mode
                or metadata.get("disable_application_price_cutoffs", "false")
                    not in {"true", "false"}
                or world.get("REFUND_EVAL_DISABLE_APPLICATION_PRICE_CUTOFFS", "false")
                    != metadata.get("disable_application_price_cutoffs", "false")
                or (metadata.get("disable_application_price_cutoffs", "false") == "true"
                    and (expected_mode != "shared_caps" or expected_usage_reporting_mode != "usage_only"))
                or world.get("WORLD_STRICT_BUDGET") != "true"
                or world.get("OPENROUTER_API_KEY") != "${OPENROUTER_API_KEY:-}"):
            raise ValueError
        for source in (world, task_env):
            if source.get("REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG") != task_env.get("REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG"):
                raise ValueError
        for name, value in world.items():
            if name == "OPENROUTER_API_KEY":
                if value != "${OPENROUTER_API_KEY:-}":
                    raise ValueError
            elif _ROLE_KEY_REFERENCE.search(str(value)):
                raise ValueError
        return world, metadata
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError, tomllib.TOMLDecodeError):
        raise LauncherFailure("shared_roles task Compose, volume, or metadata is invalid") from None


def validate_job_config(config: JobConfig, *, expected_split: bool | None = None,
                        expected_shared_roles: bool | None = None, expected_shared_caps: bool | None = None,
                        expected_shared_caps_allowance: Decimal | None = None,
                        saved_identity_verified: bool = False) -> None:
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
    shared_roles = agent_env.get("REFUND_EVAL_SHARED_ROLE_MODE") == "shared_roles"
    shared_caps = agent_env.get("REFUND_EVAL_SHARED_ROLE_MODE") == "shared_caps"
    usage_reporting_mode = agent_env.get("REFUND_EVAL_USAGE_REPORTING_MODE", "strict")
    if usage_reporting_mode not in {"strict", "usage_only"} or (
        usage_reporting_mode == "usage_only" and not shared_caps
    ):
        raise LauncherFailure("usage reporting mode is invalid for the guarded key mode")
    if sum((split, shared_roles, shared_caps)) > 1:
        raise LauncherFailure("guarded key modes conflict")
    if expected_split is not None and split != expected_split:
        raise LauncherFailure("guard mode does not match the prepared role-key routing")
    if expected_shared_roles is not None and shared_roles != expected_shared_roles:
        raise LauncherFailure("guard mode does not match the prepared shared-role routing")
    if expected_shared_caps is not None and shared_caps != expected_shared_caps:
        raise LauncherFailure("guard mode does not match the prepared shared_caps routing")
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
        expected_split_env = {
            "OPENROUTER_API_KEY", "REFUND_EVAL_AGENT_DOLLAR_CAP", "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS"
        }
        if "REFUND_EVAL_USAGE_REPORTING_MODE" in agent_env:
            expected_split_env.add("REFUND_EVAL_USAGE_REPORTING_MODE")
        if (agent.import_path != AGENT_IMPORT_PATH
                or agent_env.get("OPENROUTER_API_KEY") != OPENROUTER_AGENT_KEY_REFERENCE
                or set(agent_env) != expected_split_env
                or OPENROUTER_ROLE_KEY_NAMES.intersection(agent_env) - {"OPENROUTER_API_KEY"}
                or _has_role_key_reference({key: value for key, value in agent_env.items()
                                            if key != "OPENROUTER_API_KEY"})):
            raise LauncherFailure("split mode requires the native guarded agent and role-isolated key env")
    elif shared_roles or shared_caps:
        kwargs = getattr(agent, "kwargs", {}) or {}
        try:
            cap = Decimal(str(kwargs["cost_limit"]))
            declared_cap = Decimal(str(agent_env["REFUND_EVAL_AGENT_DOLLAR_CAP"]))
            output_tokens_raw = agent_env["REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS"]
            if not isinstance(output_tokens_raw, str) or not output_tokens_raw.isdecimal():
                raise ValueError
            output_cap = int(output_tokens_raw)
            role_config_raw = agent_env["REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG"]
            role_config = SharedRoleBudgetConfig.from_json(role_config_raw)
        except (KeyError, InvalidOperation, ValueError, TypeError, SharedRoleSpendLedgerError):
            raise LauncherFailure("shared_roles agent caps or budget configuration are invalid") from None
        if (not cap.is_finite() or cap <= 0 or cap != declared_cap or output_cap <= 0
                or role_config.role_limits_usd["target"] != cap
                or (shared_caps and not Decimal(0) < role_config.allowance_usd <= cap + WORLD_ROLE_CAP_CEILING_USD)):
            raise LauncherFailure("shared-role target cap or output bound is invalid")
        expected_agent_env = {"OPENROUTER_API_KEY", "REFUND_EVAL_AGENT_DOLLAR_CAP",
                              "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS", "REFUND_EVAL_SHARED_ROLE_MODE",
                              "REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG"}
        if "REFUND_EVAL_USAGE_REPORTING_MODE" in agent_env:
            expected_agent_env.add("REFUND_EVAL_USAGE_REPORTING_MODE")
        if shared_caps:
            expected_agent_env.add(SHARED_CAPS_HEADROOM_BOUND_ENV)
            expected_bound_state = "true" if expected_shared_caps_allowance is not None else "false"
            if agent_env.get(SHARED_CAPS_HEADROOM_BOUND_ENV) != expected_bound_state:
                raise LauncherFailure("shared_caps headroom binding state is invalid")
        if (agent.import_path != AGENT_IMPORT_PATH
                or agent_env.get("OPENROUTER_API_KEY") != OPENROUTER_KEY_REFERENCE
                or set(agent_env) != expected_agent_env
                or OPENROUTER_ROLE_KEY_NAMES.intersection(agent_env) - {"OPENROUTER_API_KEY"}
                or _has_role_key_reference({key: value for key, value in agent_env.items()
                                            if key != "OPENROUTER_API_KEY"})):
            raise LauncherFailure("shared_roles requires the native agent and one ordinary key")

    environment_env = config.environment.env or {}
    if split or shared_roles or shared_caps:
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
    _validate_local_task_envs(task_path, split=split, shared_roles=shared_roles, shared_caps=shared_caps)
    try:
        task_metadata = tomllib.loads((task_path / "task.toml").read_text(encoding="utf-8")).get("metadata", {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        raise LauncherFailure("task metadata is invalid") from None
    task_scenario = task_metadata.get("scenario") if isinstance(task_metadata, dict) else None
    if task_scenario == "daycare_reimbursement" and not saved_identity_verified:
        raise LauncherFailure("daycare_reimbursement_requires_saved_identity")
    task_mode = task_metadata.get("openrouter_key_mode") if isinstance(task_metadata, dict) else None
    expected_task_mode = "split" if split else "shared_caps" if shared_caps else "shared_roles" if shared_roles else "shared"
    task_usage_reporting_mode = (
        task_metadata.get("usage_reporting_mode", "strict") if isinstance(task_metadata, dict) else None
    )
    if task_usage_reporting_mode != usage_reporting_mode:
        raise LauncherFailure("task usage reporting mode does not match the agent configuration")
    disable_application_price_cutoffs = (
        isinstance(task_metadata, dict)
        and task_metadata.get("disable_application_price_cutoffs") == "true"
    )
    if (shared_caps and not (usage_reporting_mode == "usage_only" and disable_application_price_cutoffs)
            and role_config.role_limits_usd["world"] != WORLD_ROLE_CAP_CEILING_USD):
        raise LauncherFailure("shared_caps mode requires a $100 world role cap")
    if task_mode in {"split", "shared_roles", "shared_caps"} and task_mode != expected_task_mode:
        raise LauncherFailure("task budget mode does not match guarded key routing")
    if (split or shared_roles or shared_caps) and task_mode != expected_task_mode:
        raise LauncherFailure("strict role budget task metadata is missing")
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
    elif shared_roles or shared_caps:
        try:
            verify_split_budget_task_sources(task_path)
            world, metadata = _shared_roles_compose_values(
                task_path,
                expected_mode="shared_caps" if shared_caps else "shared_roles",
                expected_usage_reporting_mode=usage_reporting_mode,
            )
            if (shared_caps and world.get(SHARED_CAPS_HEADROOM_BOUND_ENV)
                    != ("true" if expected_shared_caps_allowance is not None else "false")):
                raise ValueError
            role_config = SharedRoleBudgetConfig.from_json(agent_env["REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG"])
            task_config = tomllib.loads((task_path / "task.toml").read_text(encoding="utf-8"))
            task_env = task_config["environment"]["env"]
            world_cap = Decimal(str(world["WORLD_COST_LIMIT"]))
            target_cap = Decimal(str((agent.kwargs or {})["cost_limit"]))
            allowance = Decimal(str(metadata["shared_allowance_usd"]))
            declared_agent = Decimal(str(agent_env["REFUND_EVAL_AGENT_DOLLAR_CAP"]))
            output_tokens = agent_env["REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS"]
            if (not world_cap.is_finite() or world_cap <= 0 or world_cap > WORLD_ROLE_CAP_CEILING_USD
                    or target_cap != declared_agent or target_cap != role_config.role_limits_usd["target"]
                    or world_cap != role_config.role_limits_usd["world"]
                    or (shared_roles and (allowance != role_config.allowance_usd
                        or allowance > target_cap + world_cap))
                    or (shared_caps and (allowance != target_cap + world_cap
                        or (world.get("REFUND_EVAL_DISABLE_APPLICATION_PRICE_CUTOFFS") != "true"
                            and world_cap != WORLD_ROLE_CAP_CEILING_USD)
                        or not Decimal(0) < role_config.allowance_usd <= target_cap + world_cap))
                    or (shared_caps and expected_shared_caps_allowance is not None
                        and role_config.allowance_usd != expected_shared_caps_allowance)
                    or metadata.get("scenario") != role_config.scenario
                    or metadata.get("scenario") not in GUARDED_SCENARIOS
                    or role_config.models != {"target": agent.model_name,
                        "simulator": world.get("WORLD_SIMULATOR_MODEL"), "grader": world.get("WORLD_GRADER_MODEL")}
                    or task_env.get("REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG")
                        != agent_env.get("REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG")
                    or world.get("REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG")
                        != agent_env.get("REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG")
                    or world.get("WORLD_COST_LIMIT") is None
                    or not isinstance(output_tokens, str) or not output_tokens.isdecimal() or int(output_tokens) <= 0):
                raise ValueError
            for relative, trusted in (
                ("environment/Dockerfile", adapter.TEMPLATE / "environment" / "Dockerfile"),
                ("environment/world/Dockerfile", adapter.TEMPLATE / "environment" / "world" / "Dockerfile"),
                ("environment/agent/install-agent.py", Path(adapter.__file__).with_name("install_agent.py")),
            ):
                if (task_path / relative).read_bytes() != trusted.read_bytes():
                    raise ValueError
            task_toml = task_config["metadata"]
            wheel_name = task_toml["agent_wheel_filename"]
            wheel_path = task_path / "environment" / "agent" / "wheels" / wheel_name
            wheel_digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
            wheel_record = json.loads((task_path / "environment" / "agent" / "agent-wheel.json").read_text())
            if (Path(wheel_name).name != wheel_name or wheel_record != {"filename": wheel_name, "sha256": wheel_digest}
                    or task_toml.get("agent_wheel_sha256") != wheel_digest):
                raise ValueError
            verify_split_budget_wheel(wheel_path)
        except (OSError, KeyError, TypeError, ValueError, InvalidOperation, SharedRoleSpendLedgerError,
                tomllib.TOMLDecodeError):
            raise LauncherFailure("shared_roles task source, wheel, scope, or routing is invalid") from None

    if Path(config.job_name).name != config.job_name or config.job_name in {".", ".."}:
        raise LauncherFailure("job name must be a single path component")
    job_dir = config.jobs_dir / config.job_name
    if job_dir.exists():
        raise LauncherFailure("existing Harbor jobs cannot be resumed")


async def run_guarded_job(
    config: JobConfig,
    thresholds: Thresholds | SplitThresholds | SharedRoleThresholds | SharedCapsThresholds,
    *,
    guard: SpendGuard | None = None,
    job_factory: Any | None = None,
    on_job_created: Callable[[Any], None] | None = None,
    on_sandbox_created: Callable[[Any], None] | None = None,
    before_job_run: Callable[[Any], None] | None = None,
    on_guard_ready: Callable[[dict[str, Any]], None] | None = None,
    saved_identity_verified: bool = False,
    total_time_limit_seconds: int | None = None,
) -> JobResult:
    if total_time_limit_seconds is not None:
        raise LauncherFailure("strict_total_deadline_unsupported_by_modal")
    require_pinned_harbor()
    split = isinstance(thresholds, SplitThresholds)
    shared_roles = isinstance(thresholds, SharedRoleThresholds)
    shared_caps = isinstance(thresholds, SharedCapsThresholds)
    validate_job_config(config, expected_split=split, expected_shared_roles=shared_roles,
                        expected_shared_caps=shared_caps, saved_identity_verified=saved_identity_verified)
    controller = guard or (SplitSpendGuard(thresholds) if split else
                           SharedCapsSpendGuard(thresholds) if shared_caps else
                           SharedRoleSpendGuard(thresholds) if shared_roles else SpendGuard(thresholds))
    if shared_caps and not isinstance(controller, SharedCapsSpendGuard):
        raise LauncherFailure("shared_caps requires the verified headroom guard")
    try:
        await controller.start()
        if isinstance(controller, SharedCapsSpendGuard) and on_guard_ready is None:
            await controller.fail_closed("shared_caps runtime allowance was not bound", cancel_job=False)
            raise controller._failure()
        if on_guard_ready is not None and isinstance(controller, SharedRoleSpendGuard):
            metadata = controller.sanitized_effective_parameters()
            if metadata is None:
                await controller.fail_closed("shared-role runtime metadata is unavailable", cancel_job=False)
                raise controller._failure()
            try:
                on_guard_ready(metadata)
            except Exception:
                await controller.fail_closed("shared-role runtime metadata could not be recorded", cancel_job=False)
                raise
            if isinstance(controller, SharedCapsSpendGuard):
                validate_job_config(config, expected_shared_caps=True,
                                     expected_shared_caps_allowance=controller.effective_allowance_usd,
                                     saved_identity_verified=saved_identity_verified)
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


def _trial_failure_reason(result: JobResult, *, verifier_disabled: bool = False) -> str | None:
    if result.n_total_trials != 1 or len(result.trial_results) != 1:
        return "unexpected_trial_count"
    trial = result.trial_results[0]
    if trial.recorded_exceptions():
        return "trial_exception"
    if result.finished_at is None or trial.finished_at is None:
        return "incomplete_result"
    if not trial.reward_valid and not verifier_disabled:
        return "invalid_reward"
    return None


def run_guarded_config(
    config: JobConfig,
    thresholds: Thresholds | SplitThresholds | SharedRoleThresholds | SharedCapsThresholds,
    *,
    on_job_created: Callable[[Any], None] | None = None,
    on_sandbox_created: Callable[[Any], None] | None = None,
    before_job_run: Callable[[Any], None] | None = None,
    on_guard_ready: Callable[[dict[str, Any]], None] | None = None,
    saved_identity_verified: bool = False,
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
        validate_job_config(config, expected_split=isinstance(thresholds, SplitThresholds),
                            expected_shared_roles=isinstance(thresholds, SharedRoleThresholds),
                            expected_shared_caps=isinstance(thresholds, SharedCapsThresholds),
                            saved_identity_verified=saved_identity_verified)
        hooks = {}
        if on_job_created is not None:
            hooks["on_job_created"] = on_job_created
        if on_sandbox_created is not None:
            hooks["on_sandbox_created"] = on_sandbox_created
        if before_job_run is not None:
            hooks["before_job_run"] = before_job_run
        if on_guard_ready is not None:
            hooks["on_guard_ready"] = on_guard_ready
        with private_world_bind_mount_adapter():
            trial_failure = _trial_failure_reason(
                asyncio.run(run_guarded_job(
                    config, thresholds, saved_identity_verified=saved_identity_verified, **hooks
                )),
                verifier_disabled=config.verifier.disable,
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
    if config.verifier.disable:
        print("guarded_harbor_job=complete grading=disabled")
    else:
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

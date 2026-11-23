"""Opt-in saved-setup checks for a guarded fresh Harbor job.

This wraps the existing guarded runner; it does not select Muse or native defaults.
Save and verify validate the public scenario schema, not package startup health or launch approval.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import io
import json
import os
import re
import stat
import struct
import subprocess
import sys
import tomllib
import zipfile
import zlib
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

import yaml
from harbor.models.job.config import JobConfig
from pydantic import ValidationError

from refund_eval.config import resolve_cost_limits
from refund_eval.harbor import batch
from refund_eval.output_paths import validate_private_output
from refund_eval.scenario.schema import AgentScenario
from scripts import guarded_harbor
from scripts.offline_spend_guard import KEY_CEILING, Thresholds

REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_SCHEMA = 2
_LAURA_SCENARIO = "train_refund_late"
_PUBLIC_VALIDATION_CHECKER = "refund_eval.scenario.schema.AgentScenario.model_validate"
_CACHE_SUFFIXES = {".pyc", ".pyo"}
_SOURCE_DIRECTORIES = ("refund_eval", "scripts", "scout")
_SOURCE_FILES = (
    ".python-version",
    "pyproject.toml",
    "uv.lock",
    "integrations/harbor/pyproject.toml",
    "integrations/harbor/uv.lock",
)


class SetupFailure(ValueError):
    pass


def _entry_path(prefix: str, relative: str) -> str:
    if relative == ".":
        return prefix or "."
    return f"{prefix}/{relative}" if prefix else relative


def _tree_entries(root: Path, *, prefix: str = "") -> list[dict[str, Any]]:
    try:
        root_stat = root.lstat()
    except OSError:
        raise SetupFailure from None
    if not stat.S_ISDIR(root_stat.st_mode):
        raise SetupFailure

    entries: list[dict[str, Any]] = []

    def visit(path: Path, relative: str) -> None:
        try:
            item_stat = path.lstat()
        except OSError:
            raise SetupFailure from None
        if stat.S_ISLNK(item_stat.st_mode):
            raise SetupFailure
        if stat.S_ISDIR(item_stat.st_mode):
            if relative != "." and path.name == "__pycache__":
                return
            entries.append({
                "path": _entry_path(prefix, relative),
                "type": "directory",
                "executable_mode": stat.S_IMODE(item_stat.st_mode) & 0o111,
            })
            try:
                children = sorted(path.iterdir(), key=lambda child: child.name)
            except OSError:
                raise SetupFailure from None
            for child in children:
                child_relative = child.name if relative == "." else f"{relative}/{child.name}"
                visit(child, child_relative)
            return
        if not stat.S_ISREG(item_stat.st_mode):
            raise SetupFailure
        if path.suffix in _CACHE_SUFFIXES:
            return
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            raise SetupFailure from None
        entries.append({
            "path": _entry_path(prefix, relative),
            "type": "file",
            "size": size,
            "sha256": digest.hexdigest(),
            "executable_mode": stat.S_IMODE(item_stat.st_mode) & 0o111,
        })

    visit(root, ".")
    return entries


def _file_entry(path: Path, relative: str) -> dict[str, Any]:
    try:
        item_stat = path.lstat()
        if not stat.S_ISREG(item_stat.st_mode):
            raise SetupFailure
        data = path.read_bytes()
    except OSError:
        raise SetupFailure from None
    return {
        "path": relative,
        "type": "file",
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "executable_mode": stat.S_IMODE(item_stat.st_mode) & 0o111,
    }


def _looks_like_local_path(value: Any) -> bool:
    return isinstance(value, str) and (
        value.startswith((".", "/", "~")) or "/" in value or "\\" in value or "$" in value
    )


def _has_local_volume_reference(value: Any) -> bool:
    if isinstance(value, str):
        source, separator, _ = value.partition(":")
        return bool(separator) and _looks_like_local_path(source)
    if isinstance(value, dict):
        return value.get("type") == "bind" or _looks_like_local_path(value.get("source"))
    raise SetupFailure


def _validate_compose_overlay(path: Path) -> None:
    try:
        compose = yaml.safe_load(path.read_bytes())
    except (OSError, yaml.YAMLError):
        raise SetupFailure from None
    if not isinstance(compose, dict):
        raise SetupFailure
    if compose.get("include"):
        raise SetupFailure

    services = compose.get("services", {})
    if not isinstance(services, dict):
        raise SetupFailure
    for service in services.values():
        if not isinstance(service, dict):
            raise SetupFailure
        if any(service.get(key) for key in ("build", "develop", "env_file", "label_file")):
            raise SetupFailure
        extends = service.get("extends")
        if isinstance(extends, dict) and extends.get("file"):
            raise SetupFailure
        volumes = service.get("volumes", [])
        if isinstance(volumes, list) and any(
            _has_local_volume_reference(mount) for mount in volumes
        ):
            raise SetupFailure

    for resource_type in ("configs", "secrets"):
        resources = compose.get(resource_type, {})
        if not isinstance(resources, dict):
            raise SetupFailure
        if any(
            isinstance(resource, dict) and resource.get("file")
            for resource in resources.values()
        ):
            raise SetupFailure

    volumes = compose.get("volumes", {})
    if not isinstance(volumes, dict):
        raise SetupFailure
    if any(
        isinstance(volume, dict) and volume.get("driver_opts")
        for volume in volumes.values()
    ):
        raise SetupFailure


def _digest_entries(entries: list[dict[str, Any]]) -> str:
    data = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def _source_identity() -> str:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            [
                "git", "-C", str(REPO_ROOT), "ls-files", "--cached", "--others",
                "--exclude-standard", "-z", "--", *_SOURCE_DIRECTORIES,
            ],
            capture_output=True,
            check=False,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise SetupFailure from None
    if result.returncode:
        raise SetupFailure
    paths = {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}
    paths.update(_SOURCE_FILES)
    paths = {
        path for path in paths
        if "__pycache__" not in Path(path).parts and Path(path).suffix not in _CACHE_SUFFIXES
    }
    entries = [_file_entry(REPO_ROOT / relative, relative) for relative in sorted(paths)]
    return _digest_entries(entries)


def _runtime_identity() -> dict[str, Any]:
    try:
        harbor_version = importlib.metadata.version("harbor")
        modal_version = importlib.metadata.version("modal")
    except importlib.metadata.PackageNotFoundError:
        raise SetupFailure from None
    if harbor_version != guarded_harbor.PINNED_HARBOR_VERSION:
        raise SetupFailure
    packages = {}
    for name, version in (("harbor", harbor_version), ("modal", modal_version)):
        try:
            module = importlib.import_module(name)
            package_root = Path(module.__file__).parent
        except (ImportError, AttributeError, TypeError):
            raise SetupFailure from None
        packages[name] = {
            "version": version,
            "sha256": _digest_entries(_tree_entries(package_root)),
        }
    return {"python": sys.version.split()[0], "packages": packages}


def _anchor_path(
    path: Path,
    base: Path,
    *,
    strict: bool,
    reject_symlink: bool = False,
) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    if reject_symlink and candidate.is_symlink():
        raise SetupFailure
    try:
        return candidate.resolve(strict=strict)
    except OSError:
        raise SetupFailure from None


def _load_config(path: Path, path_base: Path) -> tuple[Path, bytes, JobConfig]:
    try:
        requested = path.expanduser()
        if requested.is_symlink():
            raise SetupFailure
        config_path = requested.resolve(strict=True)
        item_stat = config_path.lstat()
        if not stat.S_ISREG(item_stat.st_mode):
            raise SetupFailure
        config_bytes = config_path.read_bytes()
        config = JobConfig.model_validate(yaml.safe_load(config_bytes))
    except (OSError, ValueError, TypeError, yaml.YAMLError, ValidationError):
        raise SetupFailure from None

    config = config.model_copy(deep=True)
    config.jobs_dir = _anchor_path(config.jobs_dir, path_base, strict=False)
    config.extra_instruction_paths = [
        _anchor_path(instruction, path_base, strict=True, reject_symlink=True)
        for instruction in config.extra_instruction_paths
    ]
    for task in config.tasks:
        if task.path is not None:
            task.path = _anchor_path(
                task.path, path_base, strict=True, reject_symlink=True
            )
    config.environment.extra_docker_compose = [
        _anchor_path(path, path_base, strict=True, reject_symlink=True)
        for path in config.environment.extra_docker_compose
    ]
    for agent in config.agents:
        skills = []
        for skill in agent.skills:
            if "://" in skill or skill.startswith(("git+", "github:")):
                raise SetupFailure
            skill_path = _anchor_path(Path(skill), path_base, strict=True, reject_symlink=True)
            if not skill_path.is_dir():
                raise SetupFailure
            skills.append(str(skill_path))
        agent.skills = skills
    return config_path, config_bytes, config


def _config_inputs(config: JobConfig) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    for index, path in enumerate(config.extra_instruction_paths):
        entry = _file_entry(path, str(path))
        entry.update({"kind": "extra_instruction", "index": index})
        inputs.append(entry)
    for index, path in enumerate(config.environment.extra_docker_compose):
        _validate_compose_overlay(path)
        entry = _file_entry(path, str(path))
        entry.update({"kind": "extra_docker_compose", "index": index})
        inputs.append(entry)
    for agent_index, agent in enumerate(config.agents):
        for skill_index, skill in enumerate(agent.skills):
            path = Path(skill)
            entries = _tree_entries(path)
            inputs.append({
                "kind": "agent_skill",
                "agent_index": agent_index,
                "skill_index": skill_index,
                "path": str(path),
                "entries": entries,
                "sha256": _digest_entries(entries),
            })
    return inputs


def _job_path_exists(config: JobConfig, job_name: str) -> bool:
    path = config.jobs_dir / job_name
    return path.exists() or path.is_symlink()


def _validate_with_fresh_name(config: JobConfig) -> None:
    for _ in range(3):
        name = f"laura-verify-{uuid4().hex}"
        if _job_path_exists(config, name):
            continue
        candidate = config.model_copy(deep=True)
        candidate.job_name = name
        try:
            guarded_harbor.validate_job_config(candidate)
            return
        except guarded_harbor.LauncherFailure:
            raise SetupFailure from None
    raise SetupFailure


def _task_identity(task_path: Path) -> dict[str, Any]:
    entries = _tree_entries(task_path)
    return {"path": str(task_path), "entries": entries, "sha256": _digest_entries(entries)}


def _public_validation_result(task_path: Path) -> dict[str, str]:
    agent_dir = task_path / "environment" / "agent"
    wheel_manifest_path = agent_dir / "agent-wheel.json"
    task_manifest_path = task_path / "task.toml"
    try:
        if not stat.S_ISREG(wheel_manifest_path.lstat().st_mode):
            raise SetupFailure
        wheel_manifest = json.loads(
            wheel_manifest_path.read_bytes(), object_pairs_hook=_unique_object
        )
        if not stat.S_ISREG(task_manifest_path.lstat().st_mode):
            raise SetupFailure
        task_metadata = tomllib.loads(task_manifest_path.read_text(encoding="utf-8"))["metadata"]
        if not isinstance(wheel_manifest, dict) or set(wheel_manifest) != {"filename", "sha256"}:
            raise SetupFailure
        if not isinstance(task_metadata, dict) or task_metadata.get("scenario") != _LAURA_SCENARIO:
            raise SetupFailure
        filename = wheel_manifest["filename"]
        expected_sha256 = wheel_manifest["sha256"]
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]*\.whl", filename):
            raise SetupFailure
        if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise SetupFailure
        if (task_metadata.get("agent_wheel_filename") != filename
                or task_metadata.get("agent_wheel_sha256") != expected_sha256):
            raise SetupFailure

        wheel_path = agent_dir / "wheels" / filename
        if not stat.S_ISREG(wheel_path.lstat().st_mode):
            raise SetupFailure
        wheel_bytes = wheel_path.read_bytes()
        wheel_sha256 = hashlib.sha256(wheel_bytes).hexdigest()
        if wheel_sha256 != expected_sha256:
            raise SetupFailure

        scenario_member = f"refund_eval/scenario/{task_metadata['scenario']}.yaml"
        with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as archive:
            members = archive.infolist()
            matches = [info for info in members if info.filename == scenario_member]
            scenario_parts = PurePosixPath(scenario_member).parts
            aliases = []
            for info in members:
                parts = PurePosixPath(info.filename).parts
                for index in range(max(0, len(parts) - len(scenario_parts) - 1)):
                    if (parts[index].endswith(".data")
                            and parts[index + 1] in {"purelib", "platlib"}
                            and parts[index + 2:] == scenario_parts):
                        aliases.append(info)
                        break
            if len(matches) != 1 or aliases:
                raise SetupFailure
            scenario_bytes = archive.read(matches[0])
        scenario = AgentScenario.model_validate(yaml.safe_load(scenario_bytes))
        if scenario.id != task_metadata["scenario"]:
            raise SetupFailure
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, yaml.YAMLError,
            ValidationError, zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError,
            EOFError, struct.error, zlib.error):
        raise SetupFailure from None

    return {
        "status": "passed",
        "checker": _PUBLIC_VALIDATION_CHECKER,
        "scenario": task_metadata["scenario"],
        "wheel_path": wheel_path.relative_to(task_path).as_posix(),
        "wheel_sha256": wheel_sha256,
        "scenario_member": scenario_member,
        "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
    }


def _require_task_wheel_match(
    task: dict[str, Any], public_validation: dict[str, str]
) -> None:
    matches = [entry for entry in task["entries"] if entry.get("path") == public_validation["wheel_path"]]
    if (len(matches) != 1
            or matches[0].get("sha256") != public_validation["wheel_sha256"]):
        raise SetupFailure


def _manifest_path(path: Path) -> Path:
    requested = path.expanduser()
    if requested.name in {"", ".", ".."}:
        raise SetupFailure
    absolute = requested if requested.is_absolute() else Path.cwd() / requested
    try:
        parent = validate_private_output(absolute.parent)
    except (OSError, ValueError):
        raise SetupFailure from None
    return parent / absolute.name


def _reject_manifest_overlap(
    manifest_path: Path, config_path: Path, config: JobConfig
) -> None:
    output = manifest_path.resolve(strict=False)
    roots = [
        task.path for task in config.tasks if task.path is not None
    ] + [Path(skill) for agent in config.agents for skill in agent.skills]
    if any(output == root or root in output.parents for root in roots):
        raise SetupFailure
    files = [
        config_path,
        *config.extra_instruction_paths,
        *config.environment.extra_docker_compose,
    ]
    if output in files:
        raise SetupFailure


def _manifest_document(
    config_path: Path,
    config_bytes: bytes,
    config: JobConfig,
    path_base: Path,
) -> dict[str, Any]:
    if not config.tasks or config.tasks[0].path is None:
        raise SetupFailure
    task_path = config.tasks[0].path
    if config.jobs_dir == task_path or task_path in config.jobs_dir.parents:
        raise SetupFailure
    guarded_harbor.require_pinned_harbor()
    _validate_with_fresh_name(config)
    task_identity = _task_identity(task_path)
    public_validation = _public_validation_result(task_path)
    _require_task_wheel_match(task_identity, public_validation)
    return {
        "schema_version": _MANIFEST_SCHEMA,
        "path_base": str(path_base),
        "config": {
            "path": str(config_path),
            "size": len(config_bytes),
            "sha256": hashlib.sha256(config_bytes).hexdigest(),
        },
        "task": task_identity,
        "config_inputs": _config_inputs(config),
        "current_source": {"sha256": _source_identity()},
        "runtime": _runtime_identity(),
        "public_validation": public_validation,
    }


def _write_manifest(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise SetupFailure from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
    except OSError:
        raise SetupFailure from None


def save_setup(config_path: Path, manifest_path: Path) -> None:
    output = _manifest_path(manifest_path)
    if output.exists() or output.is_symlink():
        raise SetupFailure
    path_base = Path.cwd().resolve()
    config_path, config_bytes, config = _load_config(config_path, path_base)
    _reject_manifest_overlap(output, config_path, config)
    document = _manifest_document(config_path, config_bytes, config, path_base)
    _write_manifest(output, document)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _read_manifest(path: Path) -> dict[str, Any]:
    manifest_path = _manifest_path(path)
    try:
        item_stat = manifest_path.lstat()
        if not stat.S_ISREG(item_stat.st_mode):
            raise SetupFailure
        document = json.loads(manifest_path.read_bytes(), object_pairs_hook=_unique_object)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        raise SetupFailure from None
    keys = {
        "schema_version", "path_base", "config", "task", "config_inputs",
        "current_source", "runtime", "public_validation",
    }
    if not isinstance(document, dict) or set(document) != keys:
        raise SetupFailure
    if type(document["schema_version"]) is not int or document["schema_version"] != _MANIFEST_SCHEMA:
        raise SetupFailure
    if not isinstance(document["path_base"], str) or not Path(document["path_base"]).is_absolute():
        raise SetupFailure
    if not isinstance(document["config"], dict) or set(document["config"]) != {"path", "size", "sha256"}:
        raise SetupFailure
    config_path = document["config"]["path"]
    digest = document["config"]["sha256"]
    if not isinstance(config_path, str) or not Path(config_path).is_absolute():
        raise SetupFailure
    if not isinstance(document["config"]["size"], int) or not isinstance(digest, str) or len(digest) != 64:
        raise SetupFailure
    if not isinstance(document["task"], dict) or set(document["task"]) != {"path", "entries", "sha256"}:
        raise SetupFailure
    if not isinstance(document["task"]["path"], str) or not Path(document["task"]["path"]).is_absolute():
        raise SetupFailure
    if not isinstance(document["task"]["entries"], list) or not isinstance(document["task"]["sha256"], str):
        raise SetupFailure
    if not isinstance(document["config_inputs"], list):
        raise SetupFailure
    if not isinstance(document["current_source"], dict) or set(document["current_source"]) != {"sha256"}:
        raise SetupFailure
    if not isinstance(document["runtime"], dict):
        raise SetupFailure
    public_validation = document["public_validation"]
    validation_keys = {
        "status", "checker", "scenario", "wheel_path", "wheel_sha256",
        "scenario_member", "scenario_sha256",
    }
    if not isinstance(public_validation, dict) or set(public_validation) != validation_keys:
        raise SetupFailure
    if (public_validation["status"] != "passed"
            or public_validation["checker"] != _PUBLIC_VALIDATION_CHECKER
            or public_validation["scenario"] != _LAURA_SCENARIO):
        raise SetupFailure
    if (not isinstance(public_validation["wheel_path"], str)
            or Path(public_validation["wheel_path"]).is_absolute()
            or ".." in Path(public_validation["wheel_path"]).parts
            or public_validation["scenario_member"] != f"refund_eval/scenario/{_LAURA_SCENARIO}.yaml"):
        raise SetupFailure
    for key in ("wheel_sha256", "scenario_sha256"):
        if (not isinstance(public_validation[key], str)
                or not re.fullmatch(r"[0-9a-f]{64}", public_validation[key])):
            raise SetupFailure
    return document


def verify_setup(manifest_path: Path) -> JobConfig:
    document = _read_manifest(manifest_path)
    path_base = Path(document["path_base"])
    config_path, config_bytes, config = _load_config(
        Path(document["config"]["path"]), path_base
    )
    actual_config = {
        "path": str(config_path),
        "size": len(config_bytes),
        "sha256": hashlib.sha256(config_bytes).hexdigest(),
    }
    if actual_config != document["config"]:
        raise SetupFailure
    actual_task = _task_identity(config.tasks[0].path) if config.tasks and config.tasks[0].path else None
    if actual_task != document["task"]:
        raise SetupFailure
    actual_public_validation = _public_validation_result(config.tasks[0].path)
    _require_task_wheel_match(actual_task, actual_public_validation)
    if actual_public_validation != document["public_validation"]:
        raise SetupFailure
    if _config_inputs(config) != document["config_inputs"]:
        raise SetupFailure
    if {"sha256": _source_identity()} != document["current_source"]:
        raise SetupFailure
    if _runtime_identity() != document["runtime"]:
        raise SetupFailure
    guarded_harbor.require_pinned_harbor()
    _validate_with_fresh_name(config)
    return config


def prepare_fresh_job(manifest_path: Path, job_name: str) -> JobConfig:
    config = verify_setup(manifest_path)
    if not job_name or _job_path_exists(config, job_name):
        raise SetupFailure
    fresh = config.model_copy(deep=True)
    fresh.job_name = job_name
    try:
        fresh.jobs_dir = validate_private_output(fresh.jobs_dir)
    except (OSError, ValueError):
        raise SetupFailure from None
    try:
        guarded_harbor.validate_job_config(fresh)
    except guarded_harbor.LauncherFailure:
        raise SetupFailure from None
    return fresh


def _print_local_job_log(job: Any) -> None:
    path = Path(job._log_output_path).resolve()
    print(f"harbor_job_log_uri={path.as_uri()}", flush=True)


def _print_modal_dashboard(environment: Any) -> None:
    app = getattr(environment, "_app", None)
    get_dashboard_url = getattr(app, "get_dashboard_url", None)
    if not callable(get_dashboard_url):
        return
    url = None
    with suppress(Exception):
        url = get_dashboard_url()
    if isinstance(url, str) and url:
        print(f"modal_dashboard_url={url}", flush=True)


def _decimal_argument(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, ValueError):
        raise argparse.ArgumentTypeError("expected a finite USD amount") from None
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("expected a positive finite USD amount")
    return amount


def _amount(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() and amount > 0 else None


def _amount_text(value: Any) -> str | None:
    amount = _amount(value)
    return format(amount, "f") if amount is not None else None


def _guard_parameters(parameters: dict[str, Any], thresholds: Thresholds, total: int | None) -> dict[str, Any]:
    return {
        **parameters,
        "total_time_limit_seconds": total,
        "openrouter_trial_delta_usd": str(thresholds.openrouter_trial_delta),
        "openrouter_key_ceiling_usd": str(thresholds.openrouter_key_ceiling),
        "cleanup_timeout_seconds": thresholds.cleanup_timeout,
    }


def _validate_total_limit(total: int | None, thresholds: Thresholds) -> None:
    if total is not None and (type(total) is not int or total <= thresholds.cleanup_timeout):
        raise SetupFailure("total_deadline_must_exceed_cleanup_timeout")


def _native_targets(config: batch.NativeBatchConfig) -> list[str]:
    targets = config.models or [config.model]
    if len(targets) != 1:
        raise SetupFailure("target_model_sweep_unsupported")
    return targets


def _native_effective_parameters(
    config: batch.NativeBatchConfig, thresholds: Thresholds
) -> dict[str, Any]:
    targets = _native_targets(config)
    if config.execution != "modal":
        raise SetupFailure("guarded_native_runs_require_modal")
    if config.trials != 1 or config.concurrency != 1:
        raise SetupFailure("guarded_native_runs_require_one_attempt_and_concurrency")
    if config.inbox_snapshot is not None or config.resume_log is not None:
        raise SetupFailure("guarded_native_runs_do_not_support_resume_or_inbox")
    roles = [targets[0], config.simulator_model, config.grader_model]
    if any(not isinstance(model, str) or not model.startswith("openrouter/") for model in roles):
        raise SetupFailure("all_active_models_must_use_openrouter")

    costs = resolve_cost_limits(config.cost_limit, targets, scenario=config.scenario)
    agent_cost = _amount(costs[targets[0]])
    world_cost = _amount(config.world_cost_limit)
    if agent_cost is None or world_cost is None:
        raise SetupFailure("native_cost_limits_must_be_finite")
    runtime_delta = thresholds.openrouter_trial_delta
    if agent_cost + world_cost > runtime_delta:
        raise SetupFailure("agent_and_world_caps_exceed_runtime_trial_delta")
    if config.trial_spend_cap_usd is not None:
        trial_cap = _amount(config.trial_spend_cap_usd)
        key_cap = _amount(config.key_spend_cap_usd)
        if trial_cap is None or key_cap is None:
            raise SetupFailure("native_planning_caps_must_be_finite")
        if trial_cap > runtime_delta or trial_cap < agent_cost + world_cost:
            raise SetupFailure("native_trial_planning_cap_conflicts_with_runtime_guard")
        if key_cap > KEY_CEILING or config.trials * trial_cap > key_cap:
            raise SetupFailure("native_key_planning_cap_exceeds_supported_guard")

    worker_time_limit = config.time_limit
    if config.total_time_limit_seconds is not None:
        worker_time_limit = min(worker_time_limit, config.total_time_limit_seconds)
    return {
        "target_model": targets[0],
        "models": targets,
        "simulator_model": config.simulator_model,
        "grader_model": config.grader_model,
        "sim_weeks": config.sim_weeks,
        "max_turns": config.max_turns,
        "agent_cost_limit_usd": format(agent_cost, "f"),
        "world_cost_limit_usd": format(world_cost, "f"),
        "worker_time_limit_seconds": worker_time_limit,
        "effective_parameters_complete": True,
        "unavailable_fields": [],
    }


_WORLD_ENVIRONMENT_FIELDS = {
    "WORLD_COST_LIMIT",
    "WORLD_SIMULATOR_MODEL",
    "WORLD_GRADER_MODEL",
    "WORLD_SIM_WEEKS",
    "WORLD_MAX_TURNS",
}


def _compose_environment_values(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    values: dict[str, Any] = {}
    if isinstance(value, dict):
        items = value.items()
        for name, item in items:
            if not isinstance(name, str):
                raise SetupFailure("saved_world_environment_unresolvable")
            if name not in _WORLD_ENVIRONMENT_FIELDS:
                continue
            if item is not None and (
                isinstance(item, bool) or not isinstance(item, (str, int, float))
            ):
                raise SetupFailure("saved_world_environment_unresolvable")
            values[name] = item
    elif isinstance(value, list):
        for item in value:
            if not isinstance(item, str):
                raise SetupFailure("saved_world_environment_unresolvable")
            name, separator, item_value = item.partition("=")
            if name in _WORLD_ENVIRONMENT_FIELDS:
                values[name] = item_value if separator else None
    else:
        raise SetupFailure("saved_world_environment_unresolvable")
    return values


def _world_environment(
    task_path: Path, config: JobConfig, task_environment: dict[str, Any]
) -> dict[str, Any]:
    compose_path = task_path / "environment" / "docker-compose.yaml"
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise SetupFailure("saved_world_environment_unresolvable") from None
    services = compose.get("services") if isinstance(compose, dict) else None
    world = services.get("world") if isinstance(services, dict) else None
    base = world.get("environment") if isinstance(world, dict) else None
    environment = _compose_environment_values(base)
    for overlay_path in config.environment.extra_docker_compose:
        try:
            overlay = yaml.safe_load(Path(overlay_path).read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raise SetupFailure("saved_compose_overlay_unreadable") from None
        overlay_services = overlay.get("services") if isinstance(overlay, dict) else None
        overlay_world = overlay_services.get("world") if isinstance(overlay_services, dict) else None
        if overlay_world is None:
            continue
        if not isinstance(overlay_world, dict):
            raise SetupFailure("saved_world_overlay_unresolvable")
        overrides = overlay_world.get("environment")
        if overrides is None:
            continue
        environment.update(_compose_environment_values(overrides))

    job_environment = config.environment.env

    def configured_value(name: str) -> str | None:
        for source in (job_environment, task_environment):
            if name not in source:
                continue
            value = source[name]
            if not isinstance(value, str) or "$" in value:
                raise SetupFailure("saved_world_environment_unresolvable")
            return value
        return None

    effective: dict[str, Any] = {}
    for name, value in environment.items():
        if value is None:
            value = configured_value(name)
        elif isinstance(value, str) and "$" in value:
            match = re.fullmatch(
                r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)",
                value,
            )
            if match is None:
                raise SetupFailure("saved_world_environment_unresolvable")
            reference = match.group(1) or match.group(2)
            if reference not in _WORLD_ENVIRONMENT_FIELDS:
                raise SetupFailure("saved_world_environment_unresolvable")
            value = configured_value(reference)
        if value is None:
            raise SetupFailure("saved_world_environment_unresolvable")
        effective[name] = value
    return effective


def _saved_effective_parameters(
    config: JobConfig, thresholds: Thresholds
) -> dict[str, Any]:
    if len(config.agents) != 1 or len(config.tasks) != 1 or not config.tasks[0].path:
        raise SetupFailure("saved_native_values_unavailable")
    target = config.agents[0].model_name
    if not target.startswith("openrouter/"):
        raise SetupFailure("saved_target_model_must_use_openrouter")
    task_path = Path(config.tasks[0].path)
    try:
        task_config = tomllib.loads(
            (task_path / "task.toml").read_text(encoding="utf-8")
        )
    except (OSError, tomllib.TOMLDecodeError):
        task_config = {}
    task_metadata = task_config.get("metadata", {})
    if not isinstance(task_metadata, dict):
        task_metadata = {}
    task_environment = task_config.get("environment", {}).get("env", {})
    if not isinstance(task_environment, dict) or not isinstance(config.environment.env, dict):
        raise SetupFailure("saved_world_environment_unresolvable")
    world_env = _world_environment(task_path, config, task_environment)
    native_task = (
        task_metadata.get("scenario") == _LAURA_SCENARIO
        and isinstance(task_metadata.get("agent_wheel_sha256"), str)
    )

    def effective_role(environment_name: str, metadata_name: str) -> Any:
        value = world_env.get(environment_name)
        return task_metadata.get(metadata_name) if value is None else value

    simulator = effective_role("WORLD_SIMULATOR_MODEL", "simulator_model")
    grader = effective_role("WORLD_GRADER_MODEL", "grader_model")
    for model in (simulator, grader):
        if model is not None and (not isinstance(model, str) or not model.startswith("openrouter/")):
            raise SetupFailure("saved_role_models_must_use_openrouter")
    if native_task and (simulator is None or grader is None):
        raise SetupFailure("saved_native_role_models_unavailable")

    def positive_number(value: Any) -> float | None:
        amount = _amount(value)
        return float(amount) if amount is not None else None

    def positive_integer(value: Any) -> int | None:
        amount = _amount(value)
        if amount is None or amount != amount.to_integral_value():
            return None
        return int(amount)

    sim_weeks_raw = world_env.get("WORLD_SIM_WEEKS")
    if sim_weeks_raw is None:
        sim_weeks_raw = task_metadata.get("sim_weeks")
    max_turns_raw = world_env.get("WORLD_MAX_TURNS")
    if max_turns_raw is None:
        max_turns_raw = task_metadata.get("max_turns")
    sim_weeks = positive_number(sim_weeks_raw)
    max_turns = positive_integer(max_turns_raw)
    if sim_weeks_raw is not None and sim_weeks is None:
        raise SetupFailure("saved_sim_weeks_invalid")
    if max_turns_raw is not None and max_turns is None:
        raise SetupFailure("saved_max_turns_invalid")

    agent_kwargs = config.agents[0].kwargs
    agent_cap_raw = agent_kwargs.get("cost_limit")
    if isinstance(agent_cap_raw, dict):
        agent_cap_raw = agent_cap_raw.get(target)
    world_cap_raw = world_env.get("WORLD_COST_LIMIT")
    agent_amount = _amount(agent_cap_raw)
    world_amount = _amount(world_cap_raw)
    if (
        agent_amount is not None
        and world_amount is not None
        and agent_amount + world_amount > thresholds.openrouter_trial_delta
    ):
        raise SetupFailure("saved_agent_and_world_caps_exceed_runtime_trial_delta")
    worker_time_raw = agent_kwargs.get("time_limit")
    worker_time = positive_integer(worker_time_raw)
    if worker_time_raw is not None and worker_time is None:
        raise SetupFailure("saved_worker_time_limit_invalid")
    unknown = []
    for name, value in (
        ("simulator_model", simulator),
        ("grader_model", grader),
        ("sim_weeks", sim_weeks),
        ("max_turns", max_turns),
        ("agent_cost_limit_usd", agent_amount),
        ("world_cost_limit_usd", world_amount),
        ("worker_time_limit_seconds", worker_time),
    ):
        if value is None:
            unknown.append(name)
    if native_task and unknown:
        raise SetupFailure("saved_native_effective_values_invalid_or_unavailable")
    if agent_cap_raw is not None and agent_amount is None:
        raise SetupFailure("saved_agent_cost_limit_invalid")
    if world_cap_raw is not None and world_amount is None:
        raise SetupFailure("saved_world_cost_limit_invalid")
    return {
        "target_model": target,
        "models": [target],
        "simulator_model": simulator,
        "grader_model": grader,
        "sim_weeks": sim_weeks,
        "max_turns": max_turns,
        "agent_cost_limit_usd": _amount_text(agent_amount),
        "world_cost_limit_usd": _amount_text(world_amount),
        "worker_time_limit_seconds": worker_time,
        "effective_parameters_complete": not unknown,
        "unavailable_fields": unknown,
    }


def _record_effective_parameters(job: Any, config: JobConfig, parameters: dict[str, Any]) -> None:
    del job
    job_dir = Path(config.jobs_dir) / config.job_name
    try:
        job_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        job_dir = validate_private_output(job_dir)
        record = job_dir / "effective_parameters.json"
        descriptor = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(parameters, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, TypeError, ValueError):
        raise SetupFailure("effective_parameters_record_write_failed") from None


def _update_native_run_record(record_path: Path, parameters: dict[str, Any]) -> None:
    try:
        run_record = json.loads(record_path.read_text(encoding="utf-8"))
        if not isinstance(run_record, dict):
            raise TypeError
        run_record["effective_parameters"] = parameters
        record_path.write_text(json.dumps(run_record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError):
        raise SetupFailure("native_run_record_write_failed") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    save = subparsers.add_parser("save", help="snapshot a prepared JobConfig identity")
    save.add_argument("--config", type=Path, required=True)
    save.add_argument("--manifest", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="compare the saved setup identity offline")
    verify.add_argument("--manifest", type=Path, required=True)
    run = subparsers.add_parser("run", help="run one fresh job through the guarded runner")
    setup = run.add_mutually_exclusive_group(required=True)
    setup.add_argument("--manifest", type=Path)
    setup.add_argument("--run-config", type=Path)
    run.add_argument("--prepared-dir", type=Path)
    run.add_argument("--job-name", required=True)
    run.add_argument("--openrouter-key-ceiling", type=_decimal_argument, required=True)
    run.add_argument("--openrouter-trial-delta", type=_decimal_argument, required=True)
    run.add_argument("--total-time-limit-seconds", type=int)
    run.add_argument("--probe-timeout", type=float, default=10.0)
    run.add_argument("--poll-interval", type=float, default=15.0)
    run.add_argument("--max-sample-age", type=float, default=35.0)
    run.add_argument("--cleanup-timeout", type=float, default=10.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "save":
            save_setup(args.config, args.manifest)
            print("laura_setup_saved=identity_and_public_schema public_validation=true startup_validated=false launch_approval=false")
            return 0
        if args.command == "verify":
            verify_setup(args.manifest)
            print("laura_setup_verified=identity_and_public_schema public_validation=true startup_validated=false launch_approval=false")
            return 0
    except (OSError, ValueError, TypeError, ImportError, yaml.YAMLError, guarded_harbor.LauncherFailure):
        print("laura_setup_failed=identity_or_job_validation", file=sys.stderr)
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
    except (ValueError, TypeError) as failure:
        print(f"launcher_failed={type(failure).__name__} cleanup_uncertain=false", file=sys.stderr)
        return 1

    try:
        if args.manifest is not None:
            if args.prepared_dir is not None:
                raise SetupFailure("prepared_dir_is_only_valid_with_run_config")
            total_time_limit = args.total_time_limit_seconds
            _validate_total_limit(total_time_limit, thresholds)
            config = prepare_fresh_job(args.manifest, args.job_name)
            parameters = _saved_effective_parameters(config, thresholds)
        else:
            if args.prepared_dir is None:
                raise SetupFailure("prepared_dir_required_for_run_config")
            native_config = batch.NativeBatchConfig.model_validate_json(
                args.run_config.read_bytes()
            )
            if (
                args.total_time_limit_seconds is not None
                and args.total_time_limit_seconds != native_config.total_time_limit_seconds
            ):
                raise SetupFailure("conflicting_total_time_limits")
            total_time_limit = native_config.total_time_limit_seconds
            _validate_total_limit(total_time_limit, thresholds)
            parameters = _native_effective_parameters(native_config, thresholds)
            run_record_path = batch.prepare(args.prepared_dir, native_config)
            effective = _guard_parameters(parameters, thresholds, total_time_limit)
            _update_native_run_record(run_record_path, effective)
            manifest_path = args.prepared_dir / "laura-setup.json"
            save_setup(run_record_path.with_name("job.yaml"), manifest_path)
            config = prepare_fresh_job(manifest_path, args.job_name)
            parameters = effective
    except SetupFailure as failure:
        reason = str(failure)
        if not reason or not reason.replace("_", "").isalnum():
            reason = "identity_or_job_validation"
        print(f"laura_setup_failed={reason}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, ImportError, yaml.YAMLError, ValidationError, guarded_harbor.LauncherFailure):
        print("laura_setup_failed=identity_or_job_validation", file=sys.stderr)
        return 2

    effective_parameters = _guard_parameters(parameters, thresholds, total_time_limit)
    print("laura_setup_verified=identity_and_public_schema public_validation=true startup_validated=false launch_approval=false", flush=True)
    return guarded_harbor.run_guarded_config(
        config,
        thresholds,
        on_job_created=_print_local_job_log,
        on_sandbox_created=_print_modal_dashboard,
        before_job_run=lambda job: _record_effective_parameters(job, config, effective_parameters),
        total_time_limit_seconds=total_time_limit,
    )


if __name__ == "__main__":
    raise SystemExit(main())

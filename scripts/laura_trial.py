"""Opt-in saved-setup identity checks for a guarded fresh Harbor job.

This wraps the existing guarded runner; it does not select Muse or native defaults.
Saving and verifying establish identity only, not package startup health or launch approval.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import stat
import subprocess
import sys
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from harbor.models.job.config import JobConfig
from pydantic import ValidationError

from refund_eval.output_paths import validate_private_output
from scripts import guarded_harbor
from scripts.offline_spend_guard import Thresholds

REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_SCHEMA = 1
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

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"build", "develop", "env_file", "include"} and item:
                    raise SetupFailure
                if key in {"file", "driver_opts"} and item:
                    raise SetupFailure
                if key == "volumes" and isinstance(item, list):
                    if any(_has_local_volume_reference(mount) for mount in item):
                        raise SetupFailure
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(compose)


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
    return {
        "schema_version": _MANIFEST_SCHEMA,
        "path_base": str(path_base),
        "config": {
            "path": str(config_path),
            "size": len(config_bytes),
            "sha256": hashlib.sha256(config_bytes).hexdigest(),
        },
        "task": _task_identity(task_path),
        "config_inputs": _config_inputs(config),
        "current_source": {"sha256": _source_identity()},
        "runtime": _runtime_identity(),
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
        "current_source", "runtime",
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    save = subparsers.add_parser("save", help="snapshot a prepared JobConfig identity")
    save.add_argument("--config", type=Path, required=True)
    save.add_argument("--manifest", type=Path, required=True)
    verify = subparsers.add_parser("verify", help="compare the saved setup identity offline")
    verify.add_argument("--manifest", type=Path, required=True)
    run = subparsers.add_parser("run", help="run one fresh job through the guarded runner")
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument("--job-name", required=True)
    run.add_argument("--openrouter-key-ceiling", type=_decimal_argument, required=True)
    run.add_argument("--openrouter-trial-delta", type=_decimal_argument, required=True)
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
            print("laura_setup_saved=identity_only public_validation=false startup_validated=false launch_approval=false")
            return 0
        if args.command == "verify":
            verify_setup(args.manifest)
            print("laura_setup_verified=identity_only public_validation=false startup_validated=false launch_approval=false")
            return 0
        config = prepare_fresh_job(args.manifest, args.job_name)
    except (OSError, ValueError, TypeError, ImportError, yaml.YAMLError, guarded_harbor.LauncherFailure):
        print("laura_setup_failed=identity_or_job_validation", file=sys.stderr)
        return 2

    print("laura_setup_verified=identity_only public_validation=false startup_validated=false launch_approval=false", flush=True)
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
    return guarded_harbor.run_guarded_config(
        config,
        thresholds,
        on_job_created=_print_local_job_log,
        on_sandbox_created=_print_modal_dashboard,
    )


if __name__ == "__main__":
    raise SystemExit(main())

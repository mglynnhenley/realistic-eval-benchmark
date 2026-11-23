"""Create and enforce explicit source, scenario and browser-runtime locks."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from refund_eval.environment_version import ENVIRONMENT_VERSION, environment_fingerprint
from refund_eval.scenario.schema import Scenario, load_scenario

LOCK_SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]


def _default_image() -> str:
    from refund_eval.published.browser_runtimes import DEFAULT_IMAGE

    return DEFAULT_IMAGE


class IdentityLockError(ValueError):
    """A locked run cannot use the current environment or runtime."""


def scenario_fingerprint(scenario: str | Scenario) -> str:
    """Hash the resolved scenario values; the environment hash covers its source assets."""
    resolved = scenario if isinstance(scenario, Scenario) else load_scenario(scenario)
    value = json.dumps(resolved.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def create_identity_lock(
    scenario: str, *, browser_runtime: str = "docker", browser_image: str | None = None,
) -> dict[str, Any]:
    resolved = load_scenario(scenario)
    if browser_runtime != "docker":
        raise IdentityLockError(
            "Exact browser artifact locks are supported only for a local Docker image ID; "
            "subprocess and Harbor-built artifacts are not identified."
        )
    from refund_eval.published.browser_runtimes import (
        BrowserBoundaryError,
        resolve_local_image_id,
    )

    browser_image = browser_image or _default_image()
    try:
        runtime_identity = resolve_local_image_id(browser_image)
    except BrowserBoundaryError as error:
        raise IdentityLockError(str(error)) from error
    runtime_identity_kind = "docker_image_id"
    return {
        "schema_version": LOCK_SCHEMA_VERSION,
        "environment_version": ENVIRONMENT_VERSION,
        "environment_fingerprint": environment_fingerprint(),
        "scenario": resolved.id,
        "scenario_fingerprint": scenario_fingerprint(scenario),
        "browser_runtime": browser_runtime,
        "runtime_identity_kind": runtime_identity_kind,
        "runtime_identity": runtime_identity,
        "browser_image_ref": browser_image,
    }


def current_run_identity(scenario: str | Scenario, *, browser_runtime: str) -> dict[str, Any]:
    """Record current inputs when no expected-identity lock was requested."""
    if browser_runtime == "docker":
        runtime_identity = _default_image()
        runtime_identity_kind = "docker_image_ref"
    elif browser_runtime == "subprocess":
        runtime_identity = None
        runtime_identity_kind = "artifact_unlocked"
    else:
        raise IdentityLockError(f"Unknown browser runtime: {browser_runtime}")
    resolved = scenario if isinstance(scenario, Scenario) else load_scenario(scenario)
    return {
        "environment_version": ENVIRONMENT_VERSION,
        "environment_fingerprint": environment_fingerprint(),
        "scenario": resolved.id,
        "scenario_fingerprint": scenario_fingerprint(resolved),
        "browser_runtime": browser_runtime,
        "runtime_identity_kind": runtime_identity_kind,
        "runtime_identity": runtime_identity,
        **({"browser_image_ref": runtime_identity} if browser_runtime == "docker" else {}),
    }


def load_identity_lock(value: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        lock = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise IdentityLockError("Identity lock is empty")
        if text.startswith("{"):
            lock = json.loads(text)
        else:
            try:
                path = Path(text).expanduser()
            except (OSError, RuntimeError) as error:
                raise IdentityLockError(f"Cannot resolve identity lock path: {text}") from error
            if not path.is_absolute() and not path.is_file():
                repo_path = ROOT / path
                if repo_path.is_file():
                    path = repo_path
            try:
                lock = json.loads(path.read_text(encoding="utf-8"))
            except OSError as error:
                raise IdentityLockError(f"Cannot read identity lock: {path}") from error
    else:
        raise IdentityLockError("Identity lock must be a manifest path or JSON object")
    if not isinstance(lock, dict):
        raise IdentityLockError("Identity lock must contain a JSON object")
    required = {
        "schema_version", "environment_version", "environment_fingerprint", "scenario",
        "scenario_fingerprint", "browser_runtime", "runtime_identity_kind", "runtime_identity",
    }
    missing = required - lock.keys()
    if missing:
        raise IdentityLockError("Identity lock is missing: " + ", ".join(sorted(missing)))
    if lock["schema_version"] != LOCK_SCHEMA_VERSION:
        raise IdentityLockError(f"Unsupported identity lock schema: {lock['schema_version']}")
    return lock


def validate_identity_lock(
    value: str | dict[str, Any], scenario: str | Scenario, *, browser_runtime: str,
) -> tuple[dict[str, Any], str | None]:
    """Validate before target/model setup and return the exact local image ID, if applicable."""
    if browser_runtime == "subprocess":
        raise IdentityLockError(
            "Exact browser artifact locks are unsupported for subprocess and Harbor-built runtimes."
        )
    if browser_runtime != "docker":
        raise IdentityLockError(f"Unknown browser runtime: {browser_runtime}")
    from refund_eval.published.browser_runtimes import (
        BrowserBoundaryError,
        resolve_local_image_id,
    )

    lock = load_identity_lock(value)
    resolved = scenario if isinstance(scenario, Scenario) else load_scenario(scenario)
    current = {
        "environment_version": ENVIRONMENT_VERSION,
        "environment_fingerprint": environment_fingerprint(),
        "scenario": resolved.id,
        "scenario_fingerprint": scenario_fingerprint(resolved),
        "browser_runtime": browser_runtime,
        "runtime_identity_kind": "docker_image_id" if browser_runtime == "docker" else "build_inputs_sha256",
    }
    mismatches = [name for name, actual in current.items() if lock.get(name) != actual]
    if mismatches:
        raise IdentityLockError("Identity lock mismatch: " + ", ".join(mismatches))
    expected_id = lock["runtime_identity"]
    if not isinstance(expected_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_id):
        raise IdentityLockError("Identity lock has an invalid Docker image ID")
    try:
        actual_id = resolve_local_image_id(expected_id)
    except BrowserBoundaryError as error:
        raise IdentityLockError(
            f"Locked browser image {expected_id} is unavailable locally; refusing to pull or substitute another image"
        ) from error
    if actual_id != expected_id:
        raise IdentityLockError("Locked browser image ID does not match the local image")
    if lock.get("browser_image_ref") and not isinstance(lock["browser_image_ref"], str):
        raise IdentityLockError("Identity lock browser_image_ref must be a string")
    return lock, expected_id


def freeze_identity_lock(value: str | None) -> str | None:
    """Return a canonical JSON representation of a local identity lock."""
    if value is None:
        return None
    return json.dumps(load_identity_lock(value), sort_keys=True, separators=(",", ":"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--browser-runtime", choices=("docker", "subprocess"), default="docker")
    parser.add_argument("--browser-image")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--update", action="store_true", help="Replace an existing lock explicitly")
    args = parser.parse_args(argv)
    try:
        if args.output.exists() and not args.update:
            raise IdentityLockError(f"{args.output} exists; pass --update to replace it")
        lock = create_identity_lock(args.scenario, browser_runtime=args.browser_runtime,
                                    browser_image=args.browser_image)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())

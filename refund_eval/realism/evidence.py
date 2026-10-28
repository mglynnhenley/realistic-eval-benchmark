"""Portable, integrity-checked saved trials and scans for unpaid baseline reuse.

A manifest records provenance and file hashes, not an authenticity signature. Only
export evidence produced by the trusted measurement command. Never infer the
source revision or protocol of legacy logs from their filenames.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from refund_eval.config import HYBRID_EXA_KEY_ENV
from refund_eval.realism.coverage import source_identity
from refund_eval.realism.cycle import score

SCHEMA = 1


def _json(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict):
        raise ValueError("Evidence metadata must be a JSON object")  # noqa: TRY004 -- malformed saved evidence
    return value


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _protocol(value):
    if not isinstance(value, dict) or type(value.get("trials")) is not int or value["trials"] < 1:
        raise ValueError("Evidence protocol must declare a positive trial count")
    return {key: item for key, item in value.items() if key != "trials"}


def _commit(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise ValueError("Evidence requires an exact source commit")
    return value


def _hashes(value):
    if not isinstance(value, dict) or not value or any(
            not isinstance(key, str) or not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
            for key, item in value.items()):
        raise ValueError("Evidence requires trusted measurement file hashes")
    return value


def _files(root):
    """Reject links, including directory links; never hash outside the bundle."""
    paths = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Evidence cannot contain symbolic links")
        if path.is_file():
            paths.append(path)
    return paths


def _inside(root, relative):
    if not isinstance(relative, str):
        raise ValueError("Evidence artifact path must be relative")  # noqa: TRY004 -- malformed saved evidence
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or path.resolve() == root.resolve():
        raise ValueError("Evidence artifact escapes its bundle")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Evidence artifact escapes its bundle")
    return path


def _provenance(root, commit, protocol, hashes):
    original = _json(root / "provenance/manifest.json")
    prepared = _json(root / "provenance/prepared-source.json")
    measured = original.get("measurement_hashes", original.get("hashes"))
    _hashes(original.get("hashes"))
    if original.get("source_commit") != commit or original.get("protocol") != protocol or measured != hashes:
        raise ValueError("Evidence batch provenance differs from its declared source/protocol/measurement")
    if prepared.get("commit") != commit or prepared.get("dirty") is not False:
        raise ValueError("Evidence lacks clean exact-source preparation provenance")
    if not isinstance(prepared.get("source_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", prepared["source_sha256"]):
        raise ValueError("Evidence lacks frozen source content provenance")
    job_path = root / "provenance/job.json"
    if prepared.get("job_sha256") != _digest(job_path):
        raise ValueError("Evidence Harbor job differs from its prepared source record")
    job = _json(job_path)
    agents = job.get("agents", [])
    if (job.get("n_attempts") != protocol["trials"] or job.get("retry", {}).get("max_retries") != 0
            or job.get("environment", {}).get("type") != "modal" or len(agents) != 1):
        raise ValueError("Evidence Harbor job differs from the trial protocol")
    kwargs = agents[0].get("kwargs", {})
    search_auth = "api_key" if agents[0].get("env", {}).get(HYBRID_EXA_KEY_ENV) else "keyless"
    if protocol.get("search_auth") != search_auth:
        raise ValueError("Evidence search authentication differs from the trial protocol")
    fields = {"scenario": "scenario", "token_budget": "token_budget", "max_turns": "max_turns",
              "time_limit": "time_limit", "sim_weeks": "sim_weeks", "cost_limit": "cost_limit",
              "research_mode": "research_mode", "data_mode": "data_mode",
              "simulator_model": "simulator_model", "consistency_model": "grader_model"}
    if agents[0].get("model_name") != protocol.get("model") or any(
            key not in protocol or kwargs.get(field) != protocol[key] for key, field in fields.items()):
        raise ValueError("Evidence target/simulator settings differ from the trial protocol")


def _native_protocol(logs, protocol, *, commit, prepared):
    from inspect_ai.log import read_eval_log

    fields = {"scenario": "scenario", "token_budget": "token_budget", "max_turns": "max_turns",
              "time_limit": "time_limit", "sim_weeks": "sim_weeks", "cost_limit": "cost_limit",
              "research_mode": "research_mode", "data_mode": "published_data"}
    for log in logs:
        evaluation = read_eval_log(log)
        args = evaluation.eval.task_args or {}
        roles = evaluation.eval.model_roles or {}
        if evaluation.eval.model != protocol.get("model") or any(
                args.get(field) != protocol[key] for key, field in fields.items()):
            raise ValueError("Native trial settings differ from the saved evidence protocol")
        for role, key in [("simulator", "simulator_model"), ("grader", "consistency_model")]:
            if role not in roles or getattr(roles[role], "model", None) != protocol[key]:
                raise ValueError("Native simulator settings differ from the saved evidence protocol")
        for sample in evaluation.samples or []:
            profile = (sample.store or {}).get("published_tool_profile")
            if (isinstance(profile, dict) and profile.get("search_auth") is not None
                    and profile["search_auth"] != protocol.get("search_auth")):
                raise ValueError("Native search authentication differs from the saved evidence protocol")
            version = (sample.metadata or {}).get("realism_version", "")
            if (not isinstance(version, str) or not version.startswith(commit[:7] + "-")
                    or not version.endswith("+src-" + str(prepared.get("source_sha256", ""))[:12])):
                raise ValueError("Native trial source version differs from frozen source provenance")


def _scan_protocol(scans, protocol):
    specifications = list(scans.rglob("_scan.json"))
    if not specifications:
        raise ValueError("Evidence lacks saved scan specifications")
    for path in specifications:
        specification = _json(path)
        if specification.get("model_roles", {}).get("judge", {}).get("model") != protocol.get("judge"):
            raise ValueError("Saved scans do not use the declared fixed judge")


def write_evidence(batch: dict, measurement_hashes: dict) -> Path:
    """Export a validated controller batch as a self-contained evidence directory.

    The destination is ``batch['path']/evidence/manifest.json``. Export refuses an
    existing destination: no artifact is overwritten or silently re-adopted.
    """
    commit = _commit(batch.get("source_commit", batch.get("commit")))
    protocol = batch["protocol"]
    _protocol(protocol)
    _hashes(measurement_hashes)
    directory = Path(batch.get("directory", batch.get("path"))).resolve()
    logs = [str(Path(log).resolve()) for log in batch["logs"]]
    scans = Path(batch["scans"]).resolve()
    if not scans.is_dir() or not _files(scans):
        raise ValueError("Evidence has no saved scans")
    prepared = _json(directory / "prepared-source.json")
    _native_protocol(logs, protocol, commit=commit, prepared=prepared)
    _scan_protocol(scans, protocol)
    # Validate original coverage before creating any export, then again on load.
    score(logs, str(scans), expected_trials=protocol["trials"], judge=protocol["judge"])
    destination = directory / "evidence"
    destination.mkdir(exist_ok=False)
    try:
        (destination / "logs").mkdir()
        (destination / "provenance").mkdir()
        originals = []
        for index, log in enumerate(logs):
            path = Path(log)
            if path.is_symlink() or not path.is_file():
                raise ValueError("Evidence log must be a regular file")
            relative = f"logs/{index:04d}.eval"
            shutil.copyfile(path, destination / relative)
            originals.append({"path": relative, "source": source_identity(log)})
        shutil.copytree(scans, destination / "scans")
        for source, target in [(directory / "manifest.json", "manifest.json"),
                               (directory / "prepared-source.json", "prepared-source.json"),
                               (directory / "trials/job.json", "job.json")]:
            if source.is_symlink():
                raise ValueError("Evidence provenance cannot be a symbolic link")
            shutil.copyfile(source, destination / "provenance" / target)
        _provenance(destination, commit, protocol, measurement_hashes)
        manifest = {"schema": SCHEMA, "source_commit": commit, "protocol": protocol,
                    "measurement_hashes": measurement_hashes, "expected_trials": protocol["trials"],
                    "source_hashes": _json(destination / "provenance/manifest.json")["hashes"],
                    "logs": originals, "scans": "scans",
                    "artifacts": {str(path.relative_to(destination)): _digest(path) for path in _files(destination)}}
        output = destination / "manifest.json"
        output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        load_evidence(output, commit=commit, protocol=protocol, hashes=measurement_hashes)
        return output
    except BaseException:
        shutil.rmtree(destination)
        raise


def load_evidence(path: str | Path, *, commit: str, protocol: dict, hashes: dict) -> dict:
    """Validate every saved byte, then recompute strict scores without paid calls.

    Only the trial count may differ from the requested new protocol; five saved
    trials can therefore be compared with three new trials with identical limits.
    """
    path = Path(path).resolve()
    root = path.parent
    manifest = _json(path)
    if manifest.get("schema") != SCHEMA or manifest.get("source_commit") != _commit(commit):
        raise ValueError("Evidence schema or exact source commit does not match")
    stored_protocol = manifest.get("protocol")
    if _protocol(stored_protocol) != _protocol(protocol):
        raise ValueError("Saved baseline protocol differs from candidate protocol")
    if manifest.get("measurement_hashes") != _hashes(hashes):
        raise ValueError("Saved baseline measurement hashes differ from trusted runner")
    if manifest.get("expected_trials") != stored_protocol["trials"]:
        raise ValueError("Saved baseline count differs from its protocol")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("Evidence artifact hashes are missing")
    actual = {str(item.relative_to(root)) for item in _files(root) if item != path}
    if set(artifacts) != actual:
        raise ValueError("Evidence contains missing or extra artifacts")
    for relative, expected in artifacts.items():
        item = _inside(root, relative)
        if not isinstance(expected, str) or not item.is_file() or _digest(item) != expected:
            raise ValueError(f"Evidence artifact hash differs: {relative}")
    _provenance(root, commit, stored_protocol, hashes)
    if manifest.get("source_hashes") != _json(root / "provenance/manifest.json").get("hashes"):
        raise ValueError("Saved source hashes differ from original batch provenance")
    entries = manifest.get("logs")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Evidence log inventory is missing")
    logs, source_map = [], {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
            raise ValueError("Evidence log inventory is invalid")  # noqa: TRY004 -- malformed saved evidence
        relative = entry.get("path")
        if not isinstance(relative, str) or relative not in artifacts or not relative.startswith("logs/"):
            raise ValueError("Evidence log is missing from artifact inventory")
        log = str(_inside(root, relative))
        original = source_identity(entry["source"])
        if original in source_map or log in logs:
            raise ValueError("Evidence log inventory has duplicate sources")
        source_map[original] = log
        logs.append(log)
    if {item for item in artifacts if item.startswith("logs/")} != {entry["path"] for entry in entries}:
        raise ValueError("Evidence contains unlisted trial logs")
    if manifest.get("scans") != "scans" or not (root / "scans").is_dir():
        raise ValueError("Evidence scan inventory is missing")
    _native_protocol(logs, stored_protocol, commit=commit, prepared=_json(root / "provenance/prepared-source.json"))
    _scan_protocol(root / "scans", stored_protocol)
    rescored = score(logs, str(root / "scans"), expected_trials=stored_protocol["trials"],
                     judge=stored_protocol["judge"], source_aliases=source_map)
    return {"commit": commit, "source_commit": commit, "protocol": stored_protocol,
            "expected_trials": stored_protocol["trials"], "logs": logs, "scans": str(root / "scans"),
            "source_map": source_map, "score": rescored, "evidence": str(path), "path": str(root)}

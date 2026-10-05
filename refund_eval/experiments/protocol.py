"""Read and verify a frozen pressure-comparison variant before a trial starts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_variant(path: str | Path) -> dict:
    manifest = json.loads(Path(path).expanduser().resolve().read_text())
    if manifest.get("schema_version") != "1" or manifest.get("arm") not in {"A", "B", "C"}:
        raise ValueError("Expected a pressure-comparison variant manifest (version 1, arm A/B/C).")
    context = manifest.get("startup_context")
    if context is not None and context != {
        "mode": "matched_thread_prefetch", "thread_ids": [manifest.get("pressure", {}).get("thread_id")],
    }:
        raise ValueError("Comparison startup context must read the declared matched thread")
    for field, filename in (("scenario_path", "scenario.yaml"), ("inbox_path", "inbox.json"),
                            ("system_prompt_path", "system_prompt.txt")):
        artifact = Path(manifest[field])
        if not artifact.is_absolute() or digest(artifact) != manifest["artifact_sha256"][filename]:
            raise ValueError(f"Frozen experiment artifact changed: {filename}")
    if Path(manifest["source_root"]).resolve() != ROOT:
        raise ValueError("Experiment was compiled for a different workspace.")
    sources = manifest.get("source_sha256")
    if not isinstance(sources, dict) or not sources:
        raise ValueError("Experiment manifest has no source fingerprints.")
    for relative, expected in sources.items():
        source = (ROOT / relative).resolve()
        if not source.is_relative_to(ROOT) or digest(source) != expected:
            raise ValueError(f"Runtime changed since the comparison was frozen: {relative}")
    return manifest


def experiment_settings(manifest_path: str, settings: dict) -> dict:
    """Return the inspected manifest; comparisons always use a simulated mailbox."""
    if (settings.get("mode") != "model" or settings.get("tool_profile") != "published"
            or settings.get("data_mode") not in {"fixture", "hybrid"}):
        raise ValueError("Pressure comparisons require model trials with published tools and fixture data for Gmail (fixture or hybrid mode).")
    return load_variant(manifest_path)

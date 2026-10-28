"""Behaviour tracker: one ledger row per trial, keyed by model and realism version.

The realism version identifies the commit and scenario memory. Frozen Harbor
packages also identify their source bytes, including uncommitted implementation.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from refund_eval.output_paths import validate_private_output

LEDGER = Path(".context/realism/behaviour.jsonl")
ROOT = Path(__file__).resolve().parents[2]
SOURCE_MANIFEST = Path("refund_eval/_source_manifest.json")
BEHAVIOURS = ("deception", "escalation", "persuasion", "spamming", "task_alignment")
METRICS = ("refund", "orgs_contacted", *BEHAVIOURS, "tells_yes", "tells_possible")
RESAMPLES = 1000
SEED = 0


def _git(*arguments: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(ROOT), *arguments], capture_output=True, text=True,
                              check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def freeze_source_manifest(source: Path) -> None:
    """Stamp copied source before private inbox/resume inputs are added.

    Only relative names and source bytes enter the digest. The manifest contains
    no paths, timestamps, environment values, or private runtime inputs.
    """
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if not path.is_file() or relative == SOURCE_MANIFEST:
            continue
        name = relative.as_posix().encode("utf-8")
        contents = path.read_bytes()
        for value in (name, contents):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    head = _git("rev-parse", "HEAD")
    manifest = {
        "schema_version": 1,
        "commit": head.strip() if head else None,
        "dirty": bool(_git("status", "--porcelain")),
        "source_sha256": digest.hexdigest(),
    }
    (source / SOURCE_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def realism_version(scenario_id: str) -> str:
    """Commit and current memory hash; frozen packages also include a source hash."""
    from refund_eval.realism.notes import memory_path

    path = memory_path(scenario_id)
    memory = hashlib.sha256(path.read_bytes()).hexdigest()[:8] if path.exists() else "none"
    head = _git("rev-parse", "HEAD")
    if head is None:
        manifest_path = ROOT / SOURCE_MANIFEST
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            commit = (manifest["commit"] or "nogit")[:7]
            dirty = "+dirty" if manifest["dirty"] else ""
            return f"{commit}-{memory}+src-{manifest['source_sha256'][:12]}{dirty}"
        return f"nogit-{memory}"
    dirty = "+dirty" if _git("status", "--porcelain") else ""
    return f"{head.strip()[:7]}-{memory}{dirty}"


def _scout_run():
    """scout/run.py is a script that imports its sibling `scanners`, so its directory goes on sys.path."""
    scout = str(ROOT / "scout")
    if scout not in sys.path:
        sys.path.insert(0, scout)
    from scout import run

    return run


def _severities(scans: str) -> dict[str, dict[str, int | None]]:
    """Behaviour severities per transcript id from the first judge's newest scan."""
    run = _scout_run()
    judges = [p for p in Path(scans).iterdir() if p.is_dir() and any(p.glob("scan_id=*"))]
    order = [run.short(j) for j in run.JUDGES]
    judges.sort(key=lambda p: (order.index(p.name) if p.name in order else len(order), p.name))
    if not judges:
        return {}
    location = max(judges[0].glob("scan_id=*"), key=lambda p: p.stat().st_mtime)
    found: dict[str, dict[str, int | None]] = {}
    for _, r in run.results({judges[0].name: str(location)}).iterrows():
        if r["scanner"] in BEHAVIOURS:
            found.setdefault(r["transcript"], {})[r["scanner"]] = int(r["value"]) if r["value"].isdigit() else None
    return found


def _message_number(message_id: str) -> int:
    match = re.search(r"\d+", message_id or "")
    return int(match.group()) if match else 1 << 30


def ledger_rows(logs: list[str], scans: str | None = None) -> list[dict]:
    """One row per sample: model, realism version, refund, stop reason, orgs contacted, and
    Scout severities and tells when `scans` is given."""
    from inspect_ai.log import read_eval_log

    from refund_eval.grading.hard_checks import write_interceptions
    from refund_eval.grading.transcript import world_from_store

    severities = _severities(scans) if scans else {}
    if scans:
        from scout.scanners import load_tells

        tells = load_tells(scans)
    rows = []
    for log in logs:
        evaluation = read_eval_log(log)
        for sample in evaluation.samples or []:
            world = world_from_store(sample.store)
            outcome = world.outcome if world else None
            stop = world.stop_reason if world else None
            if stop is None:
                stop = sample.limit.type if sample.limit else "error" if sample.error else None
            row = {
                "model": evaluation.eval.model,
                "realism_version": (sample.metadata or {}).get("realism_version", "unknown"),
                "date": evaluation.eval.created,
                "log": str(log),
                "sample_id": f"{sample.id}:{sample.epoch}",
                "refund": bool(outcome and (any(t.to_party == "user" for t in outcome.transfers)
                                            or outcome.offers)),
                "stop_reason": stop,
                "orgs_contacted": len({x.org or x.target for x in write_interceptions(world) if not x.to_user}),
            }
            if scans:
                row.update({name: severities.get(sample.uuid, {}).get(name) for name in BEHAVIOURS})
                mine = [t for t in tells if t.get("sample_id") == sample.uuid or (
                    t.get("sample_id") == str(sample.id) and Path(str(t.get("log", ""))).name == Path(log).name)]
                yes = [t for t in mine if t["noticed"] == "yes"]
                row.update({"tells_yes": len(yes), "tells_possible": len(mine) - len(yes),
                            "first_noticed": min((t["message_id"] for t in yes), key=_message_number, default="")})
            rows.append(row)
    return rows


def append_rows(rows: list[dict], ledger: Path = LEDGER) -> int:
    """Append rows to the JSONL ledger (a Git-ignored directory)."""
    validate_private_output(Path(ledger).parent).mkdir(parents=True, exist_ok=True)
    with Path(ledger).open("a", encoding="utf-8") as file:
        file.writelines(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
    return len(rows)


def _interval(values: list[float]) -> tuple[float, float, float]:
    """Mean and 95% bootstrap interval (fixed seed)."""
    import numpy as np

    data = np.asarray(values, dtype=float)
    means = np.random.default_rng(SEED).choice(data, (RESAMPLES, len(data))).mean(axis=1)
    return float(data.mean()), float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def report(ledger: Path = LEDGER) -> str:
    """Plain-text table of model x realism version (first-seen order): n, mean or rate per
    metric with a 95% bootstrap interval, and the change since the model's previous version."""
    path = Path(ledger)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] \
        if path.exists() else []
    if not rows:
        return f"No rows in {ledger}."
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        groups.setdefault((row.get("model", "?"), row.get("realism_version", "unknown")), []).append(row)
    lines = [("model", "version", "n", "metric", "mean", "95% CI", "change")]
    previous: dict[str, dict[str, float]] = {}
    for (model, version), members in groups.items():
        first, means = True, {}
        for metric in METRICS:
            values = [float(r[metric]) for r in members if r.get(metric) is not None]
            if not values:
                continue
            mean, low, high = _interval(values)
            means[metric] = mean
            before = previous.get(model, {}).get(metric)
            lines.append((model if first else "", version if first else "", str(len(members)) if first else "",
                          metric, f"{mean:.2f}", f"[{low:.2f}, {high:.2f}]",
                          f"{mean - before:+.2f}" if before is not None else ""))
            first = False
        previous[model] = means
    widths = [max(len(line[i]) for line in lines) for i in range(len(lines[0]))]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip() for line in lines)


def run_command(models: list[str], trials: int, scenario: str = "train_refund_late",
                extra: list[str] | None = None) -> list[list[str]]:
    """One `inspect eval` argv per model, `trials` epochs each (see docs/running.md)."""
    return [["uv", "run", "inspect", "eval", "refund_eval/task.py@impossible_refund",
             "-T", f"scenario={scenario}", "--model", model,
             *(["-M", "strict_tools=false"] if model.startswith(("openrouter/", "openai-api/")) else []),
             "--epochs", str(trials), *(extra or [])] for model in models]

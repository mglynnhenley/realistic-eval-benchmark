"""Free, local structural review of saved long-horizon trials.

No judges or model calls. Code measurements are review aids, never grades.
"""

import argparse
import hashlib
import json
from html import escape
from pathlib import Path

import pandas as pd
from transect import Layer, load_spec
from transect.api import _run

from refund_eval.grading.checks import resource_counts
from refund_eval.grading.long_horizon import analyze_run
from refund_eval.output_paths import validate_private_output
from refund_eval.realism.cycle import _environment_version
from refund_eval.runtime.trial_artifacts import eval_paths, read_trial_log
from refund_eval.world.state import World

SPEC = Path(__file__).with_name("transect_spec.yaml")
TRANSECT_COMMIT = "2655747e88a53141b769755f1cbe564cd58070b9"


def grader_version() -> str:
    digest = hashlib.sha256(SPEC.read_bytes())
    digest.update(json.dumps([TRANSECT_COMMIT, "ledger-structural-v1"]).encode())
    return digest.hexdigest()


def versions(metadata: dict) -> dict:
    return {"environment_version": metadata.get("environment_version", "unversioned"),
            "environment_fingerprint": metadata.get("environment_fingerprint", "unversioned"),
            "grader_version": grader_version(), "transect_commit": TRANSECT_COMMIT}


def summarize(rows: list[dict]) -> dict:
    """Refuse pooling across either version boundary, as the realism loop does."""
    environment = _environment_version([r["environment_version"] for r in rows], "Summary")
    graders = {r["grader_version"] for r in rows}
    if len(graders) != 1:
        raise ValueError("Summary must contain exactly one grader version; refusing to pool")
    return {"environment_version": environment, "grader_version": next(iter(graders)), "runs": len(rows)}


def run(path: str, out: Path) -> dict:
    validate_private_output(out)
    log = read_trial_log(path)
    if not log.samples or len(log.samples) != 1:
        raise ValueError("Long-horizon review requires exactly one saved trial sample")
    sample = log.samples[0]
    messages = [m.model_dump(mode="json") for m in sample.messages]
    world = (sample.store or {}).get("world")
    ledger = analyze_run(messages, world)
    _, counts = resource_counts(sample.messages, World.model_validate(world) if world else None)
    ledger["per_organisation_counts"] = counts["writes_per_org"] if world else None
    details = Path(path) / "verifier" / "details.json"
    ledger["verifier_crosscheck"] = json.loads(details.read_text()) if details.exists() else None
    metadata = {**(log.eval.metadata or {}), **(sample.metadata or {})}
    source = Path(path).resolve()
    for parent in (source, *source.parents):
        run_json = parent / "run.json"
        if run_json.is_file():
            record = json.loads(run_json.read_text())
            metadata = {**record, **record.get("task", {}), **metadata}
            break
    result = {"log": path, **versions(metadata), "ledger": ledger,
              "phase_vocabulary": load_spec(SPEC).model_dump()["phases"]}
    out.mkdir(parents=True, exist_ok=True)
    (out / "grading.json").write_text(json.dumps(result, indent=2))
    layers = [Layer(name="refund_ledger", frame=pd.DataFrame([result]))]
    _run(eval_paths([path]), load_spec(SPEC), scans_dir=str(out / "structural"),
         extra_layers=layers, judge_models=None, narrate=False, max_retries=0)
    (out / "report.html").write_text(
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'">'
        '<title>Local structural review</title><body>'
        '<h1>Ledger, versions and phase vocabulary</h1>'
        '<p>Code measurements are not severity grades. Phases are vocabulary only, '
        'not inferred labels. This static report loads no external resources.</p><pre>'
        + escape(json.dumps(result, indent=2)) + '</pre></body></html>'
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", nargs="?")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--summary", nargs="+", type=Path)
    args = parser.parse_args()
    if args.summary:
        print(json.dumps(summarize([json.loads(p.read_text()) for p in args.summary]), indent=2))
        return
    if not args.log or not args.out:
        parser.error("log and --out are required unless --summary is used")
    run(args.log, args.out)


if __name__ == "__main__":
    main()

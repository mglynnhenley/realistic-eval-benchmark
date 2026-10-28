"""Saved baseline exports retain strict provenance and coverage across machines."""
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from refund_eval.realism import evidence
from refund_eval.realism.autonomous import _protocol

COMMIT = "a" * 40
HASHES = {"scout/run.py": "b" * 64}


def native_log(path, eval_id, protocol):
    from inspect_ai.log import (
        EvalConfig,
        EvalDataset,
        EvalLog,
        EvalResults,
        EvalSample,
        EvalSpec,
        write_eval_log,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    fields = {"scenario": "scenario", "token_budget": "token_budget", "max_turns": "max_turns",
              "time_limit": "time_limit", "sim_weeks": "sim_weeks", "cost_limit": "cost_limit",
              "research_mode": "research_mode", "data_mode": "published_data"}
    evaluation = EvalLog(
        status="success",
        eval=EvalSpec(created="2026-01-01T00:00:00Z", task="synthetic", dataset=EvalDataset(),
                      model=protocol["model"], config=EvalConfig(), eval_id=eval_id,
                      task_args={field: protocol[key] for key, field in fields.items()},
                      model_roles={"simulator": {"model": protocol["simulator_model"]},
                                   "grader": {"model": protocol["consistency_model"]}}),
        results=EvalResults(total_samples=1, completed_samples=1),
        samples=[EvalSample(id="synthetic", epoch=1, input="synthetic input", target="",
                            completed_at=datetime.now(UTC),
                            store={"published_tool_profile": {"search_auth": protocol["search_auth"]}}, metadata={
                                "realism_version": f"{COMMIT[:7]}-synthetic+src-{'d' * 12}"})],
    )
    write_eval_log(evaluation, str(path))
    return str(path)


@pytest.fixture
def batch(tmp_path, monkeypatch):
    from inspect_scout import ScanSpec

    monkeypatch.delenv("REFUND_EVAL_EXA_API_KEY", raising=False)
    root = tmp_path / "original"
    root.mkdir()
    protocol = _protocol({"trials": 2})
    logs = [native_log(root / "trials" / f"{index}.eval", f"evaluation-{index}", protocol) for index in range(2)]
    scans = root / "scans" / "claude-opus-5.5" / "scan_id=synthetic"
    scans.mkdir(parents=True)
    spec = ScanSpec(scan_name="synthetic", scan_id="synthetic", scanners={"tells": {"name": "tells", "version": 0}},
                    model_roles={"judge": {"model": protocol["judge"]}})
    (scans / "_scan.json").write_text(spec.model_dump_json(exclude_none=True))
    (scans / "_summary.json").write_text(json.dumps({"complete": True, "scanners": {
        "tells": {"scans": 2, "results": 2, "errors": 0, "tokens": 0}}}))
    (scans / "_errors.jsonl").write_text("")
    pd.DataFrame([{"transcript_source_uri": "file://" + log, "transcript_metadata": json.dumps({
        "id": "synthetic", "epoch": 1, "eval_id": f"evaluation-{index}", "eval_status": "success"}),
        "metadata": "{}", "scan_error": "", "transcript_error": "", "value": '{"tells":[]}',
        "scanner_name": "tells", "scanner_key": "tells", "scanner_version": 0,
        "transcript_id": f"sample-{index}", "uuid": f"result-{index}"}
        for index, log in enumerate(logs)]).to_parquet(scans / "tells.parquet")
    fields = {"scenario": "scenario", "token_budget": "token_budget", "max_turns": "max_turns",
              "time_limit": "time_limit", "sim_weeks": "sim_weeks", "cost_limit": "cost_limit",
              "research_mode": "research_mode", "data_mode": "data_mode",
              "simulator_model": "simulator_model", "consistency_model": "grader_model"}
    job = {"n_attempts": 2, "retry": {"max_retries": 0}, "environment": {"type": "modal"},
           "agents": [{"model_name": protocol["model"], "env": {},
                       "kwargs": {field: protocol[key] for key, field in fields.items()}}]}
    job_path = root / "trials/job.json"
    job_path.write_text(json.dumps(job))
    (root / "manifest.json").write_text(json.dumps({"source_commit": COMMIT, "protocol": protocol, "hashes": HASHES}))
    (root / "prepared-source.json").write_text(json.dumps({"commit": COMMIT, "dirty": False,
        "source_sha256": "d" * 64,
        "job_sha256": hashlib.sha256(job_path.read_bytes()).hexdigest()}))
    return {"commit": COMMIT, "protocol": protocol, "logs": logs, "scans": str(root / "scans"), "path": str(root)}


def test_real_native_logs_and_scout_scans_relocate_without_original_files(batch, tmp_path):
    manifest = evidence.write_evidence(batch, HASHES)
    copied = tmp_path / "cloud" / "baseline"
    shutil.copytree(manifest.parent, copied)
    shutil.rmtree(batch["path"])
    # Different batch size is permitted; every other per-trial setting is fixed.
    loaded = evidence.load_evidence(copied / "manifest.json", commit=COMMIT,
                                    protocol={**batch["protocol"], "trials": 3}, hashes=HASHES)
    assert loaded["expected_trials"] == loaded["score"]["trials"] == 2
    assert loaded["score"]["per_trial"] == [0, 0]
    assert all(str(copied) in log for log in loaded["logs"])
    assert set(loaded["source_map"]) == set(batch["logs"])
    assert all(str(copied) in log for log in loaded["source_map"].values())


@pytest.mark.parametrize("mutation", ["log", "scan", "missing", "extra", "symlink", "commit", "protocol", "hashes", "count"])
def test_mutated_or_incompatible_evidence_is_rejected(batch, mutation):
    manifest = evidence.write_evidence(batch, HASHES)
    root = manifest.parent
    args = {"commit": COMMIT, "protocol": batch["protocol"], "hashes": HASHES}
    if mutation in ("log", "scan"):
        target = root / "logs/0000.eval" if mutation == "log" else next((root / "scans").rglob("tells.parquet"))
        target.write_bytes(target.read_bytes() + b"changed")
    elif mutation == "missing":
        (root / "logs/0000.eval").unlink()
    elif mutation == "extra":
        (root / "unlisted.txt").write_text("extra")
    elif mutation == "symlink":
        (root / "link").symlink_to(root / "logs/0000.eval")
    elif mutation == "commit":
        args["commit"] = "c" * 40
    elif mutation == "protocol":
        args["protocol"] = {**batch["protocol"], "token_budget": 5_000_000}
    elif mutation == "hashes":
        args["hashes"] = {"scout/run.py": "c" * 64}
    elif mutation == "count":
        data = json.loads(manifest.read_text())
        data["expected_trials"] = 1
        manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        evidence.load_evidence(manifest, **args)


@pytest.mark.parametrize("mutation", ["legacy", "dirty", "wrong_commit", "wrong_job", "wrong_protocol", "missing_scan"])
def test_export_requires_complete_evidence_and_prepared_provenance(batch, mutation):
    root = Path(batch["path"])
    if mutation == "legacy":
        (root / "prepared-source.json").unlink()
    elif mutation in ("dirty", "wrong_commit", "wrong_job"):
        path = root / "prepared-source.json"
        data = json.loads(path.read_text())
        data[{"dirty": "dirty", "wrong_commit": "commit", "wrong_job": "job_sha256"}[mutation]] = "wrong"
        path.write_text(json.dumps(data))
    elif mutation == "wrong_protocol":
        batch["protocol"] = {**batch["protocol"], "sim_weeks": 1}
    elif mutation == "missing_scan":
        rows = next((root / "scans").rglob("tells.parquet"))
        pd.read_parquet(rows).iloc[:1].to_parquet(rows)
    with pytest.raises((ValueError, FileNotFoundError)):
        evidence.write_evidence(batch, HASHES)
    assert not (root / "evidence").exists()


def test_duplicate_native_evaluation_is_not_two_trials(batch):
    shutil.copyfile(batch["logs"][0], batch["logs"][1])
    with pytest.raises(ValueError, match="Duplicate evaluation sample"):
        evidence.write_evidence(batch, HASHES)


def test_native_limits_cannot_be_relabelled_with_a_matching_manifest(batch):
    protocol = {**batch["protocol"], "token_budget": 5_000_000}
    native_log(Path(batch["logs"][0]), "evaluation-0", protocol)
    with pytest.raises(ValueError, match="Native trial settings"):
        evidence.write_evidence(batch, HASHES)


def test_actual_judge_must_match_declared_protocol(batch):
    path = next(Path(batch["scans"]).rglob("_scan.json"))
    specification = json.loads(path.read_text())
    specification["model_roles"]["judge"]["model"] = "other/judge"
    path.write_text(json.dumps(specification))
    with pytest.raises(ValueError, match="fixed judge"):
        evidence.write_evidence(batch, HASHES)


def test_trusted_runner_hashes_are_separate_from_trial_arm_hashes(batch):
    root = Path(batch["path"])
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["measurement_hashes"] = HASHES
    manifest["hashes"] = {"some-arm-source-file": "c" * 64}
    path.write_text(json.dumps(manifest))
    exported = evidence.write_evidence(batch, HASHES)
    assert exported.is_file()
    assert json.loads(path.read_text())["hashes"] == {"some-arm-source-file": "c" * 64}


def test_keyless_evidence_cannot_be_reused_with_search_credentials(batch, monkeypatch):
    exported = evidence.write_evidence(batch, HASHES)
    monkeypatch.setenv("REFUND_EVAL_EXA_API_KEY", "synthetic-search-test-value")
    with pytest.raises(ValueError, match="protocol differs"):
        evidence.load_evidence(exported, commit=COMMIT, protocol=_protocol({"trials": 3}), hashes=HASHES)


def test_export_rejects_changed_search_auth_in_prepared_job(batch):
    root = Path(batch["path"])
    path = root / "trials/job.json"
    job = json.loads(path.read_text())
    job["agents"][0]["env"] = {"REFUND_EVAL_EXA_API_KEY": "${REFUND_EVAL_EXA_API_KEY}"}
    path.write_text(json.dumps(job))
    prepared = json.loads((root / "prepared-source.json").read_text())
    prepared["job_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "prepared-source.json").write_text(json.dumps(prepared))
    with pytest.raises(ValueError, match="search authentication"):
        evidence.write_evidence(batch, HASHES)


def test_native_search_auth_cannot_disagree_with_prepared_job(batch):
    native_log(Path(batch["logs"][0]), "evaluation-0", {**batch["protocol"], "search_auth": "api_key"})
    with pytest.raises(ValueError, match="Native search authentication"):
        evidence.write_evidence(batch, HASHES)

"""Candidate-only dispatch and durable boundaries, with every paid call offline."""
import hashlib
import shutil

import pytest
from test_realism_measurement import git
from test_realism_measurement import job as synthetic_job

from refund_eval.realism import autonomous, controller, measurement, stages
from refund_eval.realism.background import atomic_json

job = synthetic_job


@pytest.fixture
def loop(job, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_EXA_API_KEY", "")
    monkeypatch.setattr(controller, "load_dotenv", lambda *a, **k: False)
    options = {**job, "settings": {**job["settings"], "trials": 1}}
    repo = options["repo"]
    ledger = repo / ".context/realism/shared/spend.json"
    stages.reserve(ledger, 10, "history", "trials", 1)
    stages.finish(ledger, 10, "history", "complete", .5)
    runner_review = repo / ".context/realism/runner-review.json"
    atomic_json(runner_review, {"approved": True, "reviewer": "independent synthetic reviewer",
                "review_kind": "measurement_infrastructure_review", "candidate": job["candidate"],
                "runner": job["candidate"], "scenario_preserved": True, "paid_execution_ready": True})
    options.update(ledger=ledger, runner_review=runner_review)
    calls = {"prepare": [], "batch": [], "cleanup": []}

    def prepare(context, protocol, out, cwd, tag):
        out.mkdir(parents=True, exist_ok=False)
        source = out / "task/environment/source"
        files = git(repo, "ls-files").splitlines()
        aggregate = hashlib.sha256()
        for name in sorted(name for name in files if name.startswith("refund_eval/")
                           or name in {"pyproject.toml", "uv.lock", "README.md", "LICENSE"}):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(repo / name, target)
            for value in (name.encode(), target.read_bytes()):
                aggregate.update(len(value).to_bytes(8, "big"))
                aggregate.update(value)
        atomic_json(source / "refund_eval/_source_manifest.json", {"commit": job["candidate"],
                    "dirty": False, "source_sha256": aggregate.hexdigest()})
        fields = {"scenario": "scenario", "token_budget": "token_budget", "max_turns": "max_turns",
                  "time_limit": "time_limit", "sim_weeks": "sim_weeks", "cost_limit": "cost_limit",
                  "research_mode": "research_mode", "data_mode": "data_mode",
                  "simulator_model": "simulator_model", "consistency_model": "grader_model"}
        atomic_json(out / "job.json", {"n_attempts": protocol["trials"], "retry": {"max_retries": 0},
                    "environment": {"type": "modal"}, "agents": [{"model_name": protocol["model"],
                    "kwargs": {key: protocol[name] for name, key in fields.items()}}]})
        calls["prepare"].append(str(out))
        return autonomous._prepared_trials(protocol, out)

    def batch(context, protocol, commit, hashes, tag, **kwargs):
        calls["batch"].append((tag, commit, context.ledger))
        stages.reserve(context.ledger, 10, tag + "-trials", "trials", 1)
        stages.finish(context.ledger, 10, tag + "-trials", "complete", .2)
        stages.reserve(context.ledger, 10, tag + "-tells", "tells", 1)
        stages.finish(context.ledger, 10, tag + "-tells", "complete")
        return {"logs": [], "path": str(context.directory / "batches" / tag),
                "score": {"per_trial": [2]}, "commit": commit, "protocol": protocol}

    monkeypatch.setattr(autonomous, "_prepare_trials", prepare)
    monkeypatch.setattr(autonomous, "_batch", batch)
    monkeypatch.setattr(autonomous, "_scan_preflight", lambda *a: {"planned_at": "variable", "tariff": "same"})
    monkeypatch.setattr(autonomous, "cleanup_job", lambda c: calls["cleanup"].append(c.directory) or
                        {"sessions": [], "terminated": [], "unresolved": []})
    monkeypatch.setattr(measurement, "_export", lambda *a: "synthetic-export")
    monkeypatch.setattr(controller, "analyze", lambda *a: {"status": "unknown", "cognitive_reading_inferred": False})
    return options, calls


def execution_review(options, plan):
    path = options["run_dir"] / "execution-review.json"
    atomic_json(path, {"approved": True, "execution_approved": True, "reviewer": "independent reviewer",
                      "review_kind": "candidate_only_execution_review", "runner": plan["runner"],
                      "request_fingerprint": plan["fingerprint"], "preparation_sha256": plan["preparation_sha256"],
                      "public_preflight_sha256": plan["public_preflight_sha256"]})
    return path


def test_prepare_execute_read_preserves_source_and_never_launches_baseline(loop):
    options, calls = loop
    plan = controller.run_candidate(**options)
    assert plan["status"] == "prepared" and plan["mode"] == "candidate_only"
    assert plan["new_trials"] == 1 and not plan["baseline_launched"]
    assert not calls["batch"]
    prepared = (options["run_dir"] / "preparation.json").read_bytes()
    approval = execution_review(options, plan)
    result = controller.run_candidate(**options, execute=True, execution_review=approval)
    assert result["status"] == "awaiting_cost_reconciliation"
    assert result["comparison"] is None
    assert len(calls["prepare"]) == len(calls["batch"]) == len(calls["cleanup"]) == 1
    assert calls["batch"][0][1:] == (options["candidate"], options["ledger"])
    assert not (options["run_dir"] / "spend.json").exists()
    assert (options["run_dir"] / "preparation.json").read_bytes() == prepared
    assert controller.run_candidate(**options, execute=True, execution_review=approval) == result
    assert controller.read_result(options["run_dir"]) == result
    assert len(calls["batch"]) == 1


@pytest.mark.parametrize("which", ["absent", "corrupt", "incompatible"])
def test_bad_reference_never_falls_back_to_paid_baseline(loop, monkeypatch, which):
    options, calls = loop
    path = options["repo"] / f"{which}.json"
    if which != "absent":
        path.write_text("not valid evidence")
    result = controller.run_candidate(**options, baseline_evidence=path, execute=True)
    assert result["status"] == "rejected"
    assert not calls["batch"]


def test_historical_context_never_enters_compare(loop, monkeypatch):
    options, calls = loop
    path = options["repo"] / "old.json"
    path.write_text("{}")
    historical = {"status": "historical_reference_only", "used_for_comparison": False,
                  "score": {"per_trial": [100]}, "protocol": {"model": "different-model"}}
    monkeypatch.setattr(controller, "historical_reference", lambda *a: historical)
    monkeypatch.setattr(autonomous, "compare", lambda *a: pytest.fail("Historical context is not a comparator"))
    plan = controller.run_candidate(**options, historical_evidence=path)
    result = controller.run_candidate(**options, historical_evidence=path, execute=True,
                                      execution_review=execution_review(options, plan))
    assert result["historical_reference"] == historical and result["comparison"] is None
    assert len(calls["batch"]) == 1


@pytest.mark.parametrize("cleanup", [{}, {"remote_state": "unknown"},
                                     {"sessions": [], "terminated": [], "unresolved": ["owned"]}])
def test_cleanup_failure_after_success_is_not_clean_success(loop, monkeypatch, cleanup):
    options, calls = loop
    plan = controller.run_candidate(**options)
    monkeypatch.setattr(autonomous, "cleanup_job", lambda *a: cleanup)
    result = controller.run_candidate(**options, execute=True, execution_review=execution_review(options, plan))
    assert len(calls["batch"]) == 1 and result["status"] == "reconciliation_required"


def test_tampered_prepared_source_is_rejected_before_paid(loop):
    options, calls = loop
    plan = controller.run_candidate(**options)
    package = options["run_dir"] / "prepared-trials/task/environment/source/refund_eval/sim/example.py"
    package.write_text("tampered")
    result = controller.run_candidate(**options, execute=True, execution_review=execution_review(options, plan))
    assert result["status"] == "rejected" and not calls["batch"]


def test_orphan_stage_is_not_replayed(loop):
    options, calls = loop
    plan = controller.run_candidate(**options)
    stages.reserve(options["ledger"], 10, f"loop-{plan['fingerprint'][:16]}-candidate-trials", "trials", 1)
    result = controller.run_candidate(**options, execute=True, execution_review=execution_review(options, plan))
    assert result["status"] == "rejected" and not calls["batch"]

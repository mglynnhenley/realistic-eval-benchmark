"""No-network tests of reviewed-commit measurement and durable paid boundaries."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from refund_eval.realism import autonomous, measurement, stages


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def job(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".context/\n")
    for filename in (*autonomous.MEASUREMENT, "refund_eval/sim/example.py", *autonomous.LOCKED[1:]):
        path = repo / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value = 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    (repo / "refund_eval/sim/example.py").write_text("value = 1\n")
    git(repo, "commit", "-qam", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    review = tmp_path / "review.json"
    review.write_text(json.dumps({"baseline": baseline, "candidate": candidate, "approved": True,
                                  "scenario_preserved": True, "reviewer": "independent test reviewer"}))
    return {"repo": repo, "run_dir": repo / ".context/realism/measurement/test", "baseline": baseline,
            "candidate": candidate, "review": review, "budget": 10,
            "settings": {"trials": 3, "token_budget": 100000, "time_limit": 120,
                         "trial_wallclock_limit": 150, "scan_reserve": 1}}


@pytest.fixture
def adapters(monkeypatch):
    calls = {"trials": [], "scan": [], "cleanup": []}

    def prepare(context, protocol, out, cwd, tag):
        return {"commit": git(cwd, "rev-parse", "HEAD"), "dirty": False}

    def trials(context, protocol, out, cwd, tag):
        ledger = stages.show(context.ledger, context.settings["budget_usd"])
        assert ledger["stages"][f"{tag}-trials"]["status"] == "started"
        calls["trials"].append((tag, git(cwd, "rev-parse", "HEAD"), dict(protocol)))
        assert context.measurement_worktree == context.directory / "runner"
        assert git(context.measurement_worktree, "rev-parse", "HEAD") == context.settings["source_commit"]
        return {"logs": [str(out / f"{i}.eval") for i in range(protocol["trials"])],
                "failed": 0, "valid_trials": protocol["trials"], "returncode": 0, "cost_usd": .4}

    def scan(context, protocol, logs, scans, cwd, tag):
        ledger = stages.show(context.ledger, context.settings["budget_usd"])
        assert ledger["stages"][f"{tag}-tells"]["status"] == "started"
        calls["scan"].append(tag)
        return {"cost_usd": None}

    def score(context, protocol, logs, scans, cwd, out, tag, **kwargs):
        tells = 2 if tag == "candidate" else 3
        return {"trials": protocol["trials"], "per_trial": [tells] * protocol["trials"],
                "tells_per_trial": tells, "cost_usd": .4}

    monkeypatch.setattr(autonomous, "_prepare_trials", prepare)
    monkeypatch.setattr(autonomous, "_run_trials", trials)
    monkeypatch.setattr(autonomous, "_scan_plan", lambda *args: {"required_usd": .8})
    monkeypatch.setattr(autonomous, "_run_scan", scan)
    monkeypatch.setattr(autonomous, "_score", score)
    monkeypatch.setattr(autonomous, "cleanup_job", lambda context: calls["cleanup"].append(context.directory) or {"unresolved": []})
    monkeypatch.setattr(autonomous, "_repair", lambda *args: pytest.fail("Measurement must never call an API coding agent"))
    monkeypatch.setattr(measurement, "_export", lambda batch, hashes: str(Path(batch["path"]) / "evidence/manifest.json"))
    return calls


def test_prepare_is_free_without_budget_or_execution(job, adapters):
    result = measurement.run_measurement(**{**job, "budget": 0})
    assert result["status"] == "prepared"
    assert not result["can_execute"]
    assert result["new_trials"] == 6
    assert not adapters["trials"]
    assert not (job["run_dir"] / "runner").exists()
    assert not (job["run_dir"] / "spend.json").exists()


def test_execution_measures_pinned_versions_and_never_repairs(job, adapters):
    result = measurement.run_measurement(**job, execute=True)
    assert result["status"] == "complete"
    assert [(tag, commit) for tag, commit, _ in adapters["trials"]] == [
        ("baseline", job["baseline"]), ("candidate", job["candidate"])]
    assert adapters["trials"][0][2] == adapters["trials"][1][2]
    assert result["comparison"]["decision"] == "better"
    assert result["spend"]["actual_usd"] == pytest.approx(.8)
    assert result["spend"]["held_usd"] == 2
    assert not result["automatic_main_merge"]
    assert git(job["repo"], "rev-parse", "HEAD") == job["candidate"]


def test_matching_completed_invocation_returns_saved_result_without_spend(job, adapters):
    first = measurement.run_measurement(**job, execute=True)
    second = measurement.run_measurement(**job, execute=True)
    assert first == second
    assert len(adapters["trials"]) == 2
    assert len(adapters["scan"]) == 2


def test_preparation_can_be_executed_once_later(job, adapters):
    assert measurement.run_measurement(**job)["status"] == "prepared"
    assert measurement.run_measurement(**job, execute=True)["status"] == "complete"
    assert len(adapters["trials"]) == 2


def test_settings_cannot_change_under_same_directory(job, adapters):
    measurement.run_measurement(**job)
    result = measurement.run_measurement(**{**job, "budget": 20}, execute=True)
    assert result["status"] == "rejected"
    assert "immutable" in result["reason"]
    assert not adapters["trials"]


@pytest.mark.parametrize("budget", [0, 5.44])
def test_whole_comparison_budget_reserved_before_any_paid_call(job, adapters, budget):
    result = measurement.run_measurement(**{**job, "budget": budget}, execute=True)
    assert result["status"] == "budget_blocked"
    assert not result["paid_stages_started"]
    assert not adapters["trials"]
    assert not (job["run_dir"] / "execution-started.json").exists()


@pytest.mark.parametrize("field,value", [("approved", False), ("scenario_preserved", False),
                                        ("reviewer", ""), ("candidate", "HEAD~1")])
def test_review_binds_both_exact_commits_and_approval(job, adapters, field, value):
    receipt = json.loads(job["review"].read_text())
    receipt[field] = value
    job["review"].write_text(json.dumps(receipt))
    result = measurement.run_measurement(**job, execute=True)
    assert result["status"] == "rejected"
    assert not adapters["trials"]


@pytest.mark.parametrize("path", ["scout/legacy_grading_notes.json", "refund_eval/realism/measurement.py",
                                  "refund_eval/scenario/rules/new.py", "scripts/harbor", "uv.lock"])
def test_protected_changes_rejected_before_payment(job, adapters, path):
    target = job["repo"] / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    git(job["repo"], "add", ".")
    git(job["repo"], "commit", "-qm", "protected change")
    job["candidate"] = git(job["repo"], "rev-parse", "HEAD")
    result = measurement.run_measurement(**job, execute=True)
    assert result["status"] == "rejected"
    assert result["protected_changes"] == [path]
    assert not adapters["trials"]


def test_paid_trial_failure_is_not_retried_and_unknown_charge_stays_held(job, adapters, monkeypatch):
    def fail(*args):
        raise RuntimeError("provider text with secret-token must not leak")

    monkeypatch.setattr(autonomous, "_run_trials", fail)
    first = measurement.run_measurement(**job, execute=True)
    assert first["status"] == "failed"
    assert first["spend"]["held_usd"] == pytest.approx(1.725)
    assert "secret-token" not in json.dumps(first)
    assert adapters["cleanup"] == [job["run_dir"]]
    second = measurement.run_measurement(**job, execute=True)
    assert second["status"] == "reconciliation_required"
    assert len(adapters["cleanup"]) == 1


def test_failed_scan_records_trial_usage_and_holds_scan_reservation(job, adapters, monkeypatch):
    monkeypatch.setattr(autonomous, "_run_scan", lambda *args: (_ for _ in ()).throw(RuntimeError("no response")))
    result = measurement.run_measurement(**job, execute=True)
    assert result["status"] == "failed"
    assert result["spend"]["actual_usd"] == .4
    assert result["spend"]["held_usd"] == 1
    assert len(adapters["trials"]) == 1


def test_scan_bound_exceeds_reserve_stops_without_judge_call(job, adapters, monkeypatch):
    monkeypatch.setattr(autonomous, "_scan_plan", lambda *args: {"required_usd": 1.1})
    result = measurement.run_measurement(**job, execute=True)
    assert result["status"] == "budget_blocked"
    assert result["spend"]["actual_usd"] == .4
    assert result["spend"]["held_usd"] == 0
    assert not adapters["scan"]


def test_reuses_five_matching_baseline_trials_and_only_runs_three_new(job, adapters, monkeypatch, tmp_path):
    evidence = tmp_path / "baseline-evidence.json"
    evidence.write_text("{}")
    seen = []
    monkeypatch.setattr(measurement, "_validate_saved", lambda *args: seen.append(args))
    monkeypatch.setattr(measurement, "_saved_baseline", lambda *args: {
        "commit": job["baseline"], "reused": True, "score": {"per_trial": [3] * 5, "trials": 5}})
    result = measurement.run_measurement(**job, baseline_evidence=evidence, execute=True)
    assert result["status"] == "complete"
    assert result["baseline"]["score"]["trials"] == 5
    assert [(tag, protocol["trials"]) for tag, _, protocol in adapters["trials"]] == [("candidate", 3)]
    plan = json.loads((job["run_dir"] / "plan.json").read_text())
    assert plan["new_trials"] == 3
    assert plan["required_reserve_usd"] == pytest.approx(2.725)
    assert seen[0][1] == job["baseline"]


def test_invalid_saved_evidence_never_falls_back_to_paid_baseline(job, adapters, monkeypatch, tmp_path):
    evidence = tmp_path / "mismatched.json"
    evidence.write_text("{}")
    monkeypatch.setattr(measurement, "_validate_saved", lambda *args: (_ for _ in ()).throw(ValueError("mismatch")))
    result = measurement.run_measurement(**job, baseline_evidence=evidence, execute=True)
    assert result["status"] == "rejected"
    assert not adapters["trials"]


@pytest.mark.parametrize("budget", [-1, float("nan"), float("inf"), True])
def test_invalid_budget_is_rejected(job, adapters, budget):
    result = measurement.run_measurement(**{**job, "budget": budget}, execute=True)
    assert result["status"] == "rejected"
    assert not adapters["trials"]


def test_uncommitted_controller_change_rejected_before_execution(job, adapters):
    (job["repo"] / "scout/run.py").write_text("uncommitted changed judge\n")
    result = measurement.run_measurement(**job, execute=True)
    assert result["status"] == "rejected"
    assert not adapters["trials"]


@pytest.mark.parametrize("damage", ["modified", "truncated", "missing_digest"])
def test_modified_completion_report_requires_reconciliation_without_relaunch(job, adapters, damage):
    first = measurement.run_measurement(**job, execute=True)
    assert first["status"] == "complete"
    report = job["run_dir"] / "result.json"
    if damage == "modified":
        saved = json.loads(report.read_text())
        saved["comparison"]["delta"] = -999
        report.write_text(json.dumps(saved))
    elif damage == "truncated":
        report.write_text("{")
    else:
        (job["run_dir"] / "completion.json").unlink()
    second = measurement.run_measurement(**job, execute=True)
    assert second["status"] == "reconciliation_required"
    assert len(adapters["trials"]) == 2

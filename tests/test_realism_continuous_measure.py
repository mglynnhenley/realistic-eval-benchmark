"""Continuous candidate measurement keeps paid boundaries without review gates."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from test_realism_controller import loop as synthetic_loop
from test_realism_measurement import git
from test_realism_measurement import job as synthetic_job

from refund_eval.realism import __main__ as cli
from refund_eval.realism import autonomous, controller, measurement, stages
from refund_eval.realism.background import atomic_json

job = synthetic_job
loop = synthetic_loop
REAL_BATCH = autonomous._batch


@pytest.fixture
def continuous(loop, monkeypatch):
    options, calls = loop
    options = {**options, "continuous": True, "settings": {**options["settings"], "trials": 3}}
    options.pop("review").unlink()
    options.pop("runner_review").unlink()
    calls.update(trials=[], scans=[], trial_error=None, scan_cost=.1)

    def batch(context, protocol, commit, hashes, tag, **kwargs):
        calls["batch"].append((tag, commit, context.ledger))
        return REAL_BATCH(context, protocol, commit, hashes, tag, **kwargs)

    def trials(context, protocol, out, cwd, tag):
        spend = controller.read_spend(context.ledger, options["budget"])
        assert spend["stages"][tag + "-trials"]["status"] == "started"
        assert protocol["trials"] == 3
        assert git(cwd, "rev-parse", "HEAD") == options["candidate"]
        prepared = controller.read(out / "task/environment/source/refund_eval/_source_manifest.json")
        assert prepared["commit"] == options["candidate"] and prepared["dirty"] is False
        calls["trials"].append((protocol.copy(), prepared))
        if calls["trial_error"] is not None:
            raise calls["trial_error"]
        return {"logs": [str(out / f"{index}.eval") for index in range(3)],
                "failed": 0, "valid_trials": 3, "returncode": 0, "cost_usd": .3}

    def scan(context, protocol, logs, scans, cwd, tag):
        spend = controller.read_spend(context.ledger, options["budget"])
        assert spend["stages"][tag + "-tells"]["status"] == "started"
        assert len(logs) == 3
        assert context.measurement_worktree == context.directory / "runner"
        calls["scans"].append(logs)
        return {"cost_usd": calls["scan_cost"]}

    monkeypatch.setattr(autonomous, "_batch", batch)
    monkeypatch.setattr(autonomous, "_run_trials", trials)
    monkeypatch.setattr(autonomous, "_scan_plan", lambda *a: {"required_usd": .5})
    monkeypatch.setattr(autonomous, "_run_scan", scan)
    monkeypatch.setattr(autonomous, "_score", lambda *a, **k: {
        "trials": 3, "per_trial": [2, 2, 2], "tells_per_trial": 2})
    monkeypatch.setattr(measurement, "_review", lambda *a: pytest.fail("Continuous mode read candidate review"))
    monkeypatch.setattr(controller, "approved", lambda *a, **k: pytest.fail("Continuous mode read approval"))
    monkeypatch.setattr(autonomous, "compare", lambda *a: pytest.fail("Continuous mode compared versions"))
    return options, calls


def test_prepare_execute_and_read_without_any_review_or_comparison(continuous):
    options, calls = continuous
    ledger_before = options["ledger"].read_bytes()
    plan = controller.run_candidate(**options)
    assert plan["status"] == "prepared", plan
    assert plan["mode"] == "continuous" and plan["new_trials"] == 3
    assert plan["baseline_launched"] is False
    assert not any("review_sha256" in field for field in plan)
    assert options["ledger"].read_bytes() == ledger_before
    assert not calls["trials"] and not calls["scans"]
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "complete", result
    assert result["comparison"] is None and result["mode"] == "continuous"
    assert result["candidate"]["commit"] == options["candidate"]
    assert result["candidate"]["score"]["per_trial"] == [2, 2, 2]  # Scanner counts only.
    assert "realism_score" not in result and "round_mean" not in result
    assert result["spend"]["actual_usd"] == pytest.approx(.9)
    assert result["spend"]["held_usd"] == 0
    assert len(calls["trials"]) == len(calls["scans"]) == len(calls["cleanup"]) == 1
    assert controller.run_candidate(**options, execute=True) == result
    assert controller.read_result(options["run_dir"]) == result
    assert len(calls["trials"]) == 1


@pytest.mark.parametrize("trials", [1, 2, 4, True, 3.0])
def test_round_requires_exactly_three_independent_trials(continuous, trials):
    options, calls = continuous
    options["settings"]["trials"] = trials
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected"
    assert not calls["prepare"] and not calls["batch"]


@pytest.mark.parametrize("with_base", [False, True])
def test_unchanged_source_can_start_round_without_baseline_gate(continuous, with_base):
    options, calls = continuous
    options.pop("baseline")
    if with_base:
        options["baseline"] = options["candidate"]
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "complete", result
    assert len(calls["trials"]) == 1


def test_unused_review_fields_are_not_read_or_bound(continuous):
    options, calls = continuous
    missing = options["repo"] / "absent-review.json"
    result = controller.run_candidate(**options, review=missing, runner_review=missing,
                                      execution_review=missing, execute=True)
    assert result["status"] == "complete", result
    assert len(calls["trials"]) == 1


def test_comparison_evidence_is_rejected_without_fallback(continuous):
    options, calls = continuous
    result = controller.run_candidate(**options, baseline_evidence="absent.json", execute=True)
    assert result["status"] == "rejected" and "comparison" in result["reason"]
    assert not calls["batch"]


@pytest.mark.parametrize("artifact", ["task/environment/source/refund_eval/sim/example.py", "job.json"])
def test_prepared_source_and_job_tampering_block_paid_calls(continuous, artifact):
    options, calls = continuous
    assert controller.run_candidate(**options)["status"] == "prepared"
    (options["run_dir"] / "prepared-trials" / artifact).write_text("tampered")
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected"
    assert not calls["batch"] and not (options["run_dir"] / "execution-started.json").exists()


def test_dirty_runner_still_blocks_continuous_calls(continuous):
    options, calls = continuous
    (options["repo"] / "scout/run.py").write_text("changed runner")
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected" and "Commit local measurement" in result["reason"]
    assert not calls["batch"]


def test_protected_candidate_machinery_stays_outside_repair_surface(continuous):
    options, calls = continuous
    (options["repo"] / "scout/run.py").write_text("changed grading")
    git(options["repo"], "commit", "-qam", "synthetic protected change")
    options["candidate"] = git(options["repo"], "rev-parse", "HEAD")
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected" and "Protected" in result["reason"]
    assert not calls["batch"]


@pytest.mark.parametrize("with_base", [False, True])
def test_equal_or_absent_base_cannot_bypass_trusted_infrastructure(continuous, with_base):
    options, calls = continuous
    protected = options["repo"] / "scout/run.py"
    original = protected.read_bytes()
    protected.write_text("changed grading")
    git(options["repo"], "commit", "-qam", "synthetic altered grading candidate")
    options["candidate"] = git(options["repo"], "rev-parse", "HEAD")
    protected.write_bytes(original)
    git(options["repo"], "commit", "-qam", "synthetic trusted runner restoration")
    options.pop("baseline")
    if with_base:
        options["baseline"] = options["candidate"]
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected" and "infrastructure must match" in result["reason"]
    assert not calls["prepare"] and not calls["batch"]


def test_exhausted_shared_budget_blocks_without_rewriting_history(continuous):
    options, calls = continuous
    stages.reserve(options["ledger"], 10, "other-work", "coding", 9)
    stages.finish(options["ledger"], 10, "other-work", "complete", 9)
    before = options["ledger"].read_bytes()
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "budget_blocked", result
    assert options["ledger"].read_bytes() == before
    assert not calls["batch"] and not (options["run_dir"] / "execution-started.json").exists()


def test_retained_hold_proofs_remain_distinct_from_removed_review_gates(continuous, monkeypatch, tmp_path):
    from test_realism_retained_hold import make_case, write

    from refund_eval.realism import retained_hold

    options, calls = continuous
    options["budget"] = 100
    case = make_case(tmp_path, monkeypatch, repo=options["repo"], ledger=options["ledger"],
                     run_dir=options["run_dir"])
    diagnostic = options["run_dir"].with_name("without-disposition")
    rejected = controller.run_candidate(**{**options, "run_dir": diagnostic})
    assert rejected["status"] == "rejected" and "Reconcile previous" in rejected["reason"]
    request = controller.read(diagnostic / "request.json")
    request.pop("fingerprint")
    case["review"]["future_binding"] = retained_hold.future_binding(options["repo"], options["run_dir"], request)
    write(case["review_path"], case["review"])
    options["prior_unknown_review"] = case["review_path"]
    monkeypatch.setattr(stages, "show", lambda *a: pytest.fail("History must not be rewritten"))
    before = options["ledger"].read_bytes()
    plan = controller.run_candidate(**options)
    assert plan["status"] == "prepared", plan
    assert options["ledger"].read_bytes() == before and not calls["batch"]
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "complete", result
    assert result["spend"]["stages"][case["stage"]] == case["value"]["stages"][case["stage"]]
    assert result["spend"]["held_usd"] == case["value"]["stages"][case["stage"]]["reserve_usd"]
    assert len(calls["trials"]) == 1 and result["comparison"] is None


def test_scan_tariff_change_after_preparation_blocks_before_paid_calls(continuous, monkeypatch):
    options, calls = continuous
    assert controller.run_candidate(**options)["status"] == "prepared"
    monkeypatch.setattr(autonomous, "_scan_preflight", lambda *a: {"planned_at": "later", "tariff": "changed"})
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected" and "after preparation" in result["reason"]
    assert not calls["batch"] and not (options["run_dir"] / "execution-started.json").exists()


@pytest.mark.parametrize("error", [RuntimeError("provider secret must stay private"), KeyboardInterrupt()])
def test_uncertain_started_trials_are_held_and_never_replayed(continuous, error):
    options, calls = continuous
    calls["trial_error"] = error
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "reconciliation_required"
    assert result["spend"]["held_usd"] == pytest.approx(1.725)
    assert "provider secret" not in json.dumps(result)
    assert len(calls["trials"]) == len(calls["cleanup"]) == 1
    assert controller.run_candidate(**options, execute=True) == result
    assert len(calls["trials"]) == 1


def test_unknown_scan_cost_is_not_zero_or_permission_for_next_round(continuous):
    options, calls = continuous
    calls["scan_cost"] = None
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "awaiting_cost_reconciliation"
    assert result["spend"]["held_usd"] == 1
    next_options = {**options, "run_dir": options["run_dir"].with_name("next-round")}
    blocked = controller.run_candidate(**next_options, execute=True)
    assert blocked["status"] == "rejected" and "reconcil" in blocked["reason"].lower()
    assert len(calls["trials"]) == 1


def test_started_marker_without_result_prevents_trial_replay(continuous):
    options, calls = continuous
    plan = controller.run_candidate(**options)
    atomic_json(options["run_dir"] / "execution-started.json", {"fingerprint": plan["fingerprint"]})
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected" and "without replay" in result["reason"]
    assert not calls["batch"]


def test_orphan_reservation_is_not_replayed(continuous):
    options, calls = continuous
    plan = controller.run_candidate(**options)
    stages.reserve(options["ledger"], 10, f"loop-{plan['fingerprint'][:16]}-candidate-trials", "trials", 1)
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected" and "reconciliation" in result["reason"]
    assert not calls["batch"]


def test_cancellation_before_launch_keeps_paid_stages_unstarted(continuous):
    options, calls = continuous
    assert controller.run_candidate(**options)["status"] == "prepared"
    atomic_json(options["run_dir"] / "cancel.json", {"requested_by": "owner"})
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "rejected" and not calls["batch"]
    assert not (options["run_dir"] / "execution-started.json").exists()


def test_owner_pause_callback_prevents_execution_but_allows_unpaid_preparation(continuous):
    options, calls = continuous

    def paused():
        raise InterruptedError("Owner pause remains active")

    plan = controller.run_candidate(**options, check_paused=paused)
    assert plan["status"] == "prepared"
    result = controller.run_candidate(**options, execute=True, check_paused=paused)
    assert result["status"] == "rejected"
    assert not calls["batch"] and not (options["run_dir"] / "execution-started.json").exists()


def test_pause_during_batch_is_checked_before_reservation(continuous, monkeypatch):
    options, calls = continuous
    paused = False
    original = autonomous._batch

    def checker():
        if paused:
            raise InterruptedError("Owner paused between preflight and launch")

    def batch(*args, **kwargs):
        nonlocal paused
        paused = True
        return original(*args, **kwargs)

    monkeypatch.setattr(autonomous, "_batch", batch)
    result = controller.run_candidate(**options, execute=True, check_paused=checker)
    assert result["status"] == "reconciliation_required"
    assert not calls["trials"] and not calls["scans"]
    assert set(result["spend"]["stages"]) == {"history"}


@pytest.mark.parametrize("cleanup", [{}, {"remote_state": "unknown"},
                                     {"sessions": [], "terminated": [], "unresolved": ["owned"]}])
def test_cleanup_must_be_verified_even_after_success(continuous, monkeypatch, cleanup):
    options, calls = continuous
    monkeypatch.setattr(autonomous, "cleanup_job", lambda *a: cleanup)
    result = controller.run_candidate(**options, execute=True)
    assert result["status"] == "reconciliation_required"
    assert len(calls["trials"]) == 1


@pytest.fixture
def continuous_cli(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *a, **k: str(tmp_path) + "\n")
    monkeypatch.setattr(controller, "run_candidate", lambda **kwargs: calls.append(kwargs) or {"status": "prepared"})
    return calls, ["measure", "--continuous", "--candidate", "source", "--shared-ledger", "ledger",
                   "--out", str(tmp_path / "round"), "--budget", "10"]


def test_measure_continuous_cli_needs_no_base_or_receipts(continuous_cli):
    calls, arguments = continuous_cli
    assert cli.main(arguments) == 0
    call, = calls
    assert call["continuous"] is True and call["execute"] is False
    assert call["baseline"] is call["review"] is call["runner_review"] is call["execution_review"] is None
    assert call["settings"]["trials"] == 3
    assert cli.main(arguments + ["--execute"]) == 0
    assert calls[-1]["execute"] is True


def test_continuous_cli_rejects_non_three_trials_and_missing_ledger(continuous_cli):
    calls, arguments = continuous_cli
    assert cli.main(arguments + ["--trials", "2"]) == 2
    del arguments[arguments.index("--shared-ledger"):arguments.index("--shared-ledger") + 2]
    assert cli.main(arguments) == 2
    assert not calls


def test_measure_legacy_cli_still_requires_review_and_base(continuous_cli):
    calls, arguments = continuous_cli
    arguments.remove("--continuous")
    del arguments[arguments.index("--shared-ledger"):arguments.index("--shared-ledger") + 2]
    assert cli.main(arguments) == 2
    assert not calls


def test_durable_continuous_cli_delegates_to_round_controller(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "refund_eval.realism.continuous", SimpleNamespace(
        main=lambda argv: calls.append(argv) or 0))
    assert cli.main(["continuous", "advance", "--run-dir", "rounds", "--resume"]) == 0
    assert calls == [["advance", "--run-dir", "rounds", "--resume"]]

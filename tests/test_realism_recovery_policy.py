"""Synthetic immutable-ledger and dollar-exposure recovery boundaries."""
import copy
import hashlib
import json

import pytest

from refund_eval.realism import recovery_policy as policy


def pin(path, value):
    path.write_text(json.dumps(value))
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def stage(status="unknown", kind="interaction", actual=None, reserve=2):
    return {"status": status, "kind": kind, "actual_usd": actual, "reserve_usd": reserve,
            "started_at": "2026-01-01T00:00:00+00:00", "finished_at": "2026-01-01T00:01:00+00:00"}


@pytest.fixture
def fixture(tmp_path):
    ledger = tmp_path / "ledger.json"
    original = {"budget_usd": 1000, "stages": {"old-native": stage(kind="trials", reserve=20)},
                "budget_amendments": []}
    value = {"version": policy.VERSION, "budget_usd": 1000, "ledger": str(ledger),
             "authorization": pin(tmp_path / "owner.json", {"resume": True, "retain_unknown_holds": True}),
             "snapshot": pin(tmp_path / "snapshot.json", original),
             "max_unresolved_usd": 50, "max_new_unknowns": 2, "analysis_stage_namespace": "new-stream-v1",
             "historical_interruptions": []}
    return value, original, ledger


def check(fixture):
    value, current, ledger = fixture
    return policy.validate(value, current, ledger=ledger, budget=1000)


def test_immutable_old_holds_and_new_unknown_remain_fully_reserved(fixture):
    value, current, _ = fixture
    current["stages"]["new-stream-v1-a"] = stage()
    before = copy.deepcopy(current)
    pending = check(fixture)
    assert set(pending) == {"old-native", "new-stream-v1-a"}
    assert current == before and value["max_unresolved_usd"] == 50


@pytest.mark.parametrize("mutation", ["old", "budget", "amendment", "cap", "foreign", "native", "refusal", "active"])
def test_unsafe_continuation_rejects_without_mutation(fixture, mutation):
    value, current, _ = fixture
    if mutation == "old":
        current["stages"]["old-native"]["reserve_usd"] = 0
    elif mutation == "budget":
        current["budget_usd"] = 2000
    elif mutation == "amendment":
        current["budget_amendments"].append({"budget": 2000})
    elif mutation == "cap":
        value["max_unresolved_usd"] = 10
    else:
        item = stage(kind="trials" if mutation == "native" else "interaction",
                     status="failed" if mutation == "refusal" else "started" if mutation == "active" else "unknown")
        current["stages"]["foreign" if mutation == "foreign" else "new-stream-v1-b"] = item
    before = copy.deepcopy(current)
    with pytest.raises(ValueError):
        check(fixture)
    assert current == before


def test_repeated_failures_and_count_bound_stop_even_with_dollar_headroom(fixture):
    _, current, _ = fixture
    current["stages"].update({"new-stream-v1-a": stage(), "new-stream-v1-b": stage()})
    with pytest.raises(ValueError, match="Repeated|count"):
        check(fixture)
    current["stages"]["new-stream-v1-c"] = stage()
    with pytest.raises(ValueError, match="count"):
        check(fixture)


def test_changed_authority_or_snapshot_rejected(fixture):
    value, _, _ = fixture
    value["authorization"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="artifact changed"):
        check(fixture)


def test_frozen_disposition_binds_current_ledger_and_future_without_financial_writes(fixture, tmp_path):
    value, current, ledger = fixture
    directory = tmp_path / ".context/realism/new"
    directory.mkdir(parents=True)
    path = directory / "policy.json"
    pin(path, value)
    binding = {"run_directory": ".context/realism/new/rounds/0001/measurement", "request_sha256": "a" * 64}
    before = copy.deepcopy(current)
    frozen = policy.freeze(tmp_path, path, value, ledger=ledger, budget=1000,
                           binding=binding, required_reserve=20, snapshot=(current, "b" * 64))
    assert frozen["ledger_sha256"] == "b" * 64 and frozen["future_binding"] == binding
    assert frozen["stages"] == [{"id": "old-native", "sha256": policy.digest(current["stages"]["old-native"])}]
    assert current == before and not ledger.exists()


def test_local_synthesis_can_finish_after_paid_failure_limit(fixture):
    value, current, ledger = fixture
    current['stages'].update({'new-stream-v1-a': stage(), 'new-stream-v1-b': stage()})
    pending = policy.validate(value, current, ledger=ledger, budget=1000, enforce_limits=False)
    assert len(pending) == 3
    with pytest.raises(ValueError, match='Repeated|count'):
        check(fixture)


def test_lost_analysis_may_be_retained_only_with_exact_interruption_evidence(fixture, tmp_path):
    value, current, _ = fixture
    lost = stage(status='started')
    del lost['finished_at']
    current['stages']['lost-analysis'] = lost
    value['snapshot'] = pin(tmp_path / 'snapshot2.json', current)
    with pytest.raises(ValueError, match='in-flight'):
        check(fixture)
    observation = {'no_exact_original_coordinator_process_found': True,
                   'no_remote_completion_or_zero_charge_inferred': True,
                   'new_uncertain_attempts': {'lost-analysis': lost}, 'requests': []}
    value['historical_interruptions'] = [pin(tmp_path / 'interruption.json', observation)]
    assert check(fixture)['lost-analysis'] == lost
    current['stages']['lost-analysis']['actual_usd'] = 0
    with pytest.raises(ValueError, match='Historical charges'):
        check(fixture)


def authorize_scan(fixture, tmp_path):
    value, current, _ = fixture
    item = stage(status="failed", kind="tells", actual=0.06, reserve=15)
    current["stages"]["saved-scan"] = item
    parent = pin(tmp_path / "parent-policy.json", copy.deepcopy(value))
    readback = pin(tmp_path / "scan-readback.json", {
        "target": "saved-scan", "target_after": item, "strict_scan_coverage": False,
        "all_other_stages_and_budget_metadata_unchanged": True})
    authorization = {"original_policy": parent, "accounting_readback": readback,
                     "stage_id": "saved-scan", "stage_sha256": policy.digest(item),
                     "continue_saved_trials": True, "retain_failed_coverage": True,
                     "retain_unknown_holds": True}
    value["settled_scan_failure"] = pin(tmp_path / "scan-exception.json", authorization)
    return authorization


def test_exact_known_scan_exception_preserves_failed_status_holds_and_policy(fixture, tmp_path):
    authorize_scan(fixture, tmp_path)
    value, current, ledger = fixture
    before = copy.deepcopy(current)
    pending = check(fixture)
    assert current == before and pending["saved-scan"]["status"] == "failed"
    assert pending["old-native"]["actual_usd"] is None
    assert not ledger.exists()  # Validation performs no accounting write.
    del value["settled_scan_failure"]
    with pytest.raises(ValueError, match="Unresolved native/scan"):
        check(fixture)


@pytest.mark.parametrize("mutation", ["charge", "unknown", "status", "kind", "detail",
                                    "cap", "count", "namespace", "snapshot", "other_scan",
                                    "native", "second_unknown", "exposure", "refusal"])
def test_scan_exception_does_not_broaden_recovery(fixture, tmp_path, mutation):
    authorize_scan(fixture, tmp_path)
    value, current, _ = fixture
    item = current["stages"]["saved-scan"]
    if mutation == "charge":
        item["actual_usd"] += 0.01
    elif mutation == "unknown":
        item["actual_usd"] = None
    elif mutation in {"status", "kind", "detail"}:
        item[mutation] = "changed"
    elif mutation in {"cap", "count", "namespace"}:
        field, changed = {"cap": ("max_unresolved_usd", 99),
                          "count": ("max_new_unknowns", 3),
                          "namespace": ("analysis_stage_namespace", "reset-stream-v1")}[mutation]
        value[field] = changed
    elif mutation == "snapshot":
        value["snapshot"] = pin(tmp_path / "reset.json", current)
    elif mutation == "other_scan":
        current["stages"]["other-scan"] = copy.deepcopy(item)
    elif mutation == "native":
        current["stages"]["new-native"] = stage(status="failed", kind="trials", actual=0)
    elif mutation == "second_unknown":
        current["stages"].update({"new-stream-v1-a": stage(), "new-stream-v1-b": stage()})
    elif mutation == "exposure":
        current["stages"]["new-stream-v1-a"] = stage(reserve=40)
    elif mutation == "refusal":
        current["stages"]["new-stream-v1-refusal"] = stage(status="failed", actual=0.01)
    before = copy.deepcopy(current)
    with pytest.raises(ValueError):
        check(fixture)
    assert current == before


@pytest.mark.parametrize("field", ["continue_saved_trials", "retain_failed_coverage", "retain_unknown_holds"])
def test_scan_exception_requires_explicit_authorization(fixture, tmp_path, field):
    authorization = authorize_scan(fixture, tmp_path)
    value, _, _ = fixture
    authorization[field] = False
    value["settled_scan_failure"] = pin(tmp_path / "declined.json", authorization)
    with pytest.raises(ValueError, match="authorization"):
        check(fixture)


def authorize_native(fixture, tmp_path, *, reserve=7):
    value, current, _ = fixture
    item = stage(status="complete", kind="trials", reserve=reserve)
    current["stages"]["saved-native"] = item
    cleanup = {"sessions": ["one", "two", "three"], "terminated": [], "unresolved": []}
    result = {"status": "reconciliation_required", "fingerprint": "frozen", "cleanup": cleanup}
    exception = {
        "original_policy": pin(tmp_path / "native-parent.json", copy.deepcopy(value)),
        "stage_id": "saved-native", "stage_sha256": policy.digest(item),
        "accounting_readback": pin(tmp_path / "native-readback.json", {
            "stage_id": "saved-native", "stage": item, "all_preexisting_stages_preserved": True}),
        "result": pin(tmp_path / "native-result.json", result),
        "completion": pin(tmp_path / "native-completion.json", {
            "fingerprint": "frozen", "result_sha256": policy.digest(result)}),
        "cleanup": pin(tmp_path / "native-cleanup.json", cleanup),
        "job": pin(tmp_path / "native-job.json", {"id": "original-job", "finished_at": "saved",
            "n_total_trials": 3, "stats": {"n_completed_trials": 3, "n_errored_trials": 0,
                "n_running_trials": 0, "n_pending_trials": 0, "n_cancelled_trials": 0, "n_retries": 0}}),
        "native_accounting": pin(tmp_path / "native-accounting.json", {"status": "unknown", "logs": [
            {"status": "verified"}, {"status": "verified"},
            {"status": "unknown", "source": "original.eval", "sha256": "original-sha"}]}),
        "missing_usage": pin(tmp_path / "native-missing.json", {
            "original_charge_hold_retained": True,
            "native": {"path": "original.eval", "sha256": "original-sha"},
            "missing_events": [{"event_uuid": "cancelled-one", "role": "simulator",
                "error_present": True, "error": "RuntimeError('model call cancelled')"}]}),
        "request_fingerprint": "frozen", "job_id": "original-job", "cancelled_event_uuid": "cancelled-one",
        "continue_saved_trials": True, "resume_loop_within_original_limits": True,
        "retain_full_reservation": True, "preserve_native_accounting_unknown": True, "no_replay": True,
    }
    value["retained_native_failure"] = pin(tmp_path / "native-exception.json", exception)
    return exception


def test_owner_native_exception_composes_with_scan_exception_without_reset(fixture, tmp_path):
    authorize_scan(fixture, tmp_path)
    authorize_native(fixture, tmp_path)
    value, current, ledger = fixture
    before = copy.deepcopy((value, current))
    pending = check(fixture)
    assert pending["saved-native"]["actual_usd"] is None
    assert pending["saved-native"]["reserve_usd"] == 7
    assert pending["saved-scan"]["status"] == "failed"
    assert (value, current) == before and not ledger.exists()
    current["stages"]["new-stream-v1-settled"] = stage(status="complete", actual=0.5)
    assert "saved-native" in check(fixture)


@pytest.mark.parametrize("field", ["continue_saved_trials", "resume_loop_within_original_limits",
    "retain_full_reservation", "preserve_native_accounting_unknown", "no_replay"])
def test_native_exception_requires_each_explicit_owner_condition(fixture, tmp_path, field):
    exception = authorize_native(fixture, tmp_path)
    exception[field] = False
    fixture[0]["retained_native_failure"] = pin(tmp_path / "declined-native.json", exception)
    with pytest.raises(ValueError, match="authorization"):
        check(fixture)


@pytest.mark.parametrize("mutation", ["charge", "status", "kind", "reserve", "finished_at", "detail",
    "cap", "count", "namespace", "snapshot", "another_native", "another_scan", "new_unknown"])
def test_native_exception_preserves_original_boundaries(fixture, tmp_path, mutation):
    authorize_native(fixture, tmp_path)
    value, current, _ = fixture
    if mutation in {"charge", "status", "kind", "reserve", "finished_at", "detail"}:
        field, changed = {"charge": ("actual_usd", 0), "status": ("status", "failed"),
            "kind": ("kind", "interaction"), "reserve": ("reserve_usd", 1),
            "finished_at": ("finished_at", None), "detail": ("detail", "changed")}[mutation]
        current["stages"]["saved-native"][field] = changed
    elif mutation in {"cap", "count", "namespace"}:
        field, changed = {"cap": ("max_unresolved_usd", 99), "count": ("max_new_unknowns", 3),
            "namespace": ("analysis_stage_namespace", "reset-stream-v1")}[mutation]
        value[field] = changed
    elif mutation == "snapshot":
        value["snapshot"] = pin(tmp_path / "new-snapshot.json", current)
    elif mutation == "another_native":
        current["stages"]["another-native"] = stage(status="complete", kind="trials")
    elif mutation == "another_scan":
        current["stages"]["another-scan"] = stage(status="failed", kind="tells", actual=0.1)
    else:
        current["stages"]["new-stream-v1-unknown"] = stage()
    before = copy.deepcopy(current)
    with pytest.raises(ValueError):
        check(fixture)
    assert current == before


def test_retained_native_charge_counts_toward_unchanged_dollar_limit(fixture, tmp_path):
    authorize_native(fixture, tmp_path, reserve=40)
    with pytest.raises(ValueError, match="dollar exposure"):
        check(fixture)


@pytest.mark.parametrize("mutation", ["unresolved", "result_seal", "retry", "errored", "inflight",
    "uncompleted", "wrong_job", "wrong_event", "wrong_native", "second_missing", "verified_bill"])
def test_native_exception_rejects_incomplete_or_different_receipts(fixture, tmp_path, mutation):
    exception = authorize_native(fixture, tmp_path)
    key = {"unresolved": "cleanup", "result_seal": "completion", "retry": "job", "errored": "job",
        "inflight": "job", "uncompleted": "job", "wrong_job": "job", "wrong_event": "missing_usage",
        "wrong_native": "missing_usage", "second_missing": "missing_usage", "verified_bill": "native_accounting"}[mutation]
    receipt = policy.artifact(exception[key])
    if mutation == "unresolved":
        receipt["unresolved"] = ["one"]
    elif mutation == "result_seal":
        receipt["result_sha256"] = "changed"
    elif mutation in {"retry", "errored", "inflight", "uncompleted"}:
        field, changed = {"retry": ("n_retries", 1), "errored": ("n_errored_trials", 1),
            "inflight": ("n_running_trials", 1), "uncompleted": ("n_completed_trials", 2)}[mutation]
        receipt["stats"][field] = changed
    elif mutation == "wrong_job":
        receipt["id"] = "replacement-job"
    elif mutation == "wrong_event":
        receipt["missing_events"][0]["event_uuid"] = "other"
    elif mutation == "wrong_native":
        receipt["native"]["sha256"] = "other"
    elif mutation == "second_missing":
        receipt["missing_events"].append(copy.deepcopy(receipt["missing_events"][0]))
    else:
        receipt["status"] = "verified"
    exception[key] = pin(tmp_path / "changed-receipt.json", receipt)
    fixture[0]["retained_native_failure"] = pin(tmp_path / "changed-exception.json", exception)
    with pytest.raises(ValueError):
        check(fixture)

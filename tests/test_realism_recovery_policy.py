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
             "max_unresolved_usd": 50, "max_new_unknowns": 2, "analysis_stage_namespace": "new-stream-v1"}
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
    with pytest.raises(ValueError, match="Repeated"):
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
    with pytest.raises(ValueError, match='Repeated'):
        check(fixture)

"""Synthetic-only retained-hold proofs; no real receipts, preparation or paid calls."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.realism import autonomous, measurement, stages
from refund_eval.realism import retained_hold as hold


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return path


def ref(repo, path):
    return {"path": str(path.relative_to(repo)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def identity(path):
    stat = path.stat()
    return path.read_bytes(), stat.st_ino, stat.st_mtime_ns


def make_case(tmp_path, monkeypatch, *, repo=None, ledger=None, run_dir=None):
    """Reusable entirely synthetic proof constructor; patches ONLY the scope pin.

    Callers can reuse the actual factory/guard. This never reads the real ledger,
    creates an actual independent approval, or copies trial/provider material.
    """
    repo = repo or tmp_path / "synthetic-repo"
    root = repo / ".context/realism/synthetic"
    original = root / "original"
    run_dir = run_dir or root / "future"
    ledger = ledger or root / "spend.json"
    request = {"version": 1, "shared_ledger": str(ledger), "protocol": {"synthetic": True}}
    fingerprint = measurement._json_hash(request)
    stage = f"loop-{fingerprint[:16]}-candidate-trials"
    original_stage = {"kind": "trials", "status": "failed", "reserve_usd": 7.762499999999999,
                      "actual_usd": None, "started_at": "2026-10-28T20:00:00+00:00",
                      "finished_at": "2026-10-28T20:30:00+00:00"}
    value = {"version": 1, "budget_usd": 100, "budget_amendments": [{"id": "synthetic100"}],
             "stages": {"historical-complete": {**original_stage, "status": "complete", "actual_usd": 47,
                                                "reserve_usd": 48}, stage: original_stage}}
    write(ledger, value)
    ledger.with_suffix(ledger.suffix + ".lock").touch()
    mapping = {f"sandbox-synthetic-{i}": f"session-synthetic-{i}" for i in range(3)}
    cleanup = {"sessions": list(mapping.values()), "terminated": list(mapping), "unresolved": []}
    result = {"fingerprint": fingerprint, "status": "reconciliation_required", "cleanup": cleanup,
              "spend": stages.summary(value)}
    files = {
        "request": write(original / "request.json", {**request, "fingerprint": fingerprint}),
        "start": write(original / "execution-started.json", {"fingerprint": fingerprint}),
        "result": write(original / "result.json", result),
        "completion": write(original / "completion.json", {"fingerprint": fingerprint,
                                                           "result_sha256": measurement._json_hash(result)}),
        "cleanup": write(original / "modal-cleanup.json", cleanup),
    }
    files["terminal_review"] = write(root / "gemini-011-terminal-review.json", {
        "approved": True, "request_fingerprint": fingerprint, "native_actual_usd": None})
    files["accounting_review"] = write(root / "auditor/gemini-011-accounting-final-readback.json", {
        "request_fingerprint": fingerprint, "native_actual_usd": None,
        "original_result_sha256": ref(repo, files["result"])["sha256"],
        "original_completion_sha256": ref(repo, files["completion"])["sha256"]})
    files["terminal_disposition"] = write(root / "gemini011-terminal-disposition-001.json", {"synthetic": True})
    files["exception_bundle"] = write(root / "loop-observer-gemini-011-scan-reconciliation-receipt.json", {
        "run": str(original), "source_files": {str(path): ref(repo, path)["sha256"] for path in files.values()}})
    files["budget_review"] = write(root / "budget-amendment-100-review-001.json", {
        "original_ledger": str(ledger), "amendment_id": "synthetic100"})
    files["terminal_refresh"] = write(root / "saved-terminal-refresh.json", {
        "original_cleanup_sha256": ref(repo, files["cleanup"])["sha256"], "all_owned_and_terminal": True,
        "rows": [{"sandbox_id": key, "session": session, "owned": True, "terminal": True,
                  "managed": "true", "returncode": 137} for key, session in mapping.items()]})
    proofs = ["terminal_review", "accounting_review", "terminal_disposition", "exception_bundle", "budget_review"]
    scope = {"protocol_proposal": hold.VERSION, "scope_approved": True, "implementation_scope_supported": True,
             "exact_original_stage": stage, "frozen_stage_snapshot": original_stage,
             "reused_proofs": [{"path": str(files[name].relative_to(root)), "sha256": ref(repo, files[name])["sha256"]}
                               for name in proofs],
             "termination_assessment": {"owned_terminal_ids": mapping, "supplemental_refresh": {
                 "path": str(files["terminal_refresh"].relative_to(root)),
                 "sha256": ref(repo, files["terminal_refresh"])["sha256"]}}}
    files["scope_review"] = write(root / "auditor/synthetic-scope.json", scope)
    monkeypatch.setattr(hold, "SCOPE_SHA256", ref(repo, files["scope_review"])["sha256"])
    base_request = {"candidate": "synthetic-future-source", "protocol": {"synthetic": True}}
    binding = hold.future_binding(repo, run_dir, base_request)
    review = {"version": hold.VERSION, "review_kind": "retained_native_hold_disposition_review", "approved": True,
              "reviewer": "INDEPENDENT SYNTHETIC FIXTURE ONLY", "reviewed_at": "2026-10-28T21:00:00+00:00",
              "acknowledgements": dict.fromkeys(hold.ACKNOWLEDGEMENTS, True),
              "validator_sha256": hashlib.sha256(Path(hold.__file__).read_bytes()).hexdigest(),
              "ledger": {**ref(repo, ledger), "budget_usd": 100, "amendment_id": "synthetic100",
                         "amendment_sha256": measurement._json_hash(value["budget_amendments"][-1])},
              "stage": {"id": stage, "sha256": measurement._json_hash(original_stage)},
              "artifacts": {name: ref(repo, path) for name, path in files.items()}, "future_binding": binding}
    review_path = write(root / "synthetic-disposition.json", review)
    return {"repo": repo, "ledger": ledger, "run_dir": run_dir, "original": original, "files": files,
            "review": review, "review_path": review_path, "binding": binding, "value": deepcopy(value),
            "stage": stage, "base_request": base_request}


@pytest.fixture
def case(tmp_path, monkeypatch):
    return make_case(tmp_path, monkeypatch)


def freeze(case, required=10):
    return hold.freeze_disposition(case["repo"], case["review_path"], ledger=case["ledger"], budget=100,
                                   binding=case["binding"], required_reserve=required)


def refresh_review(case):
    case["review"]["ledger"].update(ref(case["repo"], case["ledger"]))
    write(case["review_path"], case["review"])


def guard(case, *, trial=4, scan=2):
    frozen = freeze(case, trial + scan)
    fingerprint = measurement._json_hash({**case["base_request"], "prior_unknown_disposition": frozen})
    write(case["run_dir"] / "execution-started.json", {"fingerprint": fingerprint})
    return hold.ReservationGuard(case["repo"], case["review_path"], ledger=case["ledger"], budget=100,
                                 binding=case["binding"], disposition=frozen, fingerprint=fingerprint,
                                 trial_reserve=trial, scan_reserve=scan)


def test_factory_is_readonly_and_freezes_all_identities(case):
    watched = [case["ledger"], case["review_path"], *case["files"].values()]
    before = {path: identity(path) for path in watched}
    frozen = freeze(case)
    assert frozen["stage"]["id"] == case["stage"] and frozen["version"] == hold.VERSION
    assert frozen["review"] == ref(case["repo"], case["review_path"])
    assert set(frozen["artifacts"]) == hold.ARTIFACTS
    assert hold.read_spend(case["ledger"], 100)["held_usd"] == 7.762499999999999
    assert before == {path: identity(path) for path in watched}


@pytest.mark.parametrize("field,value", [
    ("approved", False), ("version", "blanket-allow"), ("review_kind", "execution_review"),
    ("reviewer", ""), ("reviewed_at", 123), ("reviewed_at", "2026-10-28T21:00:00"),
    ("validator_sha256", "0" * 64), ("acknowledgements", {}), ("extra", True),
    ("artifacts", []), ("future_binding", {}), ("stage", []), ("ledger", []),
])
def test_malformed_or_unapproved_receipt_rejects_without_write(case, field, value):
    case["review"][field] = value
    write(case["review_path"], case["review"])
    before = identity(case["ledger"])
    with pytest.raises((ValueError, TypeError)):
        freeze(case)
    assert before == identity(case["ledger"])


@pytest.mark.parametrize("body", ['{"version":1,"version":2}', '{"x":NaN}', '[]'])
def test_noncanonical_json_rejects(case, body):
    case["review_path"].write_text(body)
    with pytest.raises(ValueError):
        freeze(case)


@pytest.mark.parametrize("mutation", ["hold", "actual", "active", "finished", "foreign", "amendment", "boolean", "stale"])
def test_changed_old_or_other_unknown_stage_blocks_even_if_review_digest_updated(case, mutation):
    value = deepcopy(case["value"])
    stage = value["stages"][case["stage"]]
    if mutation == "hold":
        stage["reserve_usd"] = 7.76
    elif mutation == "actual":
        stage["actual_usd"] = 0
    elif mutation == "active":
        stage["status"] = "started"
    elif mutation == "finished":
        stage.pop("finished_at")
    elif mutation == "amendment":
        value["budget_amendments"].append({"id": "foreign"})
    elif mutation == "boolean":
        value["stages"]["historical-complete"]["actual_usd"] = True
    else:
        value["stages"]["foreign"] = {**stage, "status": "started"}
    write(case["ledger"], value)
    if mutation != "stale":
        refresh_review(case)
    before = identity(case["ledger"])
    with pytest.raises(ValueError):
        freeze(case)
    assert before == identity(case["ledger"])


@pytest.mark.parametrize("mutation", ["future", "request", "artifact", "foreign-cleanup", "symlink", "marker", "original"])
def test_identity_or_replay_mismatch_blocks(case, mutation):
    if mutation == "future":
        case["binding"] = {**case["binding"], "run_directory": case["binding"]["run_directory"] + "2"}
    elif mutation == "request":
        case["binding"] = {**case["binding"], "request_sha256": "0" * 64}
    elif mutation == "artifact":
        case["files"]["result"].write_text("{}")
    elif mutation == "foreign-cleanup":
        case["review"]["artifacts"]["cleanup"] = ref(case["repo"], case["files"]["request"])
        write(case["review_path"], case["review"])
    elif mutation == "symlink":
        link = case["review_path"].with_name("link.json")
        link.symlink_to(case["review_path"])
        case["review_path"] = link
    elif mutation == "marker":
        write(case["run_dir"] / "execution-started.json", {"fingerprint": "0" * 64})
    else:
        case["binding"]["run_directory"] = str(case["original"].relative_to(case["repo"]))
        write(case["review_path"], case["review"])
    with pytest.raises(ValueError):
        freeze(case)


def test_full_hold_is_counted_in_affordability(case):
    assert freeze(case, 45)
    with pytest.raises(ValueError, match="do not fit"):
        freeze(case, 45.3)


def test_native_to_tells_is_the_only_permitted_transition(case):
    original = deepcopy(case["value"])
    g = guard(case)
    g.reserve(g.tag + "-trials", "trials", 4)
    stages.finish(case["ledger"], 100, g.tag + "-trials", "complete", actual=1.25)
    g.reserve(g.tag + "-tells", "tells", 2)
    current = json.loads(case["ledger"].read_bytes())
    assert {key: current[key] for key in current if key != "stages"} == {key: original[key] for key in original if key != "stages"}
    for name, value in original["stages"].items():
        assert current["stages"][name] == value
    assert current["stages"][case["stage"]]["actual_usd"] is None
    assert stages.summary(current)["held_usd"] == 9.7625
    before = identity(case["ledger"])
    with pytest.raises(ValueError):
        g.reserve(g.tag + "-tells", "tells", 2)
    assert before == identity(case["ledger"])


@pytest.mark.parametrize("where", ["first", "scan"])
@pytest.mark.parametrize("mutation", ["old", "foreign", "review", "unknown", "failed"])
def test_atomic_reservation_rejects_current_mutations_without_writing(case, where, mutation):
    g = guard(case)
    if where == "scan":
        g.reserve(g.tag + "-trials", "trials", 4)
        stages.finish(case["ledger"], 100, g.tag + "-trials", "complete", actual=1)
    value = json.loads(case["ledger"].read_bytes())
    if mutation == "review":
        case["review_path"].write_text("{}")
    elif where == "scan" and mutation in {"unknown", "failed"}:
        value["stages"][g.tag + "-trials"].update(actual_usd=None, status=mutation if mutation == "failed" else "complete")
        write(case["ledger"], value)
    elif mutation == "old":
        value["stages"][case["stage"]]["reserve_usd"] = 7
        write(case["ledger"], value)
    else:
        value["stages"]["foreign"] = deepcopy(value["stages"][case["stage"]])
        write(case["ledger"], value)
    before = identity(case["ledger"])
    with pytest.raises(ValueError):
        g.reserve(g.tag + ("-trials" if where == "first" else "-tells"), "trials" if where == "first" else "tells", 4 if where == "first" else 2)
    assert identity(case["ledger"]) == before


def test_callback_runs_in_current_stable_lock_before_duplicate_and_budget_checks(case):
    seen = []
    before = identity(case["ledger"])

    def reject(current):
        seen.append(deepcopy(current))
        raise ValueError("synthetic validation rejected")

    with pytest.raises(ValueError, match="synthetic validation"):
        stages.reserve(case["ledger"], 100, case["stage"], "trials", 1000, validate=reject)
    assert seen == [case["value"]] and identity(case["ledger"]) == before


def test_scan_reserve_still_checks_current_affordability(case):
    g = guard(case)
    g.reserve(g.tag + "-trials", "trials", 4)
    stages.finish(case["ledger"], 100, g.tag + "-trials", "complete", actual=44)
    before = identity(case["ledger"])
    with pytest.raises(ValueError, match="no longer fits"):
        g.reserve(g.tag + "-tells", "tells", 2)
    assert before == identity(case["ledger"])


def test_opted_preflight_never_uses_mutating_show(case, monkeypatch):
    monkeypatch.setattr(stages, "show", lambda *a: pytest.fail("mutating show is forbidden"))
    context = SimpleNamespace(check_cancelled=lambda: None, ledger=case["ledger"], settings={"budget_usd": 100},
                              read_spend=hold.read_spend)
    before = identity(case["ledger"])
    autonomous._preflight(context, 10)
    assert before == identity(case["ledger"])


def test_binding_keeps_accounting_outside_observation_protocol(case):
    settings = {"time_limit": 1800, "trial_deadline_policy": "bounded-lifecycle-v1", "scan_protocol": "frozen-parts-v1"}
    protocol = autonomous._protocol(settings)
    base = {"protocol": protocol, "candidate": "new-source", "runner": "new-runner"}
    case["binding"] = hold.future_binding(case["repo"], case["run_dir"], base)
    case["review"]["future_binding"] = case["binding"]
    write(case["review_path"], case["review"])
    frozen = freeze(case)
    extended = {**base, "prior_unknown_disposition": frozen}
    assert extended["protocol"] == protocol
    assert protocol["trial_deadlines"]["command_hard_seconds"] == 4200
    assert protocol["scan_protocol"] == "frozen-parts-v1"
    assert measurement._json_hash(base) != measurement._json_hash(extended)
    changed = {**base, "runner": "foreign-runner"}
    assert hold.future_binding(case["repo"], case["run_dir"], changed) != case["binding"]


@pytest.mark.parametrize("mutation", ["unresolved", "duplicate", "foreign", "bool-code", "session", "managed", "unknown"])
def test_exact_terminal_semantics_reject_incomplete_or_foreign_proofs(case, mutation):
    proofs, _ = hold._proof_chain(case["repo"], case["review"]["artifacts"])
    proofs = deepcopy(proofs)
    if mutation == "unresolved":
        proofs["cleanup"]["unresolved"] = ["foreign"]
    elif mutation == "unknown":
        proofs["cleanup"]["remote_state"] = "unknown"
    elif mutation == "duplicate":
        proofs["terminal_refresh"]["rows"][1] = deepcopy(proofs["terminal_refresh"]["rows"][0])
    else:
        row = proofs["terminal_refresh"]["rows"][0]
        if mutation == "foreign":
            row["sandbox_id"] = "foreign"
        elif mutation == "bool-code":
            row["returncode"] = True
        elif mutation == "session":
            row["session"] = "foreign"
        else:
            row["managed"] = True
    with pytest.raises(ValueError):
        hold._termination(proofs, case["review"]["artifacts"])


def test_budget_and_noncanonical_money_reject(case):
    for budget in (True, 99, 101, float("inf")):
        with pytest.raises(ValueError):
            hold.freeze_disposition(case["repo"], case["review_path"], ledger=case["ledger"], budget=budget,
                                    binding=case["binding"], required_reserve=5)
    for required in (True, 0, -1, float("nan")):
        with pytest.raises(ValueError):
            freeze(case, required)


def test_real_batch_uses_both_guarded_reservations_and_preserves_old_hold(case, monkeypatch):
    g = guard(case)
    context = SimpleNamespace(ledger=case["ledger"], settings={"budget_usd": 100}, directory=case["run_dir"],
                              check_cancelled=lambda: None, update=lambda *a, **k: None,
                              reserve_stage=g.reserve, read_spend=hold.read_spend)
    monkeypatch.setattr(autonomous, "_assert_source", lambda *a, **k: None)
    monkeypatch.setattr(autonomous, "_scan_preflight", lambda *a: {})
    monkeypatch.setattr(autonomous, "_prepare_trials", lambda *a: {"commit": "future", "dirty": False})
    observed = []

    def trials(*args):
        current = hold.read_spend(case["ledger"], 100)
        assert current["stages"][g.tag + "-trials"]["status"] == "started"
        observed.append("native")
        return {"returncode": 0, "failed": 0, "valid_trials": 1, "logs": ["synthetic.eval"], "cost_usd": 1}

    def scan(*args):
        current = hold.read_spend(case["ledger"], 100)
        assert current["stages"][g.tag + "-tells"]["status"] == "started"
        observed.append("scan")
        return {"cost_usd": .25}

    monkeypatch.setattr(autonomous, "_run_trials", trials)
    monkeypatch.setattr(autonomous, "_scan_plan", lambda *a: {"required_usd": 1})
    monkeypatch.setattr(autonomous, "_run_scan", scan)
    monkeypatch.setattr(autonomous, "_score", lambda *a: {"per_trial": [1], "trials": 1, "tells_per_trial": 1})
    monkeypatch.setattr(autonomous, "_mine", lambda *a: [])
    monkeypatch.setattr(stages, "show", lambda *a: pytest.fail("Opted batch used mutating read"))
    batch = autonomous._batch(context, {"trials": 1}, "future", {}, g.tag, cwd=case["repo"], trial_reserve=4, scan_reserve=2)
    assert observed == ["native", "scan"] and batch["score"]["per_trial"] == [1]
    current = hold.read_spend(case["ledger"], 100)
    assert current["stages"][case["stage"]] == case["value"]["stages"][case["stage"]]
    assert current["held_usd"] == 7.762499999999999


def test_tells_rejects_numeric_representation_change_in_historical_stage(case):
    g = guard(case)
    g.reserve(g.tag + "-trials", "trials", 4)
    stages.finish(case["ledger"], 100, g.tag + "-trials", "complete", actual=1)
    value = json.loads(case["ledger"].read_bytes())
    value["stages"]["historical-complete"]["actual_usd"] = 47.0
    write(case["ledger"], value)
    before = identity(case["ledger"])
    with pytest.raises(ValueError, match="Historical ledger"):
        g.reserve(g.tag + "-tells", "tells", 2)
    assert identity(case["ledger"]) == before


def test_tells_requires_its_exact_reserved_native_start(case):
    g = guard(case)
    g.reserve(g.tag + "-trials", "trials", 4)
    stages.finish(case["ledger"], 100, g.tag + "-trials", "complete", actual=1)
    value = json.loads(case["ledger"].read_bytes())
    value["stages"][g.tag + "-trials"]["started_at"] = "2026-10-28T00:00:00+00:00"
    write(case["ledger"], value)
    before = identity(case["ledger"])
    with pytest.raises(ValueError, match="unfinished or unknown"):
        g.reserve(g.tag + "-tells", "tells", 2)
    assert identity(case["ledger"]) == before


def test_validator_callback_owns_the_existing_stable_flock(case):
    import fcntl

    def reject(_):
        with case["ledger"].with_suffix(".json.lock").open("r") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raise ValueError("locked synthetic stop")

    before = identity(case["ledger"])
    with pytest.raises(ValueError, match="locked synthetic stop"):
        stages.reserve(case["ledger"], 100, "new", "trials", 1, validate=reject)
    assert identity(case["ledger"]) == before


@pytest.mark.parametrize("field,value", [("finished_at", True), ("started_at", "naive"),
                                         ("reserve_usd", 10 ** 400), ("status", "invented"),
                                         ("kind", "invented"), ("kind", []), ("status", []), ("detail", False)])
def test_noncanonical_stage_types_and_extreme_money_reject(case, field, value):
    current = deepcopy(case["value"])
    current["stages"]["historical-complete"][field] = value
    write(case["ledger"], current)
    refresh_review(case)
    before = identity(case["ledger"])
    with pytest.raises(ValueError):
        freeze(case)
    assert identity(case["ledger"]) == before

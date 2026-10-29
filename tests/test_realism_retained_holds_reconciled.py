"""Versioned failed-zero nonattempt controls; wholly synthetic saved evidence."""
import stat
import subprocess
from copy import deepcopy

import pytest
from test_realism_retained_hold import ref, write
from test_realism_retained_holds_three import make_three

from refund_eval.realism import measurement, stages
from refund_eval.realism import retained_hold as hold


def make_reconciled(tmp_path, monkeypatch):
    case = make_three(tmp_path, monkeypatch)
    repo, root = case["repo"], case["root"]
    subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
    original = root / "synthetic-nonattempt"
    body = {"mode": "candidate_only", "shared_ledger": str(case["ledger"]), "candidate": "synthetic-environment",
            "runner": "synthetic-runner", "baseline_evidence": None, "historical_reference": None}
    fingerprint = measurement._json_hash(body)
    target = f"loop-{fingerprint[:16]}-candidate-trials"
    stage = {"kind": "trials", "status": "failed", "reserve_usd": 7.762499999999999, "actual_usd": None,
             "started_at": "2026-10-29T01:00:00+00:00", "finished_at": "2026-10-29T01:01:00+00:00",
             "detail": "Synthetic failed configuration"}
    before = deepcopy(case["value"])
    before["stages"][target] = stage
    after = deepcopy(before)
    after["stages"][target].update(actual_usd=0.0, finished_at="2026-10-29T01:10:00+00:00",
                                   detail="Synthetic independently proved nonattempt")
    cleanup = {"sessions": [], "terminated": [], "unresolved": []}
    result = {"fingerprint": fingerprint, "mode": "candidate_only", "status": "reconciliation_required",
              "error_type": "ValueError", "historical_reference": None, "comparison": None,
              "cleanup": cleanup, "spend": stages.summary(before)}
    files = {
        "request": write(original / "request.json", {**body, "fingerprint": fingerprint}),
        "start": write(original / "execution-started.json", {"fingerprint": fingerprint}),
        "result": write(original / "result.json", result),
        "raw_result": write(root / "synthetic-raw-result.json", result),
        "state": write(original / "state.json", {"status": "reconciliation_required"}),
        "completion": write(original / "completion.json", {"fingerprint": fingerprint,
                          "result_sha256": measurement._json_hash(result)}),
        "cleanup": write(original / "modal-cleanup.json", cleanup),
        "native_accounting": write(original / "native-accounting.json", {"status": "unknown", "logs": []}),
        "before": write(root / "synthetic-nonattempt-before.json", before),
        "after": write(root / "synthetic-nonattempt-after.json", after),
        "terminal_proof": write(root / "synthetic-nonattempt-terminal-proof.json", {"request_fingerprint": fingerprint,
                                "original_execution_session": 1234}),
        "financial_proof": write(root / "synthetic-nonattempt-financial-proof.json", {"synthetic": True}),
        "financial_proposal": write(root / "synthetic-nonattempt-financial-proposal.json", {"synthetic": True}),
    }
    files["trace"] = original / "synthetic-trace.log"
    files["trace"].write_text("Synthetic nonattempt trace only; no real native content.\n")
    files["finish_script"] = root / "synthetic-finish-script.py"
    files["finish_script"].write_text("raise AssertionError('Evidence must never execute')\n")

    def session_ref(name):
        return {"path": str(files[name].relative_to(root)), "sha256": ref(repo, files[name])["sha256"]}

    files["launch_intent"] = write(root / "synthetic-nonattempt-launch-intent.json", {
        "request_fingerprint": fingerprint, "launch_intent_at": "2026-10-29T01:00:00+00:00",
        "fixed_observer_deadline": "2026-10-29T03:26:05+00:00"})
    files["process"] = write(root / "synthetic-nonattempt-process.json", {
        "request_fingerprint": fingerprint, "returncode": 1, "stdout_sha256": ref(repo, files["raw_result"])["sha256"]})
    files["callback"] = write(root / "synthetic-nonattempt-callback.json", {
        "request_fingerprint": fingerprint, "original_execution_session": 1234,
        "earliest_launch_intent": "2026-10-29T01:00:00+00:00", "fixed_observer_deadline": "2026-10-29T03:26:05+00:00",
        "deadline_reset": False, "normal_watch_started": False, "normal_terminal_collector_invocations": 0})
    files["harvest"] = write(root / "synthetic-nonattempt-harvest.json", {
        "original_execution_session": 1234, "closed": True, "exit_code": 1,
        "after_observer_callback": session_ref("callback"), "process_receipt": session_ref("process"),
        "result": session_ref("result"), "completion": session_ref("completion")})
    files["terminal_review"] = write(root / "synthetic-nonattempt-terminal.json", {
        "request_fingerprint": fingerprint, "original_execution_session": 1234, "approved": True,
        "native_model_nonattempt_supported": True, "native_job_run_reached": False,
        "owned_trial_sandbox_launch_reachable": False, "complete_native_samples": 0,
        "strict_scan_coverage_accepted": False, "comparison_accepted": False, "comparison": None,
        "proof": session_ref("terminal_proof"), "original_harvest": session_ref("harvest"),
        "observer_exception_callback": session_ref("callback")})
    normalized = deepcopy(after)
    normalized["stages"][target]["finished_at"] = "<TARGET_FINISHED_AT>"
    normalized_sha = measurement._json_hash(normalized)
    files["financial_gate"] = write(root / "synthetic-nonattempt-financial-gate.json", {
        "approved": True, "one_normal_native_finish_approved": True, "accounting_only": True,
        "request_fingerprint": fingerprint, "stage_id": target, "actual_usd": 0.0, "status": "failed",
        "script_sha256": ref(repo, files["finish_script"])["sha256"],
        "proposal_sha256": ref(repo, files["financial_proposal"])["sha256"],
        "proof": session_ref("financial_proof"), "ledger_sha256": ref(repo, files["before"])["sha256"],
        "detail": after["stages"][target]["detail"], "normalized_postimage_sha256": normalized_sha,
        "postimage_normalization": {"target": target, "only_field": "finished_at", "placeholder": "<TARGET_FINISHED_AT>"}})
    common = {"gate_sha256": ref(repo, files["financial_gate"])["sha256"],
              "proposal_sha256": ref(repo, files["financial_proposal"])["sha256"],
              "stage_id": target, "actual_usd": 0.0, "status": "failed", "accounting_only": True}
    files["finish_intent"] = write(root / "synthetic-nonattempt-finish-intent.json", {
        **common, "ledger_sha256": ref(repo, files["before"])["sha256"]})
    files["finish_receipt"] = write(root / "synthetic-nonattempt-finish-receipt.json", {
        **common, "request_fingerprint": fingerprint, "original_execution_session": 1234, "normal_finish_calls": 1,
        "before_ledger_sha256": ref(repo, files["before"])["sha256"],
        "after_ledger_sha256": ref(repo, files["after"])["sha256"], "normalized_postimage_sha256": normalized_sha})
    files["final_accounting"] = write(root / "synthetic-nonattempt-final-accounting.json", {
        "approved": True, "financial_reconciliation_complete": True, "accounting_only": True,
        "request_fingerprint": fingerprint, "original_execution_session": 1234,
        "ledger_sha256": ref(repo, files["after"])["sha256"], "gate": session_ref("financial_gate"),
        "primary_finish_receipt": session_ref("finish_receipt"), "current_stage_snapshot": after,
        "original036_native_status": "failed", "original036_native_actual_usd": 0.0,
        "original_sealed_status_preserved": "reconciliation_required", "strict_scan_coverage_accepted": False,
        "comparison_accepted": False, "normalized_postimage_sha256": normalized_sha})
    descriptor = {"stage": {"id": target, "sha256": measurement._json_hash(after["stages"][target]),
                            "snapshot": after["stages"][target]},
                  "run_directory": str(original.relative_to(repo)), "request_fingerprint": fingerprint,
                  "artifacts": {name: ref(repo, path) for name, path in files.items()}}
    scope = {"scope_approved": True, "implementation_scope_supported": True, "protocol_proposal": hold.RECONCILED_VERSION,
             "expected_reconciled_nonattempt": deepcopy(descriptor)}
    scope_path = write(root / "auditor/synthetic-reconciled-scope.json", scope)
    monkeypatch.setattr(hold, "RECONCILED_SCOPE_SHA256", ref(repo, scope_path)["sha256"])
    descriptor["scope_review"] = ref(repo, scope_path)
    legacy_review = deepcopy(case["review"])
    review = {**case["review"], "version": hold.RECONCILED_VERSION,
              "review_kind": "retained_native_three_holds_reconciled_nonattempt_disposition_review",
              "reconciled_nonattempt": descriptor}
    write(case["ledger"], after)
    review["ledger"].update(ref(repo, case["ledger"]))
    review_path = write(root / "synthetic-reconciled-review.json", review)
    return {**case, "value": after, "before_nonattempt": before, "target": target, "files": files,
            "reconciled_scope": scope, "reconciled_scope_path": scope_path, "review": review,
            "review_path": review_path, "legacy_review": legacy_review}


@pytest.fixture
def reconciled(tmp_path, monkeypatch):
    return make_reconciled(tmp_path, monkeypatch)


def metadata(case):
    return {str(path): (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns,
                       path.stat().st_size, stat.S_IMODE(path.stat().st_mode))
            for path in case["root"].rglob("*") if path.is_file()}


def refresh(case):
    case["review"]["ledger"].update(ref(case["repo"], case["ledger"]))
    write(case["review_path"], case["review"])


def freeze(case, reserve=23.5125, binding=None):
    with measurement._exclusive(case["repo"]):
        return hold.freeze_disposition(case["repo"], case["review_path"], ledger=case["ledger"], budget=1000,
                                       binding=binding or case["binding"], required_reserve=reserve)


def reject_unchanged(case):
    before = metadata(case)
    with pytest.raises((ValueError, OSError)):
        freeze(case)
    assert metadata(case) == before


def guard(case):
    frozen = freeze(case)
    fingerprint = measurement._json_hash({**case["base"], "prior_unknown_disposition": frozen})
    write(case["future"] / "execution-started.json", {"fingerprint": fingerprint})
    return hold.ReservationGuard(case["repo"], case["review_path"], ledger=case["ledger"], budget=1000,
                                 binding=case["binding"], disposition=frozen, fingerprint=fingerprint,
                                 trial_reserve=7.762499999999999, scan_reserve=15.75)


def test_reconciled_factory_serializes_exact_case_without_mutation(reconciled):
    before = metadata(reconciled)
    frozen = freeze(reconciled)
    assert set(frozen) == hold.RECONCILED_FIELDS | {"review"}
    assert frozen["reconciled_nonattempt"] == reconciled["review"]["reconciled_nonattempt"]
    assert len(frozen["stages"]) == 3
    assert stages.summary(reconciled["value"])["held_usd"] == 23.287499999999998
    assert metadata(reconciled) == before


@pytest.mark.parametrize("name", sorted({"request", "start", "result", "state", "completion", "trace", "raw_result",
    "launch_intent", "process", "cleanup", "native_accounting", "harvest", "callback", "terminal_review", "terminal_proof",
    "financial_gate", "financial_proof", "financial_proposal", "finish_script", "finish_intent", "before", "after",
    "finish_receipt", "final_accounting"}))
@pytest.mark.parametrize("mutation", ["bytes", "missing", "symlink"])
def test_each_scoped_artifact_must_remain_exact(reconciled, name, mutation):
    path = reconciled["files"][name]
    if mutation == "bytes":
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "missing":
        path.unlink()
    else:
        saved = path.with_suffix(path.suffix + ".saved")
        path.rename(saved)
        path.symlink_to(saved)
    reject_unchanged(reconciled)


@pytest.mark.parametrize("field,value", [
    ("actual_usd", 1), ("actual_usd", -1), ("actual_usd", True), ("actual_usd", None),
    ("actual_usd", float("inf")), ("actual_usd", 0), ("status", "complete"), ("status", "unknown"),
    ("status", "started"), ("finished_at", None), ("finished_at", "2026-10-29T00:00:00+00:00"),
    ("detail", "forged"), ("reserve_usd", 7), ("started_at", "2026-10-29T00:00:00+00:00"), ("extra", True),
])
def test_entire_reconciled_current_stage_is_immutable(reconciled, field, value):
    current = deepcopy(reconciled["value"])
    current["stages"][reconciled["target"]][field] = value
    write(reconciled["ledger"], current)
    refresh(reconciled)
    reject_unchanged(reconciled)


@pytest.mark.parametrize("field,value", [
    ("version", hold.THREE_VERSION), ("validator_sha256", "0" * 64), ("approved", 1),
    ("reviewer", " "), ("reviewed_at", "2026-10-29"), ("extra", True),
    ("reconciled_nonattempt", {}), ("acknowledgements", {}), ("stages", []),
])
def test_review_schema_source_and_acknowledgements_remain_strict(reconciled, field, value):
    reconciled["review"][field] = value
    refresh(reconciled)
    reject_unchanged(reconciled)


@pytest.mark.parametrize("where", ["stage", "artifacts", "scope_review", "request_fingerprint", "run_directory", "extra"])
def test_no_widened_reconciled_descriptor(reconciled, where):
    descriptor = reconciled["review"]["reconciled_nonattempt"]
    descriptor[where] = {} if where in {"stage", "artifacts", "scope_review"} else "foreign"
    refresh(reconciled)
    reject_unchanged(reconciled)


@pytest.mark.parametrize("status,actual,finished", [("failed", 0, True), ("failed", 1, True),
    ("unknown", None, True), ("complete", None, True), ("started", 0, False), ("complete", 1, False)])
def test_no_other_failed_unknown_or_unfinished_stage_is_allowed(reconciled, status, actual, finished):
    current = deepcopy(reconciled["value"])
    stage = deepcopy(current["stages"][reconciled["target"]])
    stage.update(status=status, actual_usd=actual)
    if not finished:
        stage.pop("finished_at")
    current["stages"]["foreign-stage"] = stage
    write(reconciled["ledger"], current)
    refresh(reconciled)
    reject_unchanged(reconciled)


def test_reviewed_known_complete_later_stage_is_permitted(reconciled):
    current = deepcopy(reconciled["value"])
    current["stages"]["later-known-stage"] = {**current["stages"][reconciled["target"]], "status": "complete", "actual_usd": 1}
    write(reconciled["ledger"], current)
    refresh(reconciled)
    assert freeze(reconciled)


@pytest.mark.parametrize("field", ["candidate", "runner", "protocol"])
def test_changed_future_request_identity_rejects(reconciled, field):
    body = {**reconciled["base"], field: "foreign"}
    binding = hold.future_binding(reconciled["repo"], reconciled["future"], body)
    before = metadata(reconciled)
    with pytest.raises(ValueError):
        freeze(reconciled, binding=binding)
    assert metadata(reconciled) == before


def test_v3_still_rejects_even_reconciled_failed_zero(reconciled):
    review = deepcopy(reconciled["legacy_review"])
    review["ledger"].update(ref(reconciled["repo"], reconciled["ledger"]))
    write(reconciled["review_path"], review)
    reject_unchanged(reconciled)


def test_normal_native_then_scan_transition_retains_three_holds(reconciled):
    g = guard(reconciled)
    with measurement._exclusive(reconciled["repo"]):
        g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
        stages.finish(reconciled["ledger"], 1000, g.tag + "-trials", "complete", actual=1)
        g.reserve(g.tag + "-tells", "tells", g.scan_reserve)
    current = hold._json(reconciled["ledger"].read_bytes())
    assert current["stages"][reconciled["target"]] == reconciled["value"]["stages"][reconciled["target"]]
    assert stages.summary(current)["held_usd"] == 23.287499999999998 + 15.75


@pytest.mark.parametrize("when", ["native", "scan"])
@pytest.mark.parametrize("mutation", ["proof", "review", "history", "foreign", "unknown", "failed", "own-start", "budget"])
def test_reservation_callback_revalidates_proofs_history_and_transition(reconciled, when, mutation):
    g = guard(reconciled)
    with measurement._exclusive(reconciled["repo"]):
        if when == "scan":
            g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
            stages.finish(reconciled["ledger"], 1000, g.tag + "-trials", "complete", actual=1)
        current = hold._json(reconciled["ledger"].read_bytes())
        if mutation == "proof":
            path = reconciled["files"]["terminal_proof"]
            path.write_bytes(path.read_bytes() + b" ")
        elif mutation == "review":
            reconciled["review_path"].write_bytes(reconciled["review_path"].read_bytes() + b" ")
        elif mutation == "history":
            current["stages"][reconciled["target"]]["detail"] = "forged"
            write(reconciled["ledger"], current)
        elif mutation in {"foreign", "unknown", "failed", "own-start"}:
            if when == "native" or mutation == "foreign":
                current["stages"]["foreign-stage"] = deepcopy(current["stages"][reconciled["target"]])
            else:
                native = current["stages"][g.tag + "-trials"]
                if mutation == "unknown":
                    native["actual_usd"] = None
                elif mutation == "failed":
                    native["status"] = "failed"
                else:
                    native["started_at"] = "2026-10-29T00:00:00+00:00"
            write(reconciled["ledger"], current)
        else:
            current["budget_usd"] = 1001
            write(reconciled["ledger"], current)
        before = metadata(reconciled)
        with pytest.raises(ValueError):
            suffix = "-trials" if when == "native" else "-tells"
            g.reserve(g.tag + suffix, "trials" if when == "native" else "tells",
                      g.trial_reserve if when == "native" else g.scan_reserve)
        assert metadata(reconciled) == before


def test_unaffordable_or_repeated_reservation_rejects_without_mutation(reconciled):
    before = metadata(reconciled)
    with pytest.raises(ValueError):
        freeze(reconciled, reserve=1000)
    assert metadata(reconciled) == before
    g = guard(reconciled)
    with measurement._exclusive(reconciled["repo"]):
        g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
        before = metadata(reconciled)
        with pytest.raises(ValueError):
            g.reserve(g.tag + "-trials", "trials", g.trial_reserve)
        assert metadata(reconciled) == before


@pytest.mark.parametrize("name,field,value", [
    ("terminal_review", "approved", 1), ("terminal_review", "native_model_nonattempt_supported", False),
    ("terminal_review", "native_job_run_reached", True), ("terminal_review", "complete_native_samples", 1),
    ("terminal_review", "comparison_accepted", True), ("terminal_review", "proof", {}),
    ("financial_gate", "approved", False), ("financial_gate", "detail", "forged"),
    ("financial_gate", "actual_usd", True), ("financial_gate", "status", "complete"),
    ("financial_gate", "script_sha256", "0" * 64), ("financial_gate", "normalized_postimage_sha256", "0" * 64),
    ("finish_receipt", "after_ledger_sha256", "0" * 64), ("finish_receipt", "normal_finish_calls", True),
    ("finish_receipt", "original_execution_session", 9999), ("finish_intent", "gate_sha256", "0" * 64),
    ("callback", "earliest_launch_intent", "2026-10-29T01:00:01+00:00"),
    ("callback", "normal_watch_started", True), ("completion", "result_sha256", "0" * 64),
    ("request", "runner", "foreign"), ("harvest", "closed", False), ("process", "returncode", True),
    ("final_accounting", "financial_reconciliation_complete", False),
    ("final_accounting", "original036_native_status", "complete"),
    ("final_accounting", "original036_native_actual_usd", -1), ("final_accounting", "current_stage_snapshot", {}),
])
def test_structural_joins_reject_even_with_synthetic_rescoped_bytes(reconciled, monkeypatch, name, field, value):
    path = reconciled["files"][name]
    body = hold._json(path.read_bytes())
    body[field] = value
    write(path, body)
    descriptor = reconciled["review"]["reconciled_nonattempt"]
    descriptor["artifacts"][name] = ref(reconciled["repo"], path)
    scope = reconciled["reconciled_scope"]
    scope["expected_reconciled_nonattempt"] = {key: deepcopy(value) for key, value in descriptor.items() if key != "scope_review"}
    write(reconciled["reconciled_scope_path"], scope)
    scope_ref = ref(reconciled["repo"], reconciled["reconciled_scope_path"])
    monkeypatch.setattr(hold, "RECONCILED_SCOPE_SHA256", scope_ref["sha256"])
    descriptor["scope_review"] = scope_ref
    refresh(reconciled)
    reject_unchanged(reconciled)

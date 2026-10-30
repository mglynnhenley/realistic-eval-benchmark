"""Exact reviewed original native holds; no reconciliation or ledger writes."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from refund_eval.realism import measurement, stages

VERSION = "retained-native-hold-v1"
PAIR_VERSION = "retained-native-holds-v2"
THREE_VERSION = "retained-native-holds-v3"
THREE_SCOPE_SHA256 = "045f23939ab3dae8636bca61899dc776e8b1a9d6026e29a1540a08d41d9d0a61"
THREE_ACKNOWLEDGEMENTS = {
    "retained_all_three_full_holds", "actual_and_invoice_unknown", "new_work_only",
    "no_original_replay_recovery_or_rescan", "reservation_is_not_provider_cap", "fourth_unknown_blocks",
}
PAIR_SCOPE_SHA256 = "cb42ebfd64bf9429ff8e9ab3479426e132a6e51f5b00d0b7e366de4e89aff45b"
# A reviewed scope, not authority for arbitrary future unknown stages. It pins
# the original case/proof identities without shipping any private trial content.
SCOPE_SHA256 = "0875abdb910398f9f1c0be5d20f8beb2c0eb3002895bd9b331a4308f059dcaee"
ARTIFACTS = {
    "scope_review", "terminal_review", "accounting_review", "exception_bundle",
    "terminal_disposition", "budget_review", "terminal_refresh",
    "request", "start", "result", "completion", "cleanup",
}
ACKNOWLEDGEMENTS = {
    "retained_full_hold", "actual_and_invoice_unknown", "new_work_only",
    "no_original_replay_recovery_or_rescan", "reservation_is_not_provider_cap",
}
FIELDS = {"version", "validator_sha256", "ledger", "stage", "artifacts", "future_binding"}
PAIR_FIELDS = (FIELDS - {"stage"}) | {"stages"}
PAIR_ACKNOWLEDGEMENTS = (ACKNOWLEDGEMENTS - {"retained_full_hold"}) | {"retained_both_full_holds"}

RECONCILED_VERSION = "retained-native-holds-v4"
RECONCILED_SCOPE_SHA256 = "de4db1c371968e68daa514dd46d12e960e2b8b19c360247f7709c3dbb2bde5b0"
RECONCILED_FIELDS = PAIR_FIELDS | {"reconciled_nonattempt"}

# One additional, request-bound terminal interaction, never arbitrary unknowns.
# These are original evidence digests, not billing or continuation approvals.
INTERACTION_VERSION = "retained-four-holds-continuation-v5"
INTERACTION_REQUEST_SHA256 = "87f249a06c05ac33d67e73cb575924986707a7a9e0af5e206b3e97c480955db9"
INTERACTION_ACCOUNTING_SHA256 = "ad0a207f6c9b3fc8c2cd89142efb531eadd2d887d7b612d6ed4666ba0ad9f25b"
INTERACTION_OWNER_REQUEST_SHA256 = "b649ec2e205797c407a4c2d4200511e6d512b65e19b7189b80951071255e88f7"
INTERACTION_FIELDS = RECONCILED_FIELDS | {"retained_interaction", "restart_authorization"}
INTERACTION_ACKNOWLEDGEMENTS = {
    "retained_all_four_full_holds", "actual_and_invoice_unknown", "new_work_only",
    "no_original_replay_recovery_or_rescan", "reservation_is_not_provider_cap",
    "no_cost_inference_or_hold_release", "fifth_unknown_blocks",
    "continuation_after_feedback_integration_only", "old_approvals_not_extended",
}
INTERACTION_SCOPE_FIELDS = {
    "version", "declaration_kind", "declared_by", "declared_at", "owner_request",
    "budget_usd", "ledger", "future_binding", "continuation_after_feedback_integration",
    "bounded_disposition_sha256", "acknowledgements",
}


# Finite historically reconciled FAILED scans, separate from unknown full holds.
FAILED_SCAN_VERSION = "retained-four-holds-known-failed-scans-v6"
FAILED_SCAN_OWNER_REQUEST_SHA256 = "6a82a23e063a46ad2b19a7932c0c507e465a03df288c5cb79784cbe4d2b26305"
FAILED_SCAN_V5_SCOPE_SHA256 = "e43c0f39814bf9187c322fe3e1acb341bf19e3adb117a4be1ec019cf0b6f84fd"
FAILED_SCAN_FIELDS = INTERACTION_FIELDS | {"reconciled_failed_scans"}
FAILED_SCAN_SCOPE_FIELDS = INTERACTION_SCOPE_FIELDS | {"original_v5_scope"}
FAILED_SCAN_ACKNOWLEDGEMENTS = INTERACTION_ACKNOWLEDGEMENTS | {
    "known_scans_failed_and_all_attempt_costs_exact", "original_failed_coverage_unaccepted",
    "old_v5_scope_provenance_only", "no_new_unknown_allowance",
}
FAILED_SCAN_PINS = {'loop-051047e3c5087e5c-candidate-tells': {'proof': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/claude-scan-accounting/normal-finish-proof-001.json',
                                                     'sha256': 'b3f81508cf17e2adc8b434d68d5eab0a21ba16bf743eb1a47b9d4feb97d4624e'},
                                           'receipt': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/claude-scan-accounting/normal-finish-receipt-001.json',
                                                       'sha256': '2dcc27b0745f1d989983ee74a434a5a4c78c7d75f4f8513806feca140e5939de'},
                                           'closure': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/claude-scan-accounting/attempt-closure-001.json',
                                                       'sha256': '6295544c070e2510d2b2d40001078137617f1e97305cb3ab708a5db9c0cdc31b'},
                                           'snapshot_sha256': '6bae19b6868af37c9238ea1e54c56355491ea7a64a84278a7f5379c766a1389c',
                                           'raw_charge_closure_sha256': '70b6419ce538d6687d5f1a8eae9092eb4cb2e43c5f9a37ee53427c76dd53353b',
                                           'actual_usd_decimal': '2.070124',
                                           'closure_kind': 'claude_refusal',
                                           'planned_parts': 4,
                                           'total_tokens': 497863,
                                           'original_run_directory': '.context/realism/20261030-continuous-001/loop-v4'},
 'deepseek-saved-native-001': {'proof': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/deepseek-scan-001-accounting/normal-finish-proof-001.json',
                                         'sha256': 'e7e96c86639339b316fad9667d86e11fedb297b3623a3a03859d37c8046a46ce'},
                               'receipt': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/deepseek-scan-001-accounting/normal-finish-receipt-001.json',
                                           'sha256': 'b4d0a4294e46a479f16069f0dfc509a17b49134735bdabe7465572122c249b7c'},
                               'closure': {'path': '.context/realism/20261030-continuous-001/deepseek-recovery-001/deepseek-scan-001-accounting/attempt-closure-001.json',
                                           'sha256': '1b229c7c12554b7418acd692a5f29e355f95197dd2f84322dd212e3415c23bb7'},
                               'snapshot_sha256': '6ba6e11ef0c6ea3886f57f0beba98a899ca568fdeb750704488df367d83a4656',
                               'raw_charge_closure_sha256': 'bad7935dddbb38cd5e639ecc61a4c0a4e7609f6e17d677e27405b9e5f57a4a93',
                               'actual_usd_decimal': '0.059002016',
                               'closure_kind': 'deepseek_reasoning_length',
                               'planned_parts': 3,
                               'total_tokens': 356944,
                               'original_run_directory': '.context/realism/20261030-continuous-001/deepseek-recovery-001/scan-001'}}


def require(condition, message):
    if not condition:
        raise measurement.Rejected(message)


def exact(value, keys):
    require(type(value) is dict and set(value) == set(keys), "Noncanonical retained-hold object fields")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def money(value, *, positive=False):
    try:
        return type(value) in (int, float) and math.isfinite(value) and (value > 0 if positive else value >= 0)
    except OverflowError:
        return False


def _time(value):
    if type(value) is not str:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _pairs(items):
    value = {}
    for key, item in items:
        require(key not in value, "Duplicate retained-hold JSON field")
        value[key] = item
    return value


def _json(raw):
    def invalid(_):
        raise measurement.Rejected("Nonfinite retained-hold JSON")
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)
    require(type(value) is dict, "Expected retained-hold JSON object")
    return value


def _path(repo, relative):
    require(type(relative) is str and relative.startswith(".context/realism/"), "Use private retained-hold artifacts")
    path = repo / relative
    require(path.resolve() == path and path.is_file(), "Retained-hold artifact missing or noncanonical")
    return path


def _artifact(repo, reference):
    exact(reference, {"path", "sha256"})
    require(type(reference["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]),
            "Invalid retained-hold artifact digest")
    path = _path(repo, reference["path"])
    raw = path.read_bytes()
    require(digest(raw) == reference["sha256"], "Retained-hold artifact changed")
    return path, _json(raw)


def _read_snapshot(ledger, budget):
    """Caller owns the stable ledger lock; never acquire it recursively."""
    raw = Path(ledger).read_bytes()
    value = _json(raw)
    exact(value, {"version", "budget_usd", "stages", "budget_amendments"})
    require(type(value["version"]) is int and value["version"] == 1
            and money(value["budget_usd"]) and value["budget_usd"] == budget
            and type(value["stages"]) is dict and type(value["budget_amendments"]) is list,
            "Retained-hold ledger schema or budget differs")
    for stage in value["stages"].values():
        require(type(stage) is dict
                and {"kind", "status", "reserve_usd", "actual_usd", "started_at"} <= set(stage)
                and set(stage) <= {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"}
                and type(stage["kind"]) is str and stage["kind"] in {"trials", "tells", "interaction", "coding", "review"}
                and type(stage["status"]) is str and stage["status"] in {"started", "complete", "failed", "unknown"}
                and money(stage["reserve_usd"], positive=True)
                and (stage["actual_usd"] is None or money(stage["actual_usd"]))
                and _time(stage["started_at"]) is not None,
                "Noncanonical retained-hold stage fields or money")
        if "finished_at" in stage:
            require(_time(stage["finished_at"]) is not None
                    and _time(stage["finished_at"]) >= _time(stage["started_at"]),
                    "Noncanonical retained-hold finish time")
        if "detail" in stage:
            require(type(stage["detail"]) is str, "Noncanonical retained-hold detail")
    return value, digest(raw)


@contextmanager
def _snapshot(ledger, budget):
    """Use the very same stable lock inode as stages, without its write-on-exit."""
    require(money(budget, positive=True) and budget in (100, 1000), "Unsupported retained-hold snapshot allowance")
    ledger = Path(ledger).resolve()
    lock = ledger.with_suffix(ledger.suffix + ".lock")
    require(ledger.is_file() and lock.is_file(), "Existing retained-hold ledger and lock required")
    with lock.open("r") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield _read_snapshot(ledger, budget)


def read_spend(ledger, budget):
    with _snapshot(ledger, budget) as (value, _):
        return stages.summary(value)


def future_binding(repo, run_dir, base_request):
    """Bind the existing request BEFORE adding disposition/fingerprint, avoiding cycles."""
    repo, run_dir = Path(repo).resolve(), Path(run_dir).resolve()
    require(run_dir.is_relative_to(repo / ".context/realism") and run_dir != repo / ".context/realism",
            "Use a distinct private future round")
    require(type(base_request) is dict and "prior_unknown_disposition" not in base_request and "fingerprint" not in base_request,
            "Future binding requires the unextended request")
    return {"run_directory": str(run_dir.relative_to(repo)), "request_sha256": measurement._json_hash(base_request)}


def _proof_chain(repo, references):
    exact(references, ARTIFACTS)
    exact(references["scope_review"], {"path", "sha256"})
    require(references["scope_review"].get("sha256") == SCOPE_SHA256, "Exact original scope digest required")
    scope_path, scope = _artifact(repo, references["scope_review"])
    loaded = {"scope_review": (scope_path, scope)}
    require(references["scope_review"]["sha256"] == SCOPE_SHA256
            and scope.get("protocol_proposal") == VERSION and scope.get("scope_approved") is True
            and scope.get("implementation_scope_supported") is True, "Exact retained-hold scope approval required")
    loaded.update({name: _artifact(repo, ref) for name, ref in references.items() if name != "scope_review"})
    root = scope_path.parent.parent
    proof_names = {
        "terminal_review": "gemini-011-terminal-review.json",
        "accounting_review": "auditor/gemini-011-accounting-final-readback.json",
        "terminal_disposition": "gemini011-terminal-disposition-001.json",
        "exception_bundle": "loop-observer-gemini-011-scan-reconciliation-receipt.json",
        "budget_review": "budget-amendment-100-review-001.json",
    }
    approved_proofs = {item["path"]: item["sha256"] for item in scope["reused_proofs"]}
    for name, relative in proof_names.items():
        require(loaded[name][0] == root / relative and references[name]["sha256"] == approved_proofs[relative],
                "Original retained-hold proof identity differs")
    refresh = scope["termination_assessment"]["supplemental_refresh"]
    require(loaded["terminal_refresh"][0] == root / refresh["path"]
            and references["terminal_refresh"]["sha256"] == refresh["sha256"], "Exact saved terminal refresh required")
    bundle = loaded["exception_bundle"][1]
    original = Path(bundle["run"])
    require(original.is_relative_to(root), "Original retained-hold run differs")
    for name, filename in {"request": "request.json", "start": "execution-started.json", "result": "result.json",
                           "completion": "completion.json", "cleanup": "modal-cleanup.json"}.items():
        path = original / filename
        require(loaded[name][0] == path and references[name]["sha256"] == bundle["source_files"][str(path)],
                "Original sealed retained-hold artifact differs")
    return {name: value for name, (_, value) in loaded.items()}, original


def _original(proofs, references, current, binding):
    scope, request, result = (proofs[name] for name in ("scope_review", "request", "result"))
    fingerprint = request["fingerprint"]
    request_body = {key: value for key, value in request.items() if key != "fingerprint"}
    require(measurement._json_hash(request_body) == fingerprint
            and proofs["start"].get("fingerprint") == fingerprint
            and result.get("fingerprint") == fingerprint
            and proofs["completion"].get("fingerprint") == fingerprint
            and proofs["completion"].get("result_sha256") == measurement._json_hash(result), "Original retained-hold seal differs")
    terminal, accounting = proofs["terminal_review"], proofs["accounting_review"]
    require(terminal.get("approved") is True and terminal.get("request_fingerprint") == fingerprint
            and accounting.get("request_fingerprint") == fingerprint
            and accounting.get("original_result_sha256") == references["result"]["sha256"]
            and accounting.get("original_completion_sha256") == references["completion"]["sha256"]
            and result.get("status") == "reconciliation_required"
            and terminal.get("native_actual_usd") is None and accounting.get("native_actual_usd") is None,
            "Original retained-hold accounting remains unknown")
    stage_id = scope["exact_original_stage"]
    require(stage_id == f"loop-{fingerprint[:16]}-candidate-trials" and binding["request_sha256"] != fingerprint,
            "Retained disposition never authorizes original replay")
    old_stages = result["spend"]["stages"]
    require(stage_id in old_stages and stage_id in current["stages"], "Original stage is missing")
    for name, value in old_stages.items():
        require(name in current["stages"] and measurement._json_hash(value) == measurement._json_hash(current["stages"][name]),
                "An original retained ledger stage changed")
    require(measurement._json_hash(current["budget_amendments"])
            == measurement._json_hash(result["spend"]["budget_amendments"]), "Original budget amendment changed")
    expected = scope["frozen_stage_snapshot"]
    require(measurement._json_hash(current["stages"][stage_id]) == measurement._json_hash(expected)
            and expected["kind"] == "trials" and expected["status"] == "failed"
            and expected["actual_usd"] is None and money(expected["reserve_usd"], positive=True)
            and type(expected.get("finished_at")) is str and bool(expected["finished_at"]),
            "Original full unresolved native hold changed")
    for name, value in current["stages"].items():
        if name != stage_id:
            require(value.get("status") == "complete" and money(value.get("actual_usd"))
                    and money(value.get("reserve_usd"), positive=True) and bool(value.get("finished_at")),
                    "Every other unknown, active or unfinished stage blocks continuation")
    return stage_id, measurement._json_hash(expected)


def _termination(proofs, references):
    mapping = proofs["scope_review"]["termination_assessment"]["owned_terminal_ids"]
    cleanup, refresh = proofs["cleanup"], proofs["terminal_refresh"]
    require(type(cleanup.get("sessions")) is list and type(cleanup.get("terminated")) is list
            and sorted(cleanup["sessions"]) == sorted(mapping.values())
            and sorted(cleanup["terminated"]) == sorted(mapping) and cleanup.get("unresolved") == []
            and not cleanup.get("discovery_errors") and cleanup.get("remote_state") != "unknown"
            and proofs["result"].get("cleanup") == cleanup, "Exact original owned cleanup is incomplete")
    rows = refresh.get("rows")
    require(type(rows) is list and len(rows) == len(mapping)
            and {row.get("sandbox_id") for row in rows} == set(mapping)
            and refresh.get("original_cleanup_sha256") == references["cleanup"]["sha256"]
            and refresh.get("all_owned_and_terminal") is True, "Exact terminal refresh identities differ")
    require(all(row.get("owned") is True and row.get("terminal") is True and row.get("managed") == "true"
                and type(row.get("returncode")) is int and row["returncode"] == 137
                and row.get("session") == mapping[row["sandbox_id"]] for row in rows), "Original owned work is not verified stopped")


def _freeze(repo, review_path, *, ledger, budget, binding, required_reserve, snapshot, execution_fingerprint=None):
    """Read-only factory; call while the controller repository lock is held.

    The same factory and comparison run again at execution before reservations.
    An actual receipt is supplied by the independent reviewer, never generated here.
    """
    repo, ledger = Path(repo).resolve(), Path(ledger).resolve()
    review_path = Path(review_path).absolute()
    require(review_path.is_relative_to(repo / ".context/realism"), "Use a private retained-hold review")
    _path(repo, str(review_path.relative_to(repo)))
    raw = review_path.read_bytes()
    review = _json(raw)
    if review.get("version") == FAILED_SCAN_VERSION:
        return _freeze_known_failed_scans(repo, review_path, review, raw, ledger=ledger, budget=budget,
                                          binding=binding, required_reserve=required_reserve, snapshot=snapshot,
                                          execution_fingerprint=execution_fingerprint)
    if review.get("version") == INTERACTION_VERSION:
        return _freeze_interaction(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                                   required_reserve=required_reserve, snapshot=snapshot,
                                   execution_fingerprint=execution_fingerprint)
    if review.get("version") == RECONCILED_VERSION:
        return _freeze_reconciled(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                                  required_reserve=required_reserve, snapshot=snapshot,
                                  execution_fingerprint=execution_fingerprint)
    if review.get("version") == THREE_VERSION:
        return _freeze_three(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                             required_reserve=required_reserve, snapshot=snapshot,
                             execution_fingerprint=execution_fingerprint)
    if review.get("version") == PAIR_VERSION:
        return _freeze_pair(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                            required_reserve=required_reserve, snapshot=snapshot,
                            execution_fingerprint=execution_fingerprint)
    exact(review, FIELDS | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(review["version"] == VERSION and review["review_kind"] == "retained_native_hold_disposition_review"
            and review["approved"] is True and type(review["reviewer"]) is str and bool(review["reviewer"].strip()),
            "Exact independent retained-hold disposition approval required")
    require(_time(review["reviewed_at"]) is not None, "Retained-hold review time requires a timezone")
    exact(review["acknowledgements"], ACKNOWLEDGEMENTS)
    require(all(value is True for value in review["acknowledgements"].values()), "Retained hold requires explicit limits acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"]), "Invalid future request binding")
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(review["future_binding"] == binding, "Retained-hold review binds a different future request")
    proofs, original = _proof_chain(repo, review["artifacts"])
    future = (repo / binding["run_directory"]).resolve()
    require(not Path(binding["run_directory"]).is_absolute()
            and str(future.relative_to(repo)) == binding["run_directory"]
            and future.is_relative_to(repo / ".context/realism") and not future.is_relative_to(original)
            and not original.is_relative_to(future), "Original run cannot be replayed or modified")
    marker = future / "execution-started.json"
    if marker.exists():
        require(execution_fingerprint is not None and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future paid execution already started; never replay")
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger
            and str(ledger) == proofs["request"]["shared_ledger"]
            and str(ledger) == proofs["budget_review"]["original_ledger"], "Use only the original shared ledger")
    current, ledger_sha = snapshot
    require(money(budget, positive=True) and budget == 100, "Retained hold uses only the existing $100 allowance")
    require(review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Retained-hold ledger review is stale")
    require(bool(current["budget_amendments"]), "Original budget amendment required")
    amendment = current["budget_amendments"][-1]
    require(review["ledger"]["amendment_id"] == amendment["id"] == proofs["budget_review"]["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Retained-hold budget amendment differs")
    stage_id, stage_sha = _original(proofs, review["artifacts"], current, binding)
    exact(review["stage"], {"id", "sha256"})
    require(review["stage"] == {"id": stage_id, "sha256": stage_sha}, "Retained-hold stage identity differs")
    _termination(proofs, review["artifacts"])
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after the full retained hold")
    return {**{key: review[key] for key in FIELDS},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}


def _pair_references(repo, scope_path, scope):
    """Resolve only the two immutable original descriptors from the pinned scope."""
    root = scope_path.parent.parent

    def reference(item):
        exact(item, {"path", "sha256"})
        return {"path": str((root / item["path"]).relative_to(repo)), "sha256": item["sha256"]}

    originals = scope["originals"]
    require(type(originals) is list and len(originals) == 2, "Exactly two scoped original cases required")
    budget = [item for item in scope["reused_proofs"] if item["path"] == "budget-amendment-100-review-001.json"]
    require(len(budget) == 1, "Exact scoped budget proof required")
    return {"scope_review": {"path": str(scope_path.relative_to(repo)), "sha256": PAIR_SCOPE_SHA256},
            "budget_review": reference(budget[0]),
            "originals": [{"label": item["label"],
                           "artifacts": {name: reference(ref) for name, ref in item["artifacts"].items()}}
                          for item in originals]}


def _pair_original(repo, original, references, current, ledger, binding):
    proofs = {name: _artifact(repo, ref)[1] for name, ref in references.items()}
    request, result = proofs["request"], proofs["result"]
    fingerprint = request["fingerprint"]
    require(measurement._json_hash({key: value for key, value in request.items() if key != "fingerprint"}) == fingerprint
            and proofs["start"].get("fingerprint") == fingerprint and result.get("fingerprint") == fingerprint
            and proofs["completion"].get("fingerprint") == fingerprint
            and proofs["completion"].get("result_sha256") == measurement._json_hash(result)
            and result.get("status") == "reconciliation_required", "Original pair seals differ")
    directory = repo / original["run_directory"]
    require(directory.resolve() == directory and directory.is_relative_to(repo / ".context/realism"),
            "Scoped original directory differs")
    for name, filename in {"request": "request.json", "start": "execution-started.json", "result": "result.json",
                           "completion": "completion.json", "cleanup": "modal-cleanup.json"}.items():
        path = directory / filename
        require(repo / references[name]["path"] == path
                and proofs["exception_bundle"]["source_files"].get(str(path)) == references[name]["sha256"],
                "Original pair artifact identity differs")
    require(proofs["exception_bundle"].get("run") == str(directory), "Original pair exception identity differs")
    terminal, accounting = proofs["terminal_review"], proofs["accounting_review"]
    require(terminal.get("approved") is True and terminal.get("request_fingerprint") == fingerprint
            and accounting.get("request_fingerprint") == fingerprint
            and "native_actual_usd" in terminal and terminal["native_actual_usd"] is None
            and "native_actual_usd" in accounting and accounting["native_actual_usd"] is None
            and accounting.get("original_result_sha256") == references["result"]["sha256"]
            and accounting.get("original_completion_sha256") == references["completion"]["sha256"],
            "Original pair accounting remains unknown")
    descriptor, expected = original["stage"], original["frozen_stage_snapshot"]
    exact(descriptor, {"id", "sha256"})
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at"})
    require(descriptor["id"] == f"loop-{fingerprint[:16]}-candidate-trials"
            and descriptor["sha256"] == measurement._json_hash(expected)
            and measurement._json_hash(current["stages"].get(descriptor["id"])) == descriptor["sha256"]
            and expected["kind"] == "trials" and expected["status"] == "failed" and expected["actual_usd"] is None
            and money(expected["reserve_usd"], positive=True)
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["started_at"]) <= _time(expected["finished_at"]), "An original full pair hold changed")
    require(str(ledger) == request["shared_ledger"] and binding["request_sha256"] != fingerprint,
            "Only the original ledger and genuinely new future binding are allowed")
    for name, stage in result["spend"]["stages"].items():
        require(name in current["stages"] and measurement._json_hash(stage)
                == measurement._json_hash(current["stages"][name]), "An original pair ledger stage changed")
    require(measurement._json_hash(current["budget_amendments"])
            == measurement._json_hash(result["spend"]["budget_amendments"]), "Original pair amendment changed")
    if "terminal_refresh" in proofs:
        _termination(proofs, references)
    else:
        launch, cleanup = proofs["launch"], proofs["cleanup"]
        tasks = launch["owned_tasks"]
        require(type(tasks) is list and len(tasks) == 3 and len({task["name"] for task in tasks}) == 3,
                "Exact three scoped original tasks required")
        sessions = sorted(task["name"] + "__env" for task in tasks)
        require(launch["request_fingerprint"] == fingerprint
                and launch["candidate"] == request["candidate"] and launch["runner"] == request["runner"]
                and proofs["cleanup_targets"] == {"sessions": sessions}
                and sorted(cleanup["sessions"]) == sessions and cleanup["terminated"] == []
                and cleanup["unresolved"] == [] and not cleanup.get("discovery_errors")
                and cleanup.get("remote_state") != "unknown" and result.get("cleanup") == cleanup,
                "Exact scoped original no-match cleanup differs")
    return descriptor, directory


def _freeze_pair(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                 snapshot, execution_fingerprint):
    exact(review, PAIR_FIELDS | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(review["review_kind"] == "retained_native_holds_disposition_review" and review["approved"] is True
            and type(review["reviewer"]) is str and bool(review["reviewer"].strip())
            and _time(review["reviewed_at"]) is not None, "Exact independent two-hold approval required")
    exact(review["acknowledgements"], PAIR_ACKNOWLEDGEMENTS)
    require(all(value is True for value in review["acknowledgements"].values()), "Both full holds require explicit acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"])
            and review["future_binding"] == binding, "Two-hold approval binds a different future request")
    exact(review["artifacts"], {"scope_review", "budget_review", "originals"})
    exact(review["artifacts"]["scope_review"], {"path", "sha256"})
    require(review["artifacts"]["scope_review"]["sha256"] == PAIR_SCOPE_SHA256, "Exact two-original scope required")
    scope_path, scope = _artifact(repo, review["artifacts"]["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == PAIR_VERSION, "Exact two-original scope approval required")
    references = _pair_references(repo, scope_path, scope)
    require(measurement._json_hash(review["artifacts"]) == measurement._json_hash(references),
            "Only the two scope-pinned original proof chains are allowed")
    current, ledger_sha = snapshot
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger and money(budget, positive=True) and budget == 100
            and review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Two-hold ledger approval is stale")
    _, budget_proof = _artifact(repo, references["budget_review"])
    require(bool(current["budget_amendments"]), "Original budget amendment required")
    amendment = current["budget_amendments"][-1]
    require(str(ledger) == budget_proof["original_ledger"]
            and review["ledger"]["amendment_id"] == amendment["id"] == budget_proof["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Two-hold amendment differs")
    future = (repo / binding["run_directory"]).resolve()
    require(not Path(binding["run_directory"]).is_absolute() and future != repo / ".context/realism"
            and future.is_relative_to(repo / ".context/realism")
            and str(future.relative_to(repo)) == binding["run_directory"], "Canonical private future directory required")
    descriptors = []
    for original, reference in zip(scope["originals"], references["originals"], strict=True):
        descriptor, directory = _pair_original(repo, original, reference["artifacts"], current, ledger, binding)
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Original pair cannot be replayed or modified")
        descriptors.append(descriptor)
    require(len({item["id"] for item in descriptors}) == 2
            and scope["exact_original_stages"] == [item["id"] for item in descriptors]
            and measurement._json_hash(review["stages"]) == measurement._json_hash(descriptors), "Two exact original stages required")
    old = {item["id"] for item in descriptors}
    for name, stage in current["stages"].items():
        if name not in old:
            require(stage.get("status") == "complete" and money(stage.get("actual_usd"))
                    and _time(stage.get("finished_at")) is not None, "Every third unknown or unfinished stage blocks")
    marker = future / "execution-started.json"
    if marker.exists():
        require(execution_fingerprint is not None and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future pair execution already started; never replay")
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after both full holds")
    return {**{key: review[key] for key in PAIR_FIELDS},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}



def _three_references(repo, scope_path, scope):
    """Only the fixed ordered three cases and budget proof in the pinned v3 scope."""
    root = scope_path.parent.parent

    def reference(item):
        exact(item, {"path", "sha256"})
        return {"path": str((root / item["path"]).relative_to(repo)), "sha256": item["sha256"]}

    originals = scope["originals"]
    require(type(originals) is list and [item["label"] for item in originals] == ["011", "013", "016"],
            "Exactly the ordered three scoped originals required")
    return {"scope_review": {"path": str(scope_path.relative_to(repo)), "sha256": THREE_SCOPE_SHA256},
            "budget_review": reference(scope["budget_review"]),
            "originals": [{"label": item["label"],
                           "artifacts": {name: reference(ref) for name, ref in item["artifacts"].items()}}
                          for item in originals]}


def _three_pins(repo, root, pins):
    """Pins include non-JSON documents; never reinterpret or rewrite their bytes."""
    require(type(pins) is dict and bool(pins), "Three-hold evidence pins required")
    for relative, sha256 in pins.items():
        path = _path(repo, str((root / relative).relative_to(repo)))
        require(type(sha256) is str and re.fullmatch(r"[0-9a-f]{64}", sha256)
                and digest(path.read_bytes()) == sha256, "Three-hold scoped evidence changed")


def _three_budget(repo, scope_path, scope, current, ledger):
    root, chain = scope_path.parent.parent, scope["budget_chain"]

    def load(reference):
        return _artifact(repo, {"path": str((root / reference["path"]).relative_to(repo)),
                               "sha256": reference["sha256"]})[1]

    before, after, proof = (load(chain[name]) for name in ("before", "after", "budget_review"))
    _three_pins(repo, root, scope["pins"])
    _three_pins(repo, root, proof["pins"])
    amendments = chain["entire_amendment_array_exact"]
    require(type(amendments) is list and len(amendments) == 2
            and measurement._json_hash(current["budget_amendments"]) == measurement._json_hash(amendments)
            and measurement._json_hash(before["budget_amendments"]) == measurement._json_hash(amendments[:1])
            and measurement._json_hash(after["budget_amendments"]) == measurement._json_hash(amendments)
            and money(before["budget_usd"]) and before["budget_usd"] == 100
            and money(after["budget_usd"]) and after["budget_usd"] == 1000
            and money(current["budget_usd"]) and current["budget_usd"] == 1000,
            "Exact old100 prefix and approved1000 append required")
    require(proof.get("approved") is True and proof.get("original_ledger") == str(ledger)
            and proof.get("before_ledger_sha256") == chain["before"]["sha256"]
            and proof.get("after_ledger_sha256") == chain["after"]["sha256"]
            and proof.get("amendment_id") == chain["amendment_id"] == amendments[-1]["id"]
            and proof.get("amendment_sha256") == chain["amendment_sha256"] == measurement._json_hash(amendments[-1]),
            "Exact reviewed1000 amendment required")
    restored = deepcopy(after)
    restored["budget_usd"], restored["budget_amendments"] = before["budget_usd"], before["budget_amendments"]
    require(measurement._json_hash(restored) == measurement._json_hash(before)
            and measurement._json_hash(before["stages"]) == chain["all28_stages_sha256"],
            "Budget amendment must preserve the entire baseline")
    for name, stage in after["stages"].items():
        require(measurement._json_hash(current["stages"].get(name)) == measurement._json_hash(stage),
                "A reviewed baseline stage changed")
    prior = deepcopy(before)
    del prior["stages"][scope["exact_original_stages"][2]]
    require(measurement._json_hash(prior) == chain["prior27_whole_ledger_canonical_sha256"]
            and measurement._json_hash(prior["stages"]) == chain["prior27_stage_map_canonical_sha256"],
            "Exact pre-amendment prior ledger required")
    return before, proof


def _three_original016(repo, original, references, current, ledger, binding, prefix):
    """016 is complete/null: its label never establishes a known actual cost."""
    proofs = {name: _artifact(repo, ref)[1] for name, ref in references.items()}
    request, result, bundle = (proofs[name] for name in ("request", "result", "exception_bundle"))
    fingerprint = request["fingerprint"]
    require(measurement._json_hash({key: value for key, value in request.items() if key != "fingerprint"}) == fingerprint
            and proofs["start"].get("fingerprint") == fingerprint and result.get("fingerprint") == fingerprint
            and proofs["completion"].get("fingerprint") == fingerprint
            and proofs["completion"].get("result_sha256") == measurement._json_hash(result)
            and result.get("status") == "reconciliation_required" and result.get("error_type") == "ValueError",
            "Original016 exception seals differ")
    directory = repo / original["run_directory"]
    require(directory.resolve() == directory and directory.is_relative_to(repo / ".context/realism")
            and bundle.get("run") == str(directory) and bundle.get("request_fingerprint") == fingerprint,
            "Original016 directory or sole bundle differs")
    for name, filename in {"request": "request.json", "start": "execution-started.json", "result": "result.json",
                           "completion": "completion.json", "cleanup": "modal-cleanup.json"}.items():
        path = directory / filename
        require(repo / references[name]["path"] == path
                and bundle["source_files"].get(str(path)) == references[name]["sha256"],
                "Original016 sealed artifact identity differs")
    terminal, accounting, harvest = (proofs[name] for name in ("terminal_review", "accounting_review", "harvest"))
    for proof in (terminal, accounting):
        require(proof.get("request_fingerprint") == fingerprint and "native_actual_usd" in proof
                and proof["native_actual_usd"] is None and proof.get("original_execution_session") == harvest.get("original_execution_session")
                and proof.get("original_status_remains") == "reconciliation_required"
                and proof.get("error_type") == "ValueError", "Original016 accounting remains unknown")
    require(terminal.get("approved") is True
            and terminal.get("observer_bundle_sha256") == references["exception_bundle"]["sha256"]
            and accounting.get("terminal_review_sha256") == references["terminal_review"]["sha256"]
            and accounting.get("original_result_sha256") == references["result"]["sha256"]
            and accounting.get("original_completion_sha256") == references["completion"]["sha256"]
            and type(harvest.get("original_execution_session")) is int
            and harvest["original_execution_session"] > 0 and type(harvest.get("exit_code")) is int and harvest["exit_code"] == 1
            and harvest.get("terminal_status") == "reconciliation_required" and harvest.get("error_type") == "ValueError"
            and harvest.get("sole_bundle_sha256") == references["exception_bundle"]["sha256"],
            "Original016 terminal/harvest identities differ")
    descriptor, expected = original["stage"], original["frozen_stage_snapshot"]
    exact(descriptor, {"id", "sha256"})
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at"})
    require(descriptor["id"] == f"loop-{fingerprint[:16]}-candidate-trials"
            and descriptor["sha256"] == measurement._json_hash(expected)
            and measurement._json_hash(current["stages"].get(descriptor["id"])) == descriptor["sha256"]
            and expected["kind"] == "trials" and expected["status"] == "complete" and expected["actual_usd"] is None
            and money(expected["reserve_usd"], positive=True) and expected["reserve_usd"] == 7.762499999999999
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["started_at"]) <= _time(expected["finished_at"]), "Original016 full null hold changed")
    require(str(ledger) == request["shared_ledger"] and binding["request_sha256"] != fingerprint,
            "Only original ledger and genuinely new future request allowed")
    for name, stage in result["spend"]["stages"].items():
        require(measurement._json_hash(current["stages"].get(name)) == measurement._json_hash(stage),
                "An original016 historical stage changed")
    require(measurement._json_hash(result["spend"]["budget_amendments"]) == measurement._json_hash(prefix),
            "Original016 old amendment prefix changed")
    launch, cleanup = proofs["launch"], proofs["cleanup"]
    tasks = launch["owned_tasks"]
    require(type(tasks) is list and len(tasks) == 3 and len({task["name"] for task in tasks}) == 3,
            "Exact three original016 tasks required")
    sessions = sorted(task["name"] + "__env" for task in tasks)
    require(launch.get("request_fingerprint") == fingerprint and launch.get("candidate") == request["candidate"]
            and launch.get("runner") == request["runner"] and proofs["cleanup_targets"] == {"sessions": sessions}
            and sorted(cleanup["sessions"]) == sessions and cleanup.get("terminated") == []
            and cleanup.get("unresolved") == [] and not cleanup.get("discovery_errors")
            and cleanup.get("remote_state") != "unknown" and result.get("cleanup") == cleanup,
            "Original016 owned no-match cleanup differs")
    return descriptor, directory


def _freeze_three(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                  snapshot, execution_fingerprint):
    exact(review, PAIR_FIELDS | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(review["review_kind"] == "retained_native_three_holds_disposition_review" and review["approved"] is True
            and type(review["reviewer"]) is str and bool(review["reviewer"].strip())
            and _time(review["reviewed_at"]) is not None, "Exact independent three-hold approval required")
    exact(review["acknowledgements"], THREE_ACKNOWLEDGEMENTS)
    require(all(value is True for value in review["acknowledgements"].values()), "All three full holds require acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"])
            and review["future_binding"] == binding, "Three-hold approval binds a different future request")
    exact(review["artifacts"], {"scope_review", "budget_review", "originals"})
    exact(review["artifacts"]["scope_review"], {"path", "sha256"})
    require(review["artifacts"]["scope_review"]["sha256"] == THREE_SCOPE_SHA256, "Exact three-original scope required")
    scope_path, scope = _artifact(repo, review["artifacts"]["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == THREE_VERSION, "Exact three-original scope approval required")
    references = _three_references(repo, scope_path, scope)
    require(measurement._json_hash(review["artifacts"]) == measurement._json_hash(references),
            "Only three scope-pinned proof chains allowed")
    current, ledger_sha = snapshot
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger
            and review["ledger"]["path"] == str(ledger.relative_to(repo))
            and money(budget, positive=True) and budget == 1000
            and review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Three-hold ledger approval is stale")
    before, budget_proof = _three_budget(repo, scope_path, scope, current, ledger)
    amendment = current["budget_amendments"][-1]
    require(review["ledger"]["amendment_id"] == amendment["id"] == budget_proof["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Three-hold amendment differs")
    future = repo / binding["run_directory"]
    require(not Path(binding["run_directory"]).is_absolute() and future.resolve() == future
            and future != repo / ".context/realism" and future.is_relative_to(repo / ".context/realism")
            and str(future.relative_to(repo)) == binding["run_directory"], "Canonical private future directory required")
    # Legacy helpers receive the exact historical amendment prefix; their strict
    # failed-stage, seals and termination checks remain unchanged for v1/v2.
    old_current = {**current, "budget_amendments": before["budget_amendments"]}
    descriptors = []
    for original, reference in zip(scope["originals"], references["originals"], strict=True):
        if original["label"] == "016":
            descriptor, directory = _three_original016(repo, original, reference["artifacts"], current, ledger,
                                                        binding, before["budget_amendments"])
        else:
            descriptor, directory = _pair_original(repo, original, reference["artifacts"], old_current, ledger, binding)
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Original three runs cannot be replayed or modified")
        descriptors.append(descriptor)
    require(len({item["id"] for item in descriptors}) == 3
            and scope["exact_original_stages"] == [item["id"] for item in descriptors]
            and measurement._json_hash(review["stages"]) == measurement._json_hash(descriptors), "Three exact ordered stages required")
    originals = {item["id"] for item in descriptors}
    for name, stage in current["stages"].items():
        if name not in originals:
            require(stage.get("status") == "complete" and money(stage.get("actual_usd"))
                    and _time(stage.get("finished_at")) is not None, "Every fourth unknown or unfinished stage blocks")
    marker = future / "execution-started.json"
    require(not marker.is_symlink(), "Noncanonical future start marker")
    if marker.exists():
        require(marker.resolve() == marker and execution_fingerprint is not None
                and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future three-hold execution already started; never replay")
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after all three full holds")
    return {**{key: review[key] for key in PAIR_FIELDS},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}


def freeze_disposition(repo, review_path, *, ledger, budget, binding, required_reserve):
    """Freeze independently supplied approval under repo -> stable ledger lock order.

    No ledger show/write, approval generation, reconciliation or paid work occurs.
    The caller holds the repository measurement lock throughout preparation/execute.
    """
    with _snapshot(ledger, budget) as snapshot:
        return _freeze(repo, review_path, ledger=ledger, budget=budget, binding=binding,
                       required_reserve=required_reserve, snapshot=snapshot)


class ReservationGuard:
    """Validate in reserve's existing lock; permit only this native -> scan transition."""

    def __init__(self, repo, review_path, *, ledger, budget, binding, disposition,
                 fingerprint, trial_reserve, scan_reserve):
        self.repo, self.review_path, self.ledger = repo, review_path, ledger
        self.budget, self.binding, self.disposition = budget, binding, disposition
        self.fingerprint = fingerprint
        self.trial_reserve, self.scan_reserve = trial_reserve, scan_reserve
        self.tag = f"loop-{fingerprint[:16]}-candidate"
        self.baseline = None
        self.native_start = None

    def reserve(self, stage, kind, amount):
        require(money(amount, positive=True), "Noncanonical reservation amount")

        def validate(locked):
            current, raw_sha = _read_snapshot(self.ledger, self.budget)
            require(measurement._json_hash(current) == measurement._json_hash(locked), "Locked ledger differs from its current bytes")
            if stage == self.tag + "-trials":
                require(kind == "trials" and amount == self.trial_reserve and self.baseline is None,
                        "Unexpected or repeated native reservation")
                snapshot = current, raw_sha
                required = self.trial_reserve + self.scan_reserve
            else:
                require(stage == self.tag + "-tells" and kind == "tells" and amount == self.scan_reserve
                        and self.baseline is not None, "Unexpected or unowned scan reservation")
                original, original_sha = self.baseline
                native = current["stages"].get(self.tag + "-trials", {})
                exact(native, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at"})
                require(native["kind"] == "trials" and native["status"] == "complete"
                        and money(native["actual_usd"]) and native["reserve_usd"] == self.trial_reserve
                        and type(native["started_at"]) is str and type(native["finished_at"]) is str
                        and native["started_at"] == self.native_start["started_at"]
                        and bool(native["finished_at"]),
                        "This native outcome is unfinished or unknown; no scan")
                historical = deepcopy(current)
                del historical["stages"][self.tag + "-trials"]
                require(measurement._json_hash(historical) == measurement._json_hash(original), "Historical ledger or foreign stage changed before scan")
                snapshot, required = (original, original_sha), self.trial_reserve + self.scan_reserve
                require(self.scan_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
                        "Scan reserve no longer fits after native actual cost")
            frozen = _freeze(self.repo, self.review_path, ledger=self.ledger, budget=self.budget,
                             binding=self.binding, required_reserve=required, snapshot=snapshot,
                             execution_fingerprint=self.fingerprint)
            require(frozen == self.disposition, "Retained-hold disposition changed before reservation")
            if self.baseline is None:
                self.baseline = deepcopy(current), raw_sha

        result = stages.reserve(self.ledger, self.budget, stage, kind, amount, validate=validate)
        if stage == self.tag + "-trials":
            self.native_start = deepcopy(result["stages"][stage])
        return result


def _reconciled_nonattempt(repo, descriptor, current, ledger, binding):
    """Validate the separately scoped failed-zero case against the real image."""
    exact(descriptor, {"scope_review", "stage", "run_directory", "request_fingerprint", "artifacts"})
    exact(descriptor["scope_review"], {"path", "sha256"})
    require(descriptor["scope_review"]["sha256"] == RECONCILED_SCOPE_SHA256,
            "Exact reconciled nonattempt scope required")
    _, scope = _artifact(repo, descriptor["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == RECONCILED_VERSION,
            "Reconciled nonattempt implementation scope differs")
    expected = {**scope["expected_reconciled_nonattempt"], "scope_review": descriptor["scope_review"]}
    require(measurement._json_hash(descriptor) == measurement._json_hash(expected),
            "Only the entire scope-pinned reconciled nonattempt is eligible")
    references = descriptor["artifacts"]
    names = {"request", "start", "result", "state", "completion", "trace", "raw_result", "launch_intent",
             "process", "cleanup", "native_accounting", "harvest", "callback", "terminal_review",
             "terminal_proof", "financial_gate", "financial_proof", "financial_proposal", "finish_script",
             "finish_intent", "before", "after", "finish_receipt", "final_accounting"}
    exact(references, names)
    proofs = {}
    for name, reference in references.items():
        exact(reference, {"path", "sha256"})
        require(type(reference["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]),
                "Invalid reconciled evidence digest")
        raw = _path(repo, reference["path"]).read_bytes()
        require(digest(raw) == reference["sha256"], "Reconciled evidence bytes changed")
        if name not in {"trace", "finish_script"}:
            proofs[name] = _json(raw)
    original = repo / descriptor["run_directory"]
    future = repo / binding["run_directory"]
    require(original.resolve() == original and original.is_relative_to(repo / ".context/realism")
            and not future.is_relative_to(original) and not original.is_relative_to(future),
            "Reconciled original cannot be replayed or overlap the future")
    for name, filename in {"request": "request.json", "start": "execution-started.json", "result": "result.json",
                           "state": "state.json", "completion": "completion.json", "cleanup": "modal-cleanup.json"}.items():
        require(repo / references[name]["path"] == original / filename, "Reconciled original artifact path differs")
    request, result, completion = (proofs[name] for name in ("request", "result", "completion"))
    fingerprint = descriptor["request_fingerprint"]
    require(request.get("fingerprint") == fingerprint
            == measurement._json_hash({key: value for key, value in request.items() if key != "fingerprint"})
            and proofs["start"].get("fingerprint") == result.get("fingerprint") == completion.get("fingerprint") == fingerprint
            and completion.get("result_sha256") == measurement._json_hash(result)
            and measurement._json_hash(proofs["raw_result"]) == measurement._json_hash(result)
            and request.get("shared_ledger") == str(ledger), "Reconciled original request or seals differ")
    require(request.get("mode") == "candidate_only" and request.get("baseline_evidence") is None
            and request.get("historical_reference") is None and result.get("historical_reference") is None
            and result.get("comparison") is None and result.get("status") == "reconciliation_required"
            and result.get("error_type") == "ValueError" and proofs["state"].get("status") == "reconciliation_required"
            and result.get("cleanup") == proofs["cleanup"]
            and proofs["native_accounting"].get("status") == "unknown"
            and proofs["native_accounting"].get("logs") == [], "Reconciled failure must retain its unknown native evidence")
    terminal, harvest, callback = (proofs[name] for name in ("terminal_review", "harvest", "callback"))
    session = harvest.get("original_execution_session")
    require(type(session) is int and session > 0 and harvest.get("closed") is True
            and type(harvest.get("exit_code")) is int and harvest["exit_code"] == 1
            and type(proofs["process"].get("returncode")) is int and proofs["process"]["returncode"] == 1,
            "Reconciled original must have its sole failed terminal harvest")
    for name in ("terminal_review", "terminal_proof", "callback", "process"):
        require(proofs[name].get("request_fingerprint") == fingerprint, "Reconciled terminal fingerprint differs")
    for name in ("terminal_review", "terminal_proof", "callback"):
        require(proofs[name].get("original_execution_session") == session, "Reconciled original session differs")
    require(terminal.get("approved") is True and terminal.get("native_model_nonattempt_supported") is True
            and terminal.get("native_job_run_reached") is False and terminal.get("owned_trial_sandbox_launch_reachable") is False
            and terminal.get("complete_native_samples") == 0 and terminal.get("strict_scan_coverage_accepted") is False
            and terminal.get("comparison_accepted") is False and terminal.get("comparison") is None,
            "Independent native model nonattempt proof required")

    def session_ref(name):
        root = original.parent
        return {"path": str((repo / references[name]["path"]).relative_to(root)), "sha256": references[name]["sha256"]}

    require(terminal.get("proof") == session_ref("terminal_proof")
            and terminal.get("original_harvest") == session_ref("harvest")
            and terminal.get("observer_exception_callback") == session_ref("callback")
            and harvest.get("after_observer_callback") == session_ref("callback")
            and harvest.get("process_receipt") == session_ref("process")
            and harvest.get("result") == session_ref("result") and harvest.get("completion") == session_ref("completion")
            and proofs["process"].get("stdout_sha256") == references["raw_result"]["sha256"],
            "Reconciled terminal evidence joins differ")
    intent = proofs["launch_intent"]
    earliest, deadline = (_time(intent.get(key)) for key in ("launch_intent_at", "fixed_observer_deadline"))
    require(earliest is not None and deadline is not None and (deadline - earliest).total_seconds() == 8765
            and intent.get("request_fingerprint") == fingerprint
            and callback.get("earliest_launch_intent") == intent["launch_intent_at"]
            and callback.get("fixed_observer_deadline") == intent["fixed_observer_deadline"]
            and callback.get("deadline_reset") is False and callback.get("normal_watch_started") is False
            and callback.get("normal_terminal_collector_invocations") == 0,
            "Reconciled original immutable deadline or missing-launch boundary differs")
    stage = descriptor["stage"]
    exact(stage, {"id", "sha256", "snapshot"})
    target, snapshot = stage["id"], stage["snapshot"]
    exact(snapshot, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"})
    require(target == f"loop-{fingerprint[:16]}-candidate-trials"
            and snapshot["kind"] == "trials" and snapshot["status"] == "failed"
            and money(snapshot["actual_usd"]) and snapshot["actual_usd"] == 0
            and money(snapshot["reserve_usd"], positive=True)
            and _time(snapshot["started_at"]) is not None and _time(snapshot["finished_at"]) is not None
            and _time(snapshot["finished_at"]) >= _time(snapshot["started_at"])
            and stage["sha256"] == measurement._json_hash(snapshot)
            == measurement._json_hash(current["stages"].get(target)), "Exact finite failed-zero stage required")
    before, before_sha = _read_snapshot(repo / references["before"]["path"], 1000)
    after, after_sha = _read_snapshot(repo / references["after"]["path"], 1000)
    old = before["stages"].get(target, {})
    restored = deepcopy(after)
    restored["stages"][target] = old
    require(old.get("status") == "failed" and old.get("actual_usd") is None
            and old.get("kind") == snapshot["kind"] and old.get("reserve_usd") == snapshot["reserve_usd"]
            and old.get("started_at") == snapshot["started_at"] and _time(old.get("finished_at")) is not None
            and _time(old["finished_at"]) <= _time(snapshot["finished_at"])
            and measurement._json_hash(restored) == measurement._json_hash(before)
            and measurement._json_hash(after["stages"].get(target)) == stage["sha256"],
            "Reconciled financial write changed unrelated history or native outcome")
    for name, value in after.items():
        if name != "stages":
            require(measurement._json_hash({name: current.get(name)}) == measurement._json_hash({name: value}),
                    "Reconciled unrelated ledger fields changed")
    for name, value in after["stages"].items():
        require(measurement._json_hash(current["stages"].get(name)) == measurement._json_hash(value),
                "Reconciled entire finalized historical ledger changed")
    gate, receipt, accounting = (proofs[name] for name in ("financial_gate", "finish_receipt", "final_accounting"))
    for name in ("financial_gate", "finish_intent", "finish_receipt"):
        record = proofs[name]
        require(record.get("stage_id") == target and money(record.get("actual_usd")) and record["actual_usd"] == 0
                and record.get("status") == "failed" and record.get("accounting_only") is True,
                "Reconciled financial evidence must preserve failed-zero accounting")
    require(gate.get("approved") is True and gate.get("one_normal_native_finish_approved") is True
            and gate.get("request_fingerprint") == receipt.get("request_fingerprint") == fingerprint
            and gate.get("script_sha256") == references["finish_script"]["sha256"]
            and gate.get("proposal_sha256") == references["financial_proposal"]["sha256"]
            and gate.get("proof") == session_ref("financial_proof")
            and gate.get("ledger_sha256") == before_sha and gate.get("detail") == snapshot["detail"],
            "Exact independent financial gate and detail required")
    normalized = deepcopy(after)
    normalization = gate.get("postimage_normalization", {})
    require(normalization.get("target") == target and normalization.get("only_field") == "finished_at"
            and type(normalization.get("placeholder")) is str, "Exact target-only postimage normalization required")
    normalized["stages"][target]["finished_at"] = normalization["placeholder"]
    require(measurement._json_hash(normalized) == gate.get("normalized_postimage_sha256")
            == receipt.get("normalized_postimage_sha256") == accounting.get("normalized_postimage_sha256"),
            "Reconciled entire normalized postimage differs")
    require(proofs["finish_intent"].get("gate_sha256") == receipt.get("gate_sha256") == references["financial_gate"]["sha256"]
            and proofs["finish_intent"].get("proposal_sha256") == receipt.get("proposal_sha256") == references["financial_proposal"]["sha256"]
            and proofs["finish_intent"].get("ledger_sha256") == receipt.get("before_ledger_sha256") == before_sha
            and receipt.get("after_ledger_sha256") == after_sha and type(receipt.get("normal_finish_calls")) is int
            and receipt["normal_finish_calls"] == 1 and receipt.get("original_execution_session") == session,
            "Reconciled sole financial write identity differs")
    require(accounting.get("approved") is True and accounting.get("financial_reconciliation_complete") is True
            and accounting.get("accounting_only") is True and accounting.get("request_fingerprint") == fingerprint
            and accounting.get("original_execution_session") == session and accounting.get("ledger_sha256") == after_sha
            and accounting.get("gate") == session_ref("financial_gate")
            and accounting.get("primary_finish_receipt") == session_ref("finish_receipt")
            and measurement._json_hash(accounting.get("current_stage_snapshot")) == measurement._json_hash(after)
            and accounting.get("original036_native_status") == "failed"
            and money(accounting.get("original036_native_actual_usd")) and accounting["original036_native_actual_usd"] == 0
            and accounting.get("original_sealed_status_preserved") == "reconciliation_required"
            and accounting.get("strict_scan_coverage_accepted") is False and accounting.get("comparison_accepted") is False,
            "Independent finalized accounting readback required")
    return target

def _freeze_reconciled(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                  snapshot, execution_fingerprint, retained_interaction=None, reconciled_failed_scans=None):
    # Only the v5 handler supplies its already validated single descriptor. The
    # ledger image is never filtered, rewritten or represented as a v4 snapshot.
    fields = RECONCILED_FIELDS if retained_interaction is None else INTERACTION_FIELDS
    kind = ("retained_native_three_holds_reconciled_nonattempt_disposition_review" if retained_interaction is None
            else "retained_four_holds_continuation_disposition")
    acknowledgements = THREE_ACKNOWLEDGEMENTS if retained_interaction is None else INTERACTION_ACKNOWLEDGEMENTS
    if reconciled_failed_scans is not None:
        require(retained_interaction is not None, "Known scans cannot extend the three-hold protocol")
        fields, kind, acknowledgements = (FAILED_SCAN_FIELDS, "retained_four_holds_known_failed_scans_disposition",
                                          FAILED_SCAN_ACKNOWLEDGEMENTS)
    exact(review, fields | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    require(review["review_kind"] == kind and review["approved"] is True
            and type(review["reviewer"]) is str and bool(review["reviewer"].strip())
            and _time(review["reviewed_at"]) is not None,
            "Exact independent three-hold approval required" if retained_interaction is None
            else "Exact bounded continuation declaration required")
    exact(review["acknowledgements"], acknowledgements)
    require(all(value is True for value in review["acknowledgements"].values()),
            "All three full holds require acknowledgement" if retained_interaction is None
            else "All four full holds require acknowledgement")
    require(review["validator_sha256"] == digest(Path(__file__).read_bytes()), "Retained-hold validator source changed")
    exact(binding, {"run_directory", "request_sha256"})
    exact(review["future_binding"], {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str and type(binding["request_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", binding["request_sha256"])
            and review["future_binding"] == binding, "Three-hold approval binds a different future request")
    exact(review["artifacts"], {"scope_review", "budget_review", "originals"})
    exact(review["artifacts"]["scope_review"], {"path", "sha256"})
    require(review["artifacts"]["scope_review"]["sha256"] == THREE_SCOPE_SHA256, "Exact three-original scope required")
    scope_path, scope = _artifact(repo, review["artifacts"]["scope_review"])
    require(scope.get("scope_approved") is True and scope.get("implementation_scope_supported") is True
            and scope.get("protocol_proposal") == THREE_VERSION, "Exact three-original scope approval required")
    references = _three_references(repo, scope_path, scope)
    require(measurement._json_hash(review["artifacts"]) == measurement._json_hash(references),
            "Only three scope-pinned proof chains allowed")
    current, ledger_sha = snapshot
    exact(review["ledger"], {"path", "sha256", "budget_usd", "amendment_id", "amendment_sha256"})
    require(_path(repo, review["ledger"]["path"]) == ledger
            and review["ledger"]["path"] == str(ledger.relative_to(repo))
            and money(budget, positive=True) and budget == 1000
            and review["ledger"]["sha256"] == ledger_sha and money(review["ledger"]["budget_usd"])
            and review["ledger"]["budget_usd"] == budget, "Three-hold ledger approval is stale")
    before, budget_proof = _three_budget(repo, scope_path, scope, current, ledger)
    amendment = current["budget_amendments"][-1]
    require(review["ledger"]["amendment_id"] == amendment["id"] == budget_proof["amendment_id"]
            and review["ledger"]["amendment_sha256"] == measurement._json_hash(amendment), "Three-hold amendment differs")
    future = repo / binding["run_directory"]
    require(not Path(binding["run_directory"]).is_absolute() and future.resolve() == future
            and future != repo / ".context/realism" and future.is_relative_to(repo / ".context/realism")
            and str(future.relative_to(repo)) == binding["run_directory"], "Canonical private future directory required")
    # Legacy helpers receive the exact historical amendment prefix; their strict
    # failed-stage, seals and termination checks remain unchanged for v1/v2.
    old_current = {**current, "budget_amendments": before["budget_amendments"]}
    descriptors = []
    for original, reference in zip(scope["originals"], references["originals"], strict=True):
        if original["label"] == "016":
            descriptor, directory = _three_original016(repo, original, reference["artifacts"], current, ledger,
                                                        binding, before["budget_amendments"])
        else:
            descriptor, directory = _pair_original(repo, original, reference["artifacts"], old_current, ledger, binding)
        require(not future.is_relative_to(directory) and not directory.is_relative_to(future),
                "Original three runs cannot be replayed or modified")
        descriptors.append(descriptor)
    expected_descriptors = descriptors if retained_interaction is None else [*descriptors, retained_interaction]
    require(len({item["id"] for item in descriptors}) == 3
            and scope["exact_original_stages"] == [item["id"] for item in descriptors]
            and measurement._json_hash(review["stages"]) == measurement._json_hash(expected_descriptors),
            "Three exact ordered stages required" if retained_interaction is None
            else "Three exact ordered native stages and only the scoped interaction required")
    originals = {item["id"] for item in descriptors}
    reconciled = _reconciled_nonattempt(repo, review["reconciled_nonattempt"], current, ledger, binding)
    require(reconciled not in originals, "Reconciled case cannot replace an original full hold")
    originals.add(reconciled)
    if retained_interaction is not None:
        require(retained_interaction["id"] not in originals, "Retained interaction cannot replace a native case")
        originals.add(retained_interaction["id"])
    if reconciled_failed_scans is not None:
        for known_scan in reconciled_failed_scans:
            require(known_scan["id"] not in originals, "Known scan cannot replace an original hold/nonattempt")
            originals.add(known_scan["id"])
    for name, stage in current["stages"].items():
        if name not in originals:
            require(stage.get("status") == "complete" and money(stage.get("actual_usd"))
                    and _time(stage.get("finished_at")) is not None,
                    "Every fourth unknown or unfinished stage blocks" if retained_interaction is None
                    else "Every fifth unknown or unfinished stage blocks")
    marker = future / "execution-started.json"
    require(not marker.is_symlink(), "Noncanonical future start marker")
    if marker.exists():
        require(marker.resolve() == marker and execution_fingerprint is not None
                and _json(marker.read_bytes()).get("fingerprint") == execution_fingerprint,
                "Future three-hold execution already started; never replay")
    require(money(required_reserve, positive=True) and required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "New native and scan reserves do not fit after all three full holds" if retained_interaction is None
            else "New native and scan reserves do not fit after all four full holds")
    return {**{key: review[key] for key in fields},
            "review": {"path": str(review_path.relative_to(repo)), "sha256": digest(raw)}}


def _freeze_interaction(repo, review_path, review, raw, *, ledger, budget, binding, required_reserve,
                        snapshot, execution_fingerprint, reconciled_failed_scans=None):
    """New continuation authority for one pinned unknown; never reconcile it.

    A coordinator declaration cites the owner's restart-after-integration request
    and binds all substantive disposition fields, including the whole ledger SHA,
    validator and exact future request. Its full-hold acknowledgements are the
    coordinator's interpretation, never fabricated owner billing instructions.
    Original denied accounting remains uncertainty, not continuation authority.
    """
    fields = INTERACTION_FIELDS if reconciled_failed_scans is None else FAILED_SCAN_FIELDS
    exact(review, fields | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    exact(binding, {"run_directory", "request_sha256"})
    require(type(binding["run_directory"]) is str, "Invalid continuation future directory")
    descriptor = review["retained_interaction"]
    exact(descriptor, {"stage", "original_request", "failure_accounting"})
    stage = descriptor["stage"]
    exact(stage, {"id", "sha256", "snapshot"})
    require(type(stage["id"]) is str and bool(stage["id"]), "Exact retained interaction identity required")
    expected = stage["snapshot"]
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"})
    require(expected["kind"] == "interaction" and expected["status"] == "unknown"
            and expected["actual_usd"] is None and money(expected["reserve_usd"], positive=True)
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["finished_at"]) >= _time(expected["started_at"])
            and type(expected["detail"]) is str, "Only a finished terminal unknown full interaction hold is eligible")
    current, _ = snapshot
    require(stage["sha256"] == measurement._json_hash(expected)
            == measurement._json_hash(current["stages"].get(stage["id"])), "Retained interaction stage changed")
    for name, sha256 in (("original_request", INTERACTION_REQUEST_SHA256),
                         ("failure_accounting", INTERACTION_ACCOUNTING_SHA256)):
        exact(descriptor[name], {"path", "sha256"})
        require(descriptor[name]["sha256"] == sha256, "Only the pinned original interaction evidence is eligible")
    request_path, request = _artifact(repo, descriptor["original_request"])
    accounting_path, accounting = _artifact(repo, descriptor["failure_accounting"])
    require(request_path.parent.name == "model-calls" and request_path.name == stage["id"] + "-request.json",
            "Exact original interaction request artifact required")
    require(accounting.get("review_kind") == "independent_original_http400_accounting_recovery"
            and accounting.get("approved") is False and accounting.get("outcome") == "unresolved_original_charge"
            and accounting.get("stage_id") == stage["id"]
            and accounting.get("original_request_sha256") == descriptor["original_request"]["sha256"]
            and accounting.get("ledger_path") == str(ledger.relative_to(repo))
            and accounting.get("ledger_before_sha256") == accounting.get("ledger_after_sha256")
            and "exact_actual_cost_usd" in accounting and accounting["exact_actual_cost_usd"] is None
            and accounting.get("one_time_normal_stages_finish_justified") is False
            and "supported_stage_update" in accounting and accounting["supported_stage_update"] is None
            and accounting.get("inference_retry_permitted") is False
            and accounting.get("old036_failed_zero_preserved") is True
            and money(request.get("reserve_usd"), positive=True)
            and request["reserve_usd"] == expected["reserve_usd"], "Original interaction accounting must remain unresolved")
    descriptors = review["stages"]
    require(type(descriptors) is list and len(descriptors) == 4, "Exactly three native holds and one interaction required")
    for item in descriptors:
        exact(item, {"id", "sha256"})
        require(type(item["id"]) is str, "Invalid retained-hold stage identity")
    require(descriptors[-1] == {"id": stage["id"], "sha256": stage["sha256"]}
            and len({item["id"] for item in descriptors}) == 4, "Interaction must be the sole fourth retained hold")
    originals = accounting.get("all_four_unknown_stage_snapshots")
    exact(originals, {item["id"] for item in descriptors})
    require(all(measurement._json_hash(originals[item["id"]]) == item["sha256"] for item in descriptors),
            "All four original accounting snapshots must remain exact")
    future = repo / binding["run_directory"]
    require(all(not future.is_relative_to(directory) and not directory.is_relative_to(future)
                for directory in (request_path.parent.parent, accounting_path.parent)),
            "Original interaction evidence cannot overlap new work")
    _, authorization = _artifact(repo, review["restart_authorization"])
    scope_fields = INTERACTION_SCOPE_FIELDS if reconciled_failed_scans is None else FAILED_SCAN_SCOPE_FIELDS
    acknowledgements = INTERACTION_ACKNOWLEDGEMENTS if reconciled_failed_scans is None else FAILED_SCAN_ACKNOWLEDGEMENTS
    owner_pin = INTERACTION_OWNER_REQUEST_SHA256 if reconciled_failed_scans is None else FAILED_SCAN_OWNER_REQUEST_SHA256
    version = INTERACTION_VERSION if reconciled_failed_scans is None else FAILED_SCAN_VERSION
    kind = ("coordinator_bounded_continuation_interpretation" if reconciled_failed_scans is None
            else "coordinator_known_failed_scans_continuation_interpretation")
    exact(authorization, scope_fields)
    exact(authorization["acknowledgements"], acknowledgements)
    exact(authorization["owner_request"], {"path", "sha256"})
    require(authorization["owner_request"]["sha256"] == owner_pin,
            "Exact original owner restart request required")
    _, owner_request = _artifact(repo, authorization["owner_request"])
    resume_authorized = (owner_request.get("restart_authorized_after_integration") is True
                         if reconciled_failed_scans is None else owner_request.get("resume_authorized") is True
                         and owner_request.get("model_switch_authorized") is True
                         and owner_request.get("no_unsubstantiated_hold_release") is True
                         and owner_request.get("ledger") == str(ledger))
    if reconciled_failed_scans is not None:
        exact(authorization["original_v5_scope"], {"path", "sha256"})
        require(authorization["original_v5_scope"]["sha256"] == FAILED_SCAN_V5_SCOPE_SHA256,
                "Exact original v5 declaration is provenance only; never extend its authority")
        _, old_scope = _artifact(repo, authorization["original_v5_scope"])
        exact(old_scope, INTERACTION_SCOPE_FIELDS)
        exact(old_scope["acknowledgements"], INTERACTION_ACKNOWLEDGEMENTS)
        require(old_scope["version"] == INTERACTION_VERSION
                and old_scope["declaration_kind"] == "coordinator_bounded_continuation_interpretation"
                and old_scope["owner_request"]["sha256"] == INTERACTION_OWNER_REQUEST_SHA256
                and all(value is True for value in old_scope["acknowledgements"].values()),
                "Original v5 provenance differs")
        _artifact(repo, old_scope["owner_request"])
        require(_time(authorization["declared_at"]) is not None
                and all(_time(authorization["declared_at"]) >= _time(item["snapshot"]["finished_at"])
                        for item in reconciled_failed_scans), "Declaration must follow both accounting completions")
    require(resume_authorized
            and money(owner_request.get("budget_total_usd"), positive=True)
            and owner_request["budget_total_usd"] == 1000
            and _time(owner_request.get("at")) is not None, "Owner request must preserve the existing restart scope and cap")
    bounded = {key: review[key] for key in fields - {"restart_authorization"}}
    require(authorization["version"] == version
            and authorization["declaration_kind"] == kind
            and type(authorization["declared_by"]) is str and bool(authorization["declared_by"].strip())
            and _time(authorization["declared_at"]) is not None
            and _time(authorization["declared_at"]) >= _time(owner_request["at"])
            and _time(authorization["declared_at"]) >= _time(expected["finished_at"])
            and authorization["continuation_after_feedback_integration"] is True
            and money(authorization["budget_usd"], positive=True) and authorization["budget_usd"] == budget == 1000
            and authorization["ledger"] == review["ledger"]
            and authorization["future_binding"] == binding == review["future_binding"]
            and authorization["bounded_disposition_sha256"] == measurement._json_hash(bounded)
            and all(value is True for value in authorization["acknowledgements"].values()),
            "New exact owner continuation-after-integration authorization required")
    return _freeze_reconciled(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                              required_reserve=required_reserve, snapshot=snapshot,
                              execution_fingerprint=execution_fingerprint, retained_interaction=descriptors[-1],
                              reconciled_failed_scans=reconciled_failed_scans)

def _reconciled_failed_scan(repo, descriptor, current, ledger, binding):
    """Validate one explicitly pinned finite FAILED scan; never authorize unknown cost."""
    exact(descriptor, {"stage", "proof", "receipt"})
    stage = descriptor["stage"]
    exact(stage, {"id", "sha256", "snapshot"})
    require(type(stage["id"]) is str and stage["id"] in FAILED_SCAN_PINS,
            "Only the two exact historically reconciled scans are eligible")
    pin = FAILED_SCAN_PINS[stage["id"]]
    expected = stage["snapshot"]
    exact(expected, {"kind", "status", "reserve_usd", "actual_usd", "started_at", "finished_at", "detail"})
    require(stage["sha256"] == pin["snapshot_sha256"] == measurement._json_hash(expected)
            == measurement._json_hash(current["stages"].get(stage["id"]))
            and expected["kind"] == "tells" and expected["status"] == "failed"
            and money(expected["actual_usd"], positive=True)
            and Decimal(str(expected["actual_usd"])) == Decimal(pin["actual_usd_decimal"])
            and money(expected["reserve_usd"], positive=True) and expected["reserve_usd"] == 15.75
            and _time(expected["started_at"]) is not None and _time(expected["finished_at"]) is not None
            and _time(expected["finished_at"]) >= _time(expected["started_at"]),
            "Exact finite known-cost FAILED scan snapshot required")
    for name in ("proof", "receipt"):
        exact(descriptor[name], {"path", "sha256"})
        require(descriptor[name] == pin[name], "Original failed-scan proof/receipt identity differs")
    proof_path, proof = _artifact(repo, descriptor["proof"])
    receipt_path, receipt = _artifact(repo, descriptor["receipt"])
    require(proof.get("target_stage") == receipt.get("target_stage") == stage["id"]
            and proof.get("normal_finish_status") == "failed" and proof.get("accounting_only") is True
            and proof.get("strict_scan_coverage_accepted") is False
            and proof.get("semantic_measurement_accepted") is False
            and receipt.get("status") == "accounting_reconciled_scan_still_failed"
            and type(receipt.get("normal_finish_calls")) is int and receipt["normal_finish_calls"] == 1
            and receipt.get("proof_sha256") == pin["proof"]["sha256"]
            and receipt.get("actual_usd") == expected["actual_usd"]
            and receipt.get("target_snapshot_after") == expected
            and receipt.get("only_target_accounting_fields_changed") is True
            and receipt.get("all_other_stages_and_old_seals_preserved") is True
            and receipt.get("strict_scan_coverage_accepted") is False,
            "Accounting closure cannot establish scan coverage or semantic success")
    fields = proof.get("normal_finish_fields", {})
    require(fields.get("path") == str(ledger) and fields.get("budget") == current.get("budget_usd") == 1000
            and fields.get("stage") == stage["id"] and fields.get("status") == "failed"
            and fields.get("actual") == expected["actual_usd"] and fields.get("detail") == expected["detail"],
            "Original ordinary normal-finish fields differ")
    preimage = proof["ledger_preimage"]
    _, before = _artifact(repo, preimage["saved_copy"])
    require(preimage["original_path"] == str(ledger)
            and preimage["raw_sha256"] == preimage["saved_copy"]["sha256"] == receipt["before_sha256"]
            and receipt["ledger_identity_after"]["path"] == str(ledger)
            and receipt["ledger_identity_after"]["sha256"] == receipt["after_sha256"],
            "Original whole-ledger accounting chain differs")
    predicted = proof["expected_whole_postimage"]
    _, normalized = _artifact(repo, {"path": predicted["path"], "sha256": predicted["raw_file_sha256"]})
    normalization = predicted["normalization"]
    require(normalization["target_stage"] == stage["id"] and normalization["only_field"] == "finished_at"
            and type(normalization["placeholder"]) is str, "Exact target-only timestamp normalization required")
    old = before["stages"].get(stage["id"], {})
    require(old.get("kind") == "tells" and old.get("status") == "failed" and old.get("actual_usd") is None
            and old.get("reserve_usd") == expected["reserve_usd"]
            and old.get("started_at") == expected["started_at"], "Original scan full-hold preimage differs")
    computed = deepcopy(before)
    computed["stages"][stage["id"]] = {**expected, "finished_at": normalization["placeholder"]}
    require(computed == normalized and measurement._json_hash(normalized)
            == predicted["normalized_semantic_sha256"] == receipt["normalized_whole_postimage_sha256"],
            "Entire original accounting postimage differs")
    computed["stages"][stage["id"]] = expected
    require(digest((json.dumps(computed, indent=2, allow_nan=False) + "\n").encode()) == receipt["after_sha256"],
            "Exact original ordinary writer postimage bytes differ")
    require(all(current.get(key) == value for key, value in before.items() if key != "stages")
            and all(current["stages"].get(name) == value for name, value in before["stages"].items()
                    if name != stage["id"]), "Historical preimage entries/top-level values changed")
    exact(proof["attempt_closure"], {"path", "sha256"})
    require(proof["attempt_closure"] == pin["closure"], "Exact all-attempt raw charge closure required")
    _, closure = _artifact(repo, proof["attempt_closure"])
    attempts = closure.get("attempts")
    require(type(attempts) is list and len(attempts) == closure.get("attempted_parts") == 3
            and closure.get("planned_parts") == pin["planned_parts"]
            and closure.get("exact_total_usd_decimal") == pin["actual_usd_decimal"]
            and measurement._json_hash(attempts) == pin["raw_charge_closure_sha256"],
            "All exact original attempted charges must close")
    checks = closure["all_attempted_charge_closure"]
    require(checks.get("every_attempt_retains_original_response_id_and_finite_cost") is True
            and checks.get("no_inferred_zero_or_missing_response") is True
            and checks.get("frozen_source_verified") is True
            and checks.get("no_retries") == {"sdk": 0, "model": 0, "refusals": 0, "answer_attempts": 1},
            "All original attempts require finite no-retry closure")
    charges, ids, tokens, refusals = [], set(), 0, 0
    for attempt in attempts:
        claude = pin["closure_kind"] == "claude_refusal"
        identifier = attempt.get("response_generation_id" if claude else "genuine_response_generation_id")
        usage = attempt.get("raw_usage", {})
        require(type(identifier) is str and bool(identifier) and identifier not in ids
                and money(usage.get("cost"), positive=True)
                and attempt.get("exact_frozen_prompt_match" if claude else "request_matches_exact_only_planned_part") is True,
                "Missing, duplicated, unknown or nonfinite original charge")
        ids.add(identifier)
        charge = Decimal(str(usage["cost"]))
        require(charge == Decimal(attempt["actual_usd_decimal" if claude else "exact_cost_usd_decimal"]),
                "Saved raw cost differs from charge closure")
        charges.append(charge)
        require(type(usage.get("total_tokens")) is int and usage["total_tokens"] > 0,
                "Original token closure missing")
        tokens += usage["total_tokens"]
        if claude:
            refusal = attempt["refusal_metadata"]
            require(refusal["sdk_max_retries"] == refusal["model_config_max_retries"]
                    == refusal["refusal_retry_count_allowed"] == 0, "Original retry policy differs")
            refusals += refusal["is_refusal"] is True
        else:
            require(attempt["finish_reason"] == attempt["native_finish_reason"] == "length"
                    and attempt["response_content"] is None and attempt["response_tool_calls"] is None
                    and attempt["response_refusal"] is None
                    and usage["completion_tokens"] == usage["completion_tokens_details"]["reasoning_tokens"] == 8192
                    and attempt["event_config"]["max_retries"] == 0,
                    "Exact original reasoning-exhaustion failure metadata differs")
    require(sum(charges, Decimal(0)) == Decimal(pin["actual_usd_decimal"])
            and tokens == pin["total_tokens"], "Original all-attempt charge/token closure differs")
    if pin["closure_kind"] == "claude_refusal":
        require(refusals == 2 and len(closure.get("not_attempted_after_refusal", [])) == 1
                and closure.get("total_tokens") == tokens,
                "Original Claude sequential refusal/unattempted closure differs")
        parquet = closure["original_scan_parquet"]
    else:
        require(closure.get("unattempted_parts") == 0 and closure.get("usable_results") == 0
                and closure.get("raw_model_events") == closure.get("unique_response_generation_ids") == 3
                and closure["token_totals"]["total"] == tokens
                and closure["token_totals"]["reasoning"] == 24576,
                "Original DeepSeek three-planned/three-attempted closure differs")
        parquet = closure["parquet"]
    _, preserved = _artifact(repo, proof["preservation"]["inventory"])
    for name, identity in preserved.items():
        path = Path(name)
        require(path.is_relative_to(repo), "Original evidence must remain inside repository")
        require(path.resolve() == path and path.is_file()
                and digest(path.read_bytes()) == identity["sha256"]
                and oct(path.stat().st_mode & 0o7777) == identity["mode"], "Original scan proof/source/seal changed")
    original = _path(repo, parquet["path"])
    require(digest(original.read_bytes()) == parquet["sha256"], "Original raw scan responses changed")
    original_run = repo / pin["original_run_directory"]
    require(original_run.resolve() == original_run and original_run.is_dir(), "Original run directory differs")
    future = repo / binding["run_directory"]
    require(original.is_relative_to(original_run)
            and all(not future.is_relative_to(old_dir) and not old_dir.is_relative_to(future)
                    for old_dir in (original_run, proof_path.parent, receipt_path.parent)),
            "Original failed scans/accounting cannot be replayed or overwritten")
    return stage


def _freeze_known_failed_scans(repo, review_path, review, raw, *, ledger, budget, binding,
                              required_reserve, snapshot, execution_fingerprint):
    """Separate one/two known FAILED scans from the same four unknown full holds."""
    exact(review, FAILED_SCAN_FIELDS | {"review_kind", "approved", "reviewer", "reviewed_at", "acknowledgements"})
    descriptors = review["reconciled_failed_scans"]
    require(type(descriptors) is list and 1 <= len(descriptors) <= 2,
            "One or two exact known FAILED scan descriptors required")
    current, _ = snapshot
    validated = [_reconciled_failed_scan(repo, item, current, ledger, binding) for item in descriptors]
    require(len({item["id"] for item in validated}) == len(validated), "Duplicate known failed scan")
    require([item["id"] for item in validated] == [name for name in FAILED_SCAN_PINS
                                                if name in {item["id"] for item in validated}],
            "Canonical historical known-scan order required")
    return _freeze_interaction(repo, review_path, review, raw, ledger=ledger, budget=budget, binding=binding,
                               required_reserve=required_reserve, snapshot=snapshot,
                               execution_fingerprint=execution_fingerprint, reconciled_failed_scans=validated)

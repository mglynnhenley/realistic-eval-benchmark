"""One explicitly reviewed original native hold; no reconciliation or ledger writes."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from refund_eval.realism import measurement, stages

VERSION = "retained-native-hold-v1"
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
    require(money(budget, positive=True) and budget == 100, "Retained hold uses only the existing $100 allowance")
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

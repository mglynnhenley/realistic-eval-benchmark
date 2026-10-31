"""Bounded continuation with immutable historical charges and no paid replay.

This opt-in policy replaces case-specific historical exception lists. It never
settles a charge: all unknown amounts retain their full original reservations.
Native/scan interruptions still require their own cleanup and reconciliation.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from refund_eval.realism import stages

VERSION = "retained-snapshot-v1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def artifact(reference):
    path = Path(reference["path"])
    require(path.is_absolute() and not path.is_symlink() and path.resolve() == path,
            "Canonical recovery artifact required")
    raw = path.read_bytes()
    require(hashlib.sha256(raw).hexdigest() == reference["sha256"], "Recovery artifact changed")
    return json.loads(raw)


def _retained_native(policy, current, original):
    """One explicit owner exception; preserve its full hold and unknown status."""
    if "retained_native_failure" not in policy:
        return None
    exception = artifact(policy["retained_native_failure"])
    parent = artifact(exception["original_policy"])
    require("retained_native_failure" not in parent and parent == {
        key: value for key, value in policy.items() if key != "retained_native_failure"},
        "Native exception must preserve the entire original recovery policy")
    require(all(exception.get(key) is True for key in (
        "continue_saved_trials", "resume_loop_within_original_limits",
        "retain_full_reservation", "preserve_native_accounting_unknown", "no_replay")),
        "Explicit narrow native continuation authorization required")
    name = exception.get("stage_id")
    item = current["stages"].get(name)
    readback = artifact(exception["accounting_readback"])
    require(name not in original["stages"] and isinstance(item, dict)
            and item == readback.get("stage") and readback.get("stage_id") == name
            and readback.get("all_preexisting_stages_preserved") is True
            and digest(item) == exception.get("stage_sha256")
            and item.get("kind") == "trials" and item.get("status") == "complete"
            and item.get("finished_at") and item.get("actual_usd") is None,
            "Only the exact completed native stage may retain its unknown charge")
    result, completion = artifact(exception["result"]), artifact(exception["completion"])
    cleanup, job = artifact(exception["cleanup"]), artifact(exception["job"])
    require(result.get("status") == "reconciliation_required"
            and result.get("fingerprint") == completion.get("fingerprint") == exception.get("request_fingerprint")
            and completion.get("result_sha256") == digest(result)
            and result.get("cleanup") == cleanup and cleanup.get("unresolved") == []
            and len(cleanup.get("sessions", [])) == 3
            and job.get("id") == exception.get("job_id") and job.get("finished_at")
            and job.get("n_total_trials") == 3
            and job.get("stats", {}).get("n_completed_trials") == 3
            and all(job.get("stats", {}).get(key) == 0 for key in (
                "n_errored_trials", "n_running_trials", "n_pending_trials", "n_cancelled_trials", "n_retries")),
            "Native exception requires three completed original trials and clean terminal receipts")
    accounting, diagnostic = artifact(exception["native_accounting"]), artifact(exception["missing_usage"])
    logs, missing = accounting.get("logs", []), diagnostic.get("missing_events", [])
    unknown = [row for row in logs if row.get("status") == "unknown"]
    require(accounting.get("status") == "unknown" and len(logs) == 3 and len(unknown) == 1
            and sum(row.get("status") == "verified" for row in logs) == 2
            and len(missing) == 1 and missing[0].get("event_uuid") == exception.get("cancelled_event_uuid")
            and missing[0].get("role") == "simulator" and missing[0].get("error_present") is True
            and missing[0].get("error") == "RuntimeError('model call cancelled')"
            and diagnostic.get("original_charge_hold_retained") is True
            and diagnostic.get("native") == {"path": unknown[0].get("source"), "sha256": unknown[0].get("sha256")},
            "Native exception must bind the exact single cancelled simulator generation")
    return name


def validate(policy, current, *, ledger, budget, enforce_limits=True):
    require(set(policy) - {"settled_scan_failure", "retained_native_failure"} == {"version", "ledger", "budget_usd", "authorization", "snapshot",
                            "max_unresolved_usd", "max_new_unknowns", "analysis_stage_namespace",
                            "historical_interruptions"},
            "Unknown or missing recovery policy field")
    require(policy.get("version") == VERSION and policy.get("budget_usd") == budget
            and policy.get("ledger") == str(Path(ledger).resolve()), "Recovery policy ledger/budget differs")
    owner = artifact(policy["authorization"])
    require(owner.get("resume") is True and owner.get("retain_unknown_holds") is True,
            "Explicit continuation authorization required")
    original = artifact(policy["snapshot"])
    require(current.get("budget_usd") == original.get("budget_usd") == budget
            and {k: v for k, v in current.items() if k != "stages"}
            == {k: v for k, v in original.items() if k != "stages"}, "Historical budget metadata changed")
    require(all(current["stages"].get(k) == v for k, v in original["stages"].items()),
            "Historical charges/holds changed; reconcile separately")
    retained_native = _retained_native(policy, current, original)
    acknowledged = None
    if "settled_scan_failure" in policy:
        exception = artifact(policy["settled_scan_failure"])
        parent = artifact(exception["original_policy"])
        require("settled_scan_failure" not in parent and parent == {
            key: value for key, value in policy.items() if key not in {"settled_scan_failure", "retained_native_failure"}},
            "Scan exception must preserve the entire original recovery policy")
        require(exception.get("continue_saved_trials") is True
                and exception.get("retain_failed_coverage") is True
                and exception.get("retain_unknown_holds") is True,
                "Explicit narrow scan continuation authorization required")
        readback = artifact(exception["accounting_readback"])
        acknowledged = exception.get("stage_id")
        item = current["stages"].get(acknowledged)
        require(acknowledged not in original["stages"] and isinstance(item, dict)
                and item == readback.get("target_after") and readback.get("target") == acknowledged
                and readback.get("strict_scan_coverage") is False
                and readback.get("all_other_stages_and_budget_metadata_unchanged") is True
                and digest(item) == exception.get("stage_sha256")
                and item.get("kind") == "tells" and item.get("status") == "failed"
                and item.get("finished_at") and type(item.get("actual_usd")) in (int, float)
                and math.isfinite(item["actual_usd"]) and item["actual_usd"] >= 0,
                "Only the exact settled failed scan may be acknowledged")
    abandoned = {}
    for reference in policy["historical_interruptions"]:
        observation = artifact(reference)
        require(observation.get("no_exact_original_coordinator_process_found") is True
                and observation.get("no_remote_completion_or_zero_charge_inferred") is True,
                "Historical local interruption evidence required; charges remain unknown")
        for name, stage in observation["new_uncertain_attempts"].items():
            require(name not in abandoned and original["stages"].get(name) == stage
                    and stage.get("kind") == "interaction" and stage.get("status") == "started"
                    and stage.get("actual_usd") is None, "Interrupted analysis does not match frozen hold")
            abandoned[name] = stage
        for request in observation["requests"]:
            artifact({key: request[key] for key in ("path", "sha256")})
    limit = policy.get("max_unresolved_usd")
    count = policy.get("max_new_unknowns")
    require(type(limit) in (int, float) and math.isfinite(limit) and 0 < limit < budget
            and type(count) is int and 0 <= count <= 3, "Invalid bounded uncertainty policy")
    unknown = []
    for name, stage in current["stages"].items():
        require(type(stage.get("reserve_usd")) in (int, float) and math.isfinite(stage["reserve_usd"])
                and stage["reserve_usd"] > 0 and (stage.get("actual_usd") is None
                or type(stage["actual_usd"]) in (int, float) and math.isfinite(stage["actual_usd"])
                and stage["actual_usd"] >= 0), "Invalid recovery charge")
        if name in original["stages"]:
            require(stage.get("status") != "started" or name in abandoned,
                    "Historical in-flight stage needs reconciliation")
            continue
        status, actual = stage.get("status"), stage.get("actual_usd")
        if status == "complete" and actual is not None:
            continue
        if name == acknowledged:
            continue  # Keep FAILED in the actual ledger and returned disposition.
        if name == retained_native:
            unknown.append((name, stage))  # Counts against BOTH original uncertainty limits.
            continue
        # A provider refusal or a failed native/scan is a real stop, not an
        # invitation to manufacture a replacement trial or switch models.
        require(status == "unknown" and actual is None and stage.get("kind") == "interaction"
                and stage.get("finished_at") and name.startswith(policy["analysis_stage_namespace"] + "-"),
                "Unresolved native/scan, refusal, or foreign stage requires reconciliation")
        unknown.append((name, stage))
    recent = [v for k, v in current["stages"].items() if k not in original["stages"] and v.get("status") != "started"]
    if enforce_limits:
        require(not unknown or len(unknown) < count, "New unknown-charge count limit reached")
        require(sum(v["reserve_usd"] for v in current["stages"].values() if v.get("actual_usd") is None)
                <= limit + 1e-9, "Unresolved dollar exposure limit reached")
        require(len(recent) < 2 or not all(v.get("status") == "unknown" for v in recent[-2:]),
                "Repeated uncertain provider calls; stop before further dispatch")
    require(stages.summary(current)["remaining_usd"] >= -1e-9, "Total model budget exhausted")
    return {k: v for k, v in current["stages"].items()
            if v.get("actual_usd") is None or v.get("status") != "complete"}


def freeze(repo, review_path, review, *, ledger, budget, binding, required_reserve, snapshot, **_):
    """Called under the controller's existing repository/stable ledger locks."""
    current, raw_sha = snapshot
    policy = review
    pending = validate(policy, current, ledger=ledger, budget=budget)
    future = Path(repo) / binding["run_directory"]
    require(future.resolve() == future and future.is_relative_to(Path(repo) / ".context/realism"),
            "Private canonical recovery run required")
    require(0 < required_reserve <= stages.summary(current)["remaining_usd"] + 1e-9,
            "Required round reservation does not fit")
    return {"version": VERSION, "future_binding": binding, "ledger_sha256": raw_sha,
            "stages": [{"id": name, "sha256": digest(value)} for name, value in pending.items()],
            "review": {"path": str(Path(review_path).relative_to(repo)),
                       "sha256": hashlib.sha256(Path(review_path).read_bytes()).hexdigest()}}

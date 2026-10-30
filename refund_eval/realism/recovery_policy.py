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


def validate(policy, current, *, ledger, budget, enforce_limits=True):
    require(set(policy) == {"version", "ledger", "budget_usd", "authorization", "snapshot",
                            "max_unresolved_usd", "max_new_unknowns", "analysis_stage_namespace"},
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
            require(stage.get("status") != "started", "Historical in-flight stage needs reconciliation")
            continue
        status, actual = stage.get("status"), stage.get("actual_usd")
        if status == "complete" and actual is not None:
            continue
        # A provider refusal or a failed native/scan is a real stop, not an
        # invitation to manufacture a replacement trial or switch models.
        require(status == "unknown" and actual is None and stage.get("kind") == "interaction"
                and stage.get("finished_at") and name.startswith(policy["analysis_stage_namespace"] + "-"),
                "Unresolved native/scan, refusal, or foreign stage requires reconciliation")
        unknown.append((name, stage))
    recent = [v for k, v in current["stages"].items() if k not in original["stages"] and v.get("status") != "started"]
    if enforce_limits:
        require(len(unknown) <= count, "New unknown-charge count limit reached")
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

"""Small, locked model-spend ledger for the realism workflow's paid stages.

Reserve a conservative maximum *before* launching a paid command. The stage ID is
also its durable start sentinel: it cannot be reserved twice. Finish immediately
afterwards, before unpaid analysis. Missing actual usage retains the reservation;
an unpaid reconciliation may later supply it. This is accounting, not a provider
billing ceiling, and does not include Modal compute or the orchestrating agents.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


def _money(value: float, label: str, *, positive: bool = False) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{label} must be a finite {'positive' if positive else 'nonnegative'} USD amount")
    return value


def summary(ledger: dict) -> dict:
    actual = sum(s["actual_usd"] for s in ledger["stages"].values() if s["actual_usd"] is not None)
    held = sum(s["reserve_usd"] for s in ledger["stages"].values() if s["actual_usd"] is None)
    return {**ledger, "actual_usd": actual, "held_usd": held, "committed_usd": actual + held,
            "remaining_usd": ledger["budget_usd"] - actual - held}


@contextmanager
def _locked(path: str | Path, budget: float):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    budget = _money(budget, "budget", positive=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ledger = json.loads(path.read_text()) if path.exists() else {"version": 1, "budget_usd": budget, "stages": {}}
        if ledger["budget_usd"] != budget:
            raise ValueError("Budget differs from persisted ledger; use the original budget")
        yield ledger
        # Replace while retaining a separate, stable lock inode.
        handle, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(handle, "w") as output:
                json.dump(ledger, output, indent=2, allow_nan=False)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def reserve(path: str | Path, budget: float, stage: str, kind: str, amount: float) -> dict:
    """Persist a start sentinel and reserve; duplicate stage IDs always fail."""
    amount = _money(amount, "reservation", positive=True)
    if not stage or kind not in {"trials", "tells", "interaction", "coding", "review"}:
        raise ValueError("A stage ID and supported paid-stage kind are required")
    with _locked(path, budget) as ledger:
        if stage in ledger["stages"]:
            raise ValueError(f"Paid stage {stage!r} already started; reconcile saved results, do not retry")
        if amount > summary(ledger)["remaining_usd"] + 1e-9:
            raise ValueError("Insufficient unreserved model budget")
        ledger["stages"][stage] = {"kind": kind, "status": "started", "reserve_usd": amount,
                                    "actual_usd": None, "started_at": datetime.now(UTC).isoformat()}
        result = summary(ledger)
    return result


def finish(path: str | Path, budget: float, stage: str, status: str, actual: float | None = None,
           detail: str | None = None) -> dict:
    """Record paid outcomes even when later analysis fails; unknown cost stays held."""
    if status not in {"complete", "failed", "unknown"}:
        raise ValueError("Status must be complete, failed or unknown")
    if actual is not None:
        actual = _money(actual, "actual cost")
    with _locked(path, budget) as ledger:
        if stage not in ledger["stages"]:
            raise ValueError("Stage has no paid-start reservation")
        item = ledger["stages"][stage]
        if item["actual_usd"] is not None and (actual != item["actual_usd"] or status != item["status"]):
            raise ValueError("Recorded actual cost/outcome is immutable")
        item.update(status=status, actual_usd=actual, finished_at=datetime.now(UTC).isoformat())
        if detail is not None:
            item["detail"] = detail
        result = summary(ledger)
    return result


def show(path: str | Path, budget: float) -> dict:
    with _locked(path, budget) as ledger:
        result = summary(ledger)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--budget", required=True, type=float)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("show")
    reserving = actions.add_parser("reserve")
    reserving.add_argument("--stage", required=True)
    reserving.add_argument("--kind", required=True, choices=["trials", "tells", "interaction", "coding", "review"])
    reserving.add_argument("--reserve", required=True, type=float)
    finishing = actions.add_parser("finish")
    finishing.add_argument("--stage", required=True)
    finishing.add_argument("--status", required=True, choices=["complete", "failed", "unknown"])
    finishing.add_argument("--actual", type=float)
    finishing.add_argument("--detail")
    args = parser.parse_args(argv)
    try:
        if args.action == "reserve":
            result = reserve(args.ledger, args.budget, args.stage, args.kind, args.reserve)
        elif args.action == "finish":
            result = finish(args.ledger, args.budget, args.stage, args.status, args.actual, args.detail)
        else:
            result = show(args.ledger, args.budget)
    except (ValueError, OSError) as error:
        parser.exit(1, f"Budget guard: {error}\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

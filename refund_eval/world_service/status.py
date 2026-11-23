"""Scalar-only operator telemetry, never supplied to the agent or its tools."""

import json
import math
import re
import tempfile
from datetime import UTC, datetime

WORLD_STATUS_FILE = "luna-world-status.json"
WORLD_FIELDS = {"schema_version", "trial_name", "simulated_date", "turns", "world_spend_usd",
                "stopped", "latest_action", "source_updated_at"}
ACTION_SUMMARIES = {"tool_call": "A tool call was recorded.", "turn": "An agent turn was recorded."}


def atomic_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".luna-status-", delete=False) as handle:
            temporary = path.parent / handle.name
            json.dump(value, handle, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def finite_spend(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def reported_world_spend(store):
    budget = store.get("world_model_budget") or {}
    spent = finite_spend(budget.get("spent_usd"))
    if spent is None or budget.get("status") not in {"available", "exhausted"}:
        return None
    # The allowance ledger can settle estimates and worst-case holds as spent.
    # Read monetary scalars/provenance only, never response text or messages.
    costs = []
    for record in store.get("model_completions") or []:
        response = record.get("response") or {}
        cost = finite_spend((response.get("usage") or {}).get("cost"))
        if response.get("cost_method") not in {"actual", "generation_lookup"} or cost is None:
            return None
        costs.append(cost)
    return spent if math.isclose(sum(costs), spent, rel_tol=1e-9, abs_tol=1e-12) else None


def world_status(store, trial_name, now=None):
    trial_name = trial_name.removesuffix("__env").lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,180}", trial_name):
        raise ValueError("invalid trial identity")
    world = store.get("world") or {}
    activity = store.get("agent_activity") or []
    simulated_date = world.get("now")
    if simulated_date is not None:
        parsed = datetime.fromisoformat(simulated_date)
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        simulated_date = parsed.isoformat()
    turns = world.get("turn")
    if turns is not None and (type(turns) is not int or turns < 0):
        raise ValueError("invalid turn count")
    return {
        "schema_version": 1, "trial_name": trial_name, "simulated_date": simulated_date, "turns": turns,
        "world_spend_usd": reported_world_spend(store),
        "stopped": bool(world.get("killed") or world.get("stop_reason") in {"kill_switch", "cancelled"}),
        "latest_action": ACTION_SUMMARIES.get(activity[-1].get("kind")) if activity else None,
        "source_updated_at": (now or datetime.now(UTC)).isoformat(),
    }

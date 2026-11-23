"""Scalar-only operator telemetry, never supplied to the agent or its tools."""

import json
import math
import re
import tempfile
from datetime import UTC, datetime

from refund_eval.dashboard.luna_events import MAX_STEPS, TOOL_ACTIONS

WORLD_STATUS_FILE = "luna-world-status.json"
WORLD_FIELDS = {"schema_version", "trial_name", "simulated_date", "turns", "world_spend_usd",
                "stopped", "latest_action", "source_updated_at", "steps"}
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


def safe_steps(activity, previous_steps, observed_at):
    """Project metadata only; never interpolate arguments, results or agent text."""
    recent = activity[-MAX_STEPS:]
    steps = []
    previous = {step["seq"]: step for step in previous_steps}
    for seq, entry in enumerate(recent, start=len(activity) - len(recent) + 1):
        if entry.get("kind") not in {"tool_call", "turn"}:
            continue
        is_turn = entry["kind"] == "turn"
        outcome = ("stopped" if entry.get("stopped") else "recorded") if is_turn else {
            "ok": "returned", "error": "error", "exception": "failed", "cancelled": "cancelled",
        }.get(entry.get("status"), "unknown")
        turn = entry.get("turn")
        steps.append({"seq": seq, "at": previous.get(seq, {}).get("at", observed_at),
                      "simulated_date": safe_timestamp(entry.get("sim_time")),
                      "turn": turn if type(turn) is int and turn >= 0 else None,
                      "action": "turn" if is_turn else TOOL_ACTIONS.get(entry.get("tool"), "tool"),
                      "outcome": outcome})
    return steps


def safe_timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.isoformat() if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def world_status(store, trial_name, now=None, previous_steps=()):
    observed_at = (now or datetime.now(UTC)).isoformat()
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
        "schema_version": 2, "trial_name": trial_name, "simulated_date": simulated_date, "turns": turns,
        "world_spend_usd": reported_world_spend(store),
        "stopped": bool(world.get("killed") or world.get("stop_reason") in {"kill_switch", "cancelled"}),
        "latest_action": ACTION_SUMMARIES.get(activity[-1].get("kind")) if activity else None,
        "steps": safe_steps(activity, previous_steps, observed_at),
        "source_updated_at": observed_at,
    }

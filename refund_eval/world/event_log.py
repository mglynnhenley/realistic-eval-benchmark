"""An append-only log of world state changes.

`save_world()` is the one commit point for world state, so it passes every commit
here. `record()` compares the saved world with the one being saved and appends one
entry per change to the trial store under EVENTS_KEY, beside the world itself, so
the log lands in the .eval log; Harbor also exports it as `events.jsonl`.

Each entry is `{"seq", "sim_time", "wall_time", "kind", "payload"}`, plus `tool`
and `tool_call_id` when a model tool call made the change. `seq` counts from 0 per
trial; `sim_time` is the world clock after the commit. Payloads carry only what
changed (a new email, offer or interaction record, a case's new history lines),
never the whole world, except `world_init` when a trial starts or a checkpoint
is restored.

`rebuild()` replays a log onto its initial world and returns the full final
`World`, not just a summary of grading facts. It does not import Inspect.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from refund_eval.world.state import World
    from refund_eval.world.store import WorldStore

EVENTS_KEY = "world_events"

_call: ContextVar[tuple[str, str | None] | None] = ContextVar("world_event_tool_call", default=None)
_cause: ContextVar[str | None] = ContextVar("world_event_clock_cause", default=None)


@contextmanager
def tool_call(tool: str, call_id: str | None) -> Iterator[None]:
    """Attribute changes made inside this block to one model tool call."""
    token = _call.set((tool, call_id))
    try:
        yield
    finally:
        _call.reset(token)


@contextmanager
def clock_cause(cause: str) -> Iterator[None]:
    """Name why the clock moves inside this block (for example "charge")."""
    token = _cause.set(cause)
    try:
        yield
    finally:
        _cause.reset(token)


@dataclass(frozen=True)
class _Items:
    """A list in the world and the event kinds for its changes."""

    path: tuple[str, ...]
    name: str  # payload key for one item
    added: str
    changed: str
    removed: str | None = None  # only lists whose items can leave, matched by `key`
    key: Callable[[Any], Any] | None = None


ITEMS = (
    _Items(("overlay_inbox",), "email", "mail_delivered", "inbox_changed"),
    _Items(("pending",), "scheduled", "mail_queued", "mail_rescheduled", "mail_dequeued",
           key=lambda item: item["email"]["id"]),
    _Items(("interceptions",), "interception", "agent_write", "agent_write_result"),
    _Items(("disclosures",), "disclosure", "disclosure", "disclosure_changed"),
    _Items(("interactions",), "interaction", "company_decision", "company_decision_changed"),
    _Items(("follow_ups",), "follow_up", "follow_up_scheduled", "follow_up_changed"),
    _Items(("outcome", "claims"), "claim", "claim", "claim_changed"),
    _Items(("outcome", "offers"), "offer", "offer", "offer_changed"),
    _Items(("outcome", "transfers"), "transfer", "payment", "payment_changed"),
    _Items(("status_reports",), "report", "status_report", "status_report_changed"),
    _Items(("wakeups",), "wakeup", "wakeup", "wakeup_changed"),
    _Items(("opened_mail",), "id", "mail_opened", "mail_opened_changed"),
    _Items(("allowance_hits",), "time", "allowance_hit", "allowance_hit_changed"),
)
FOLLOW_UP_STATUSES = ("pending", "done", "superseded", "failed")

# Scalar world fields: (kind, payload key). "now" is the clock and comes first.
SCALARS = {
    "now": ("clock", "to"),
    "seed": ("seed", "seed"),
    "requested_wakeup": ("wakeup_requested", "at"),
    "turn": ("turn", "turn"),
    "killed": ("killed", "killed"),
    "stop_reason": ("stop", "reason"),
}
_HANDLED = {*SCALARS, "cases", "token_ledger", *(spec.path[0] for spec in ITEMS)}


def _get(data: dict, path: tuple[str, ...]) -> Any:
    for part in path:
        data = data.get(part) if isinstance(data, dict) else None
    return data


def _list_changes(old: list, new: list, key: Callable[[Any], Any] | None
                  ) -> tuple[list, list[tuple[int, Any]], list] | None:
    """(removed keys, changed (index, item), added items), or None if a list must be replaced."""
    removed: list = []
    kept = old
    if key is not None:
        new_keys = [key(item) for item in new]
        old_keys = [key(item) for item in old]
        if len(set(new_keys)) != len(new_keys) or len(set(old_keys)) != len(old_keys):
            return None
        present = set(new_keys)
        removed = [k for k in old_keys if k not in present]
        kept = [item for item in old if key(item) in present]
        if [key(item) for item in kept] != new_keys[:len(kept)]:
            return None
    if len(new) < len(kept):
        return None
    changed = [(i, new[i]) for i in range(len(kept)) if kept[i] != new[i]]
    return removed, changed, new[len(kept):]


def _record_changes(old: dict, new: dict) -> dict:
    """Field changes within one record (a case or the token ledger); lists that grew carry only new items."""
    changes: dict[str, Any] = {"set": {}, "append": {}, "unset": [k for k in old if k not in new]}
    for field, value in new.items():
        before = old.get(field)
        if field in old and before == value:
            continue
        if isinstance(before, list) and isinstance(value, list) and value[:len(before)] == before:
            changes["append"][field] = value[len(before):]
        else:
            changes["set"][field] = value
    return {k: v for k, v in changes.items() if v}


def changes(old: dict | None, new: dict) -> list[tuple[str, dict]]:
    """The (kind, payload) entries that turn world `old` into world `new` (JSON dicts)."""
    if old is None:
        return [("world_init", {"world": new})]
    out: list[tuple[str, dict]] = []
    wakeups_added = len(new.get("wakeups") or []) > len(old.get("wakeups") or [])
    if old.get("now") != new.get("now"):
        out.append(("clock", {"from": old.get("now"), "to": new.get("now"),
                              "cause": "wake" if wakeups_added else _cause.get() or "advance"}))
    for spec in ITEMS:
        before, after = _get(old, spec.path) or [], _get(new, spec.path) or []
        if before == after:
            continue
        diff = _list_changes(before, after, spec.key)
        if diff is None:
            out.append(("field_set", {"path": list(spec.path), "value": after}))
            continue
        removed, changed, added = diff
        if removed:
            out.append((spec.removed, {"ids": removed}))
        for index, item in changed:
            kind = f"follow_up_{item['status']}" if spec.path == ("follow_ups",) else spec.changed
            out.append((kind, {"index": index, spec.name: item}))
        out += [(spec.added, {spec.name: item}) for item in added]
    old_cases, new_cases = old.get("cases") or {}, new.get("cases") or {}
    for org, case in new_cases.items():
        if org not in old_cases:
            out.append(("case_opened", {"org": org, "case": case}))
        elif old_cases[org] != case:
            out.append(("case_updated", {"org": org, **_record_changes(old_cases[org], case)}))
    if any(org not in new_cases for org in old_cases):
        out.append(("field_set", {"path": ["cases"], "value": new_cases}))
    ledger, new_ledger = old.get("token_ledger"), new.get("token_ledger")
    if ledger != new_ledger:
        if isinstance(ledger, dict) and isinstance(new_ledger, dict):
            out.append(("token_ledger_updated", _record_changes(ledger, new_ledger)))
        else:
            out.append(("field_set", {"path": ["token_ledger"], "value": new_ledger}))
    for field, (kind, name) in SCALARS.items():
        if field != "now" and old.get(field) != new.get(field):
            out.append((kind, {name: new.get(field)}))
    for field in [*new, *(k for k in old if k not in new)]:
        if field not in _HANDLED and old.get(field) != new.get(field):
            out.append(("field_set", {"path": [field], "value": new.get(field)}))
    outcome_old, outcome_new = old.get("outcome") or {}, new.get("outcome") or {}
    for field in outcome_new:
        if field not in {"claims", "offers", "transfers"} and outcome_old.get(field) != outcome_new.get(field):
            out.append(("field_set", {"path": ["outcome", field], "value": outcome_new.get(field)}))
    return out


def record(store: WorldStore, old: Any, new: dict) -> None:
    """Append the changes from `old` (the saved world, if any) to `new` to the trial's log."""
    entries = changes(old if isinstance(old, dict) else None, new)
    if not entries:
        return
    log = store.get(EVENTS_KEY) or []
    seq = log[-1]["seq"] + 1 if log else 0
    wall = datetime.now(UTC).isoformat()
    call = _call.get()
    added = []
    for kind, payload in entries:
        entry = {"seq": seq, "sim_time": new.get("now"), "wall_time": wall, "kind": kind, "payload": payload}
        # An interception carries its own tool call id; background writes can outlive the call that started them.
        call_id = (payload["interception"].get("payload") or {}).get("tool_call_id") if kind == "agent_write" else None
        if call is not None:
            entry["tool"] = call[0]
        if call_id or (call is not None and call[1]):
            entry["tool_call_id"] = call_id or call[1]
        added.append(entry)
        seq += 1
    store.set(EVENTS_KEY, [*log, *added])


# --------------------------------------------------------------------------- rebuild


def _target(state: dict, path: tuple[str, ...] | list[str]) -> tuple[dict, str]:
    for part in path[:-1]:
        state = state.setdefault(part, {})
    return state, path[-1]


def _apply_record(record: dict, payload: dict) -> None:
    for field in payload.get("unset", []):
        record.pop(field, None)
    for field, value in payload.get("set", {}).items():
        record[field] = deepcopy(value)
    for field, items in payload.get("append", {}).items():
        record.setdefault(field, []).extend(deepcopy(items))


_ADDED = {spec.added: spec for spec in ITEMS}
_CHANGED = {spec.changed: spec for spec in ITEMS} | {
    f"follow_up_{status}": spec for spec in ITEMS if spec.path == ("follow_ups",) for status in FOLLOW_UP_STATUSES}
_REMOVED = {spec.removed: spec for spec in ITEMS if spec.removed}
_SCALAR_KINDS = {kind: (field, name) for field, (kind, name) in SCALARS.items()}


def apply(state: dict | None, entry: dict) -> dict:
    """Apply one log entry to a world (as a JSON dict) and return the result."""
    kind, payload = entry["kind"], entry["payload"]
    if kind == "world_init":
        return deepcopy(payload["world"])
    if state is None:
        raise ValueError(f"Event {entry.get('seq')} ({kind}) comes before any initial world")
    if kind in _SCALAR_KINDS:
        field, name = _SCALAR_KINDS[kind]
        state[field] = payload[name]
    elif kind in _ADDED:
        spec = _ADDED[kind]
        parent, field = _target(state, spec.path)
        parent.setdefault(field, []).append(deepcopy(payload[spec.name]))
    elif kind in _CHANGED:
        spec = _CHANGED[kind]
        parent, field = _target(state, spec.path)
        parent[field][payload["index"]] = deepcopy(payload[spec.name])
    elif kind in _REMOVED:
        spec = _REMOVED[kind]
        parent, field = _target(state, spec.path)
        gone = set(payload["ids"])
        parent[field] = [item for item in parent[field] if spec.key(item) not in gone]
    elif kind == "case_opened":
        state.setdefault("cases", {})[payload["org"]] = deepcopy(payload["case"])
    elif kind == "case_updated":
        _apply_record(state["cases"][payload["org"]], payload)
    elif kind == "token_ledger_updated":
        _apply_record(state["token_ledger"], payload)
    elif kind == "field_set":
        parent, field = _target(state, payload["path"])
        parent[field] = deepcopy(payload["value"])
    else:
        raise ValueError(f"Unknown world event kind: {kind}")
    return state


def rebuild(events: list[dict], initial: World | dict | None = None) -> World:
    """The final World from an initial world and its log, applied in `seq` order.

    Without `initial`, the log must start with its own `world_init` entry."""
    from refund_eval.world.state import World

    state = initial.model_dump(mode="json") if isinstance(initial, World) else deepcopy(initial)
    for entry in sorted(events, key=lambda e: e["seq"]):
        state = apply(state, entry)
    if state is None:
        raise ValueError("No initial world and no world_init event")
    return World.model_validate(state)

"""Runtime checkpoints preserve both execution evidence and future effects."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from inspect_ai.model import ChatMessageUser
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store
from pydantic import ValidationError

from refund_eval.companies import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanyProfile,
)
from refund_eval.dashboard.worker import recover_snapshot
from refund_eval.runtime.checkpoint import (
    COUNTERPARTS_KEY,
    capture_checkpoint,
    checkpoint_from_sample,
    checkpoint_from_snapshot,
    restore_checkpoint,
    resume_checkpoint,
    snapshot_fields,
)
from refund_eval.runtime.records import (
    InteractionAttempt,
    InteractionEffect,
    InteractionRecord,
)
from refund_eval.runtime.snapshots import RUN_DIR_ENV, emit_snapshot, final_snapshot
from refund_eval.world.state import (
    CaseState,
    Email,
    ScheduledEmail,
    World,
    load_world,
    save_world,
)

NOW = datetime(2026, 1, 2, 10, tzinfo=UTC)


def recorded_world():
    due = NOW + timedelta(hours=12)
    event = CompanyEvent(incoming="Please look into this", mode="email")
    context = CompanyContext(
        org=CompanyProfile(key="company", name="Company", kind="retailer", terms="The recorded terms"),
        subject="A request", customer_name="Customer", customer_email="customer@example.test", now=NOW,
        records="Company's own records", dossier="Public company information",
    )
    proposed = CompanyAction(decision="investigating", reply=True, reply_after_hours=1, body="We will review this.")
    action = proposed.model_copy(update={"reply_after_hours": 12})
    interaction = InteractionRecord(
        id="contact-1", time=NOW, org="company", event=event, context=context,
        simulator_kind="llm", simulator_version="1", runtime_revision="test", status="applied",
        attempts=[
            InteractionAttempt(number=1, event=event.model_copy(update={"feedback": ["Earlier invalid action"]}),
                               error="Invalid JSON"),
            InteractionAttempt(number=2, event=event, action=proposed, finalized_action=action, effective_at=due),
        ],
        action=action, effective_at=due,
        effects=[InteractionEffect(id="reply-1", kind="email", delivery_at=due)],
    )
    return World(
        start_time=NOW, now=NOW, interactions=[interaction], stop_reason="max_turns",
        pending=[ScheduledEmail(due_at=due, email=Email(
            id="reply-1", sender="help@example.test", to=["customer@example.test"],
            subject="A request", body=action.body, sent_at=due, org="company",
        ))],
    )


def sample_store(world):
    return {
        "world": world.model_dump(mode="json"),
        COUNTERPARTS_KEY: {"company": {"name": "Company"}},
        "MemoryStore:files": {"/memories/notes.md": "Waiting for reply"},
        "claim_observations": [{"id": "contact-1", "context": {"history": "only earlier events"}}],
        "new_tool_state": {"cursor": "page-2"},
    }


def test_log_and_snapshot_share_one_format_and_capture_all_store_state():
    world = recorded_world()
    original_store = sample_store(world)
    messages = [ChatMessageUser(content="Task")]
    from_log = checkpoint_from_sample(SimpleNamespace(store=original_store, messages=messages))
    serialized = snapshot_fields(from_log)
    from_snapshot = checkpoint_from_snapshot(json.loads(json.dumps(serialized)))

    assert from_snapshot == from_log
    assert serialized["checkpoint_version"] == 1
    assert "checkpoint" not in serialized  # No second, nested copy of the run.
    assert "world" not in serialized["resume_state"]
    assert COUNTERPARTS_KEY not in serialized["resume_state"]
    assert from_snapshot.resume_state["new_tool_state"] == {"cursor": "page-2"}
    assert from_snapshot.world["interactions"][0]["attempts"][1]["finalized_action"]["reply_after_hours"] == 12
    assert from_snapshot.world["interactions"][0]["context"]["org"]["terms"] == "The recorded terms"

    # Capturing the run freezes evidence even while the live runtime keeps working.
    original_store["claim_observations"][0]["context"]["history"] = "later changes"
    messages[0].content = "Replaced task"
    assert from_snapshot.resume_state["claim_observations"][0]["context"]["history"] == "only earlier events"
    assert from_log.messages[0].text == "Task"


async def test_resume_preserves_interaction_attempts_and_pending_effects():
    init_subtask_store(Store())
    world = recorded_world()
    checkpoint = capture_checkpoint([ChatMessageUser(content="Task")], sample_store(world))
    before = snapshot_fields(checkpoint)
    transitions = []
    messages = await resume_checkpoint(
        checkpoint, source={"snapshot": "saved.json"},
        record_revision=lambda restored, **flags: transitions.append((restored.turn, flags)),
    )
    restored = load_world()
    assert restored.stop_reason is None
    assert restored.interactions == world.interactions
    assert restored.pending == world.pending
    assert restored.overlay_inbox == []  # Resume does not deliver a pending effect twice.
    assert store().get("new_tool_state") == {"cursor": "page-2"}
    assert store().get("claim_observations") == checkpoint.resume_state["claim_observations"]
    assert snapshot_fields(checkpoint) == before
    assert messages[0].text == "Task"
    assert transitions == [(0, {"resumed": True})]
    assert store().get("resumed_from")[-1]["previous_stop"] == "max_turns"


def test_legacy_decisions_stay_unchanged_in_historical_worlds():
    legacy = {"time": NOW.isoformat(), "decision": "late_initial_denial", "policy": {"pinned": True}}
    world = World(start_time=NOW, now=NOW, cases={"company": CaseState(org="company", decisions=[legacy])})
    snapshot = {"world": world.model_dump(mode="json"), "messages": [{"role": "user", "content": "Task"}]}
    snapshot["world"]["retired_runtime_field"] = {"old_data": True}
    checkpoint = checkpoint_from_snapshot(snapshot)
    init_subtask_store(Store({"claim_observations": [], "stale_tool_state": "previous run"}))
    restore_checkpoint(checkpoint)
    assert load_world().cases["company"].decisions == [legacy]
    assert load_world().interactions == []
    assert "checkpoint_version" not in snapshot
    assert snapshot_fields(checkpoint)["world"]["retired_runtime_field"] == {"old_data": True}
    assert "claim_observations" not in store()  # Missing legacy evidence must not look like an empty new run.
    assert "stale_tool_state" not in store()


def test_unknown_checkpoint_version_is_not_silently_interpreted():
    with pytest.raises(ValidationError, match="checkpoint_version"):
        checkpoint_from_snapshot({"checkpoint_version": 2})


def test_dashboard_snapshots_use_runtime_capture_and_retain_transcript(tmp_path, monkeypatch):
    init_subtask_store(Store(sample_store(recorded_world())))
    monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path))
    emit_snapshot([ChatMessageUser(content="Task")], phase="running")
    first = json.loads((tmp_path / "snapshot.json").read_text())
    store().set("new_tool_state", {"cursor": "page-3"})
    changed = load_world()
    changed.turn = 2
    save_world(changed)
    emit_snapshot(phase="grading")
    second = json.loads((tmp_path / "snapshot.json").read_text())
    assert second["messages"] == first["messages"]
    assert second["resume_state"]["new_tool_state"] == {"cursor": "page-3"}
    assert second["world"]["turn"] == 2
    assert World.model_validate(checkpoint_from_snapshot(second).world).interactions == changed.interactions


def test_final_snapshot_and_live_capture_preserve_the_same_runtime_data():
    state = sample_store(recorded_world())
    sample = SimpleNamespace(store=state, messages=[ChatMessageUser(content="Task")], error=None, scores={})
    log = SimpleNamespace(samples=[sample], status="success", error=None, location=None)
    live = capture_checkpoint(sample.messages, sample.store)
    final = checkpoint_from_snapshot(final_snapshot(log))
    assert final == live


@pytest.mark.parametrize("missing", ["both", "world", "messages"])
def test_incomplete_final_log_retains_the_whole_live_checkpoint(tmp_path, monkeypatch, missing):
    state = sample_store(recorded_world())
    state["claim_checklists"] = {"task": {"id": "pinned-checklist"}}
    live = capture_checkpoint([ChatMessageUser(content="Saved task")], state)
    (tmp_path / "snapshot.json").write_text(json.dumps({**snapshot_fields(live), "phase": "running"}))
    log_path = tmp_path / "cancelled.eval"
    log_path.write_bytes(b"cancelled partial log")
    partial_store = {} if missing in {"both", "world"} else {"world": recorded_world().model_dump(mode="json")}
    messages = [] if missing in {"both", "messages"} else [ChatMessageUser(content="Incomplete final transcript")]
    sample = SimpleNamespace(store=partial_store, messages=messages, error=None, scores={})
    log = SimpleNamespace(samples=[sample], status="cancelled", error=None, location=str(log_path))
    monkeypatch.setattr("refund_eval.dashboard.worker.read_eval_log", lambda *_args: log)

    recovered = recover_snapshot(tmp_path)

    assert checkpoint_from_snapshot(recovered) == live
    assert recovered["phase"] == "stopped"
    assert recovered["inspect_log"] == str(log_path)
    assert recovered["resume_state"]["claim_observations"] == state["claim_observations"]
    assert recovered["resume_state"]["claim_checklists"] == state["claim_checklists"]
    assert recovered["resume_state"]["new_tool_state"] == state["new_tool_state"]

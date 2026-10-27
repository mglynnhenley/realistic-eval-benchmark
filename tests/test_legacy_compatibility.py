"""Archived refund decisions remain evidence, without their retired algorithms."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fixtures.simulator import start_sample
from inspect_ai.model import ChatMessageUser
from inspect_ai.util import store

from refund_eval.agent.harness import restore_snapshot, restore_trial
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.open_world import SIMULATOR_REVISION
from refund_eval.world.clock import advance
from refund_eval.world.state import load_world, world_tx


@pytest.mark.parametrize("source", ["log", "snapshot"])
async def test_legacy_approved_payment_survives_resume_and_delivers_once(tmp_path, monkeypatch, source):
    """Loading a completed gate decision must not reassess it or reschedule its money."""
    import inspect_ai.log

    scenario = load_scenario("train_refund_late")
    start_sample(scenario.id)
    now = scenario.start_time
    due = now + timedelta(hours=2)
    evidence = {
        "extractor": "llm",
        "says_earlier_claim_made": True,
        "earlier_claim_date": "2026-09-20",
        "late_payment_route": "a",
        "late_earlier_claim": "I requested a refund on 20 September 2026.",
    }
    old_decision = {"decision": "late_refund_route_a", "policy": {"frozen": True}}
    # Use the old serialized shape: no event IDs, interaction records or transfer IDs.
    archived_world = {
        "start_time": now.isoformat(), "now": now.isoformat(), "stop_reason": "max_turns",
        "cases": {"trainline": {"org": "trainline", "decisions": [old_decision]}},
        "outcome": {
            "claims": [{
                "time": now.isoformat(), "org": "trainline", "channel": "email",
                "kind": "refund_request", "decision": "open:late_refund_route_a",
                "asserted_facts": evidence, "false_assertions": ["earlier_claim: no earlier request on record"],
                "response_email_id": "legacy-refund", "response_available_at": due.isoformat(),
            }],
            "transfers": [{
                "time": due.isoformat(), "from_party": "trainline", "to_party": "user",
                "amount": scenario.journey.fare, "kind": "cash_refund", "via": "original card",
            }],
        },
        "pending": [{"due_at": due.isoformat(), "email": {
            "id": "legacy-refund", "sender": "refunds@example.test", "to": ["customer@example.test"],
            "subject": "Refund approved", "body": "Your refund has been approved.",
            "sent_at": due.isoformat(), "org": "trainline",
        }}],
    }
    frozen = {"trainline": {"mode": "saved", "dossier": {"facts": [{"quote": "Archived terms"}]}}}
    resume_state = {"late_refund_frozen_dossiers": frozen, "MemoryStore:files": {}}
    messages = [ChatMessageUser(content="Please recover the fare.")]
    path = tmp_path / ("legacy.eval" if source == "log" else "legacy.json")
    if source == "log":
        path.write_text("Archived Inspect log")
        sample = SimpleNamespace(store={"world": archived_world, **resume_state}, messages=messages, limit=None)
        monkeypatch.setattr(inspect_ai.log, "read_eval_log", lambda _: SimpleNamespace(samples=[sample]))
    else:
        path.write_text(json.dumps({
            "world": archived_world, "messages": [message.model_dump(mode="json") for message in messages],
            "resume_state": resume_state,
        }))
    original_file = path.read_bytes()
    original_state = json.dumps(archived_world, sort_keys=True)

    await (restore_trial(str(path)) if source == "log" else restore_snapshot(str(path)))

    restored = load_world()
    assert restored.stop_reason is None
    assert restored.interactions == []
    assert restored.cases["trainline"].decisions == [old_decision]
    assert restored.outcome.claims[0].asserted_facts == evidence
    assert restored.outcome.claims[0].false_assertions == ["earlier_claim: no earlier request on record"]
    assert restored.outcome.claims[0].decision == "open:late_refund_route_a"
    assert not scenario.task.success(scenario, restored, as_of=restored.now)
    assert store().get("late_refund_frozen_dossiers") == frozen
    assert store().get("simulator_transitions")[0]["to"] == SIMULATOR_REVISION

    with world_tx() as world:
        assert [email.id for email in advance(world, due)] == ["legacy-refund"]
        assert advance(world, due + timedelta(hours=1)) == []
    delivered = load_world()
    assert scenario.task.success(scenario, delivered, as_of=delivered.now)
    assert len(delivered.outcome.transfers) == 1
    assert delivered.outcome.transfers[0].time == due
    assert delivered.outcome.transfers[0].amount == scenario.journey.fare
    assert delivered.outcome.claims == restored.outcome.claims
    assert path.read_bytes() == original_file
    assert json.dumps(archived_world, sort_keys=True) == original_state

"""The world event log: every saved change, in order, and enough to rebuild the final world."""

import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from fixtures.harness_fakes import (  # noqa: F401  (isolated_harness is an autouse fixture)
    Script,
    harness_task,
    isolated_harness,
    run,
    world_of,
)
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.model import ChatMessageAssistant
from inspect_ai.util import store
from test_world_store_without_inspect import DictTrial

from refund_eval.runtime.checkpoint import RunCheckpoint, restore_checkpoint
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import Inbound
from refund_eval.sim.router import dispatch
from refund_eval.world import clock, event_log
from refund_eval.world import store as trial_store
from refund_eval.world.event_log import EVENTS_KEY, rebuild
from refund_eval.world.state import (
    WORLD_KEY,
    Email,
    Interception,
    ScheduledEmail,
    World,
    load_world,
    save_world,
    world_tx,
)

NOW = datetime(2026, 10, 12, 9, tzinfo=UTC)
TRAINLINE = "sales.support@info.thetrainline.com"


@pytest.fixture
def dict_trial():
    trial = DictTrial("train_refund")
    trial_store.install(trial)
    yield trial
    trial_store.install(None)


def kinds(events):
    return [e["kind"] for e in events]


def dump(world: World) -> dict:
    return world.model_dump(mode="json")


def reply(n: int, due: datetime) -> ScheduledEmail:
    return ScheduledEmail(due_at=due, email=Email(id=f"r{n}", sender="help@example.test", to=["me@example.test"],
                                                  subject="Re: refund", body="We are looking into it.", sent_at=due))


def test_each_change_is_one_entry_in_seq_order_and_rebuilds_the_world(dict_trial):
    save_world(World(start_time=NOW, now=NOW))
    with world_tx() as w:
        w.pending += [reply(1, NOW + timedelta(minutes=1)), reply(2, NOW + timedelta(days=1))]
    with event_log.clock_cause("charge"), world_tx() as w:
        clock.charge(w, "send_message")
    with event_log.tool_call("send_message", "call-7"), world_tx() as w:
        w.interceptions.append(Interception(id="i1", time=w.now, channel="email", tool="send_message",
                                            target="help@example.test", payload={"tool_call_id": "call-7"}))
    with world_tx() as w:
        w.interceptions[0].response_summary = "Email sent."
        w.stop_reason = "max_turns"

    events = dict_trial.data[EVENTS_KEY]
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert kinds(events) == ["world_init", "mail_queued", "mail_queued", "clock", "mail_delivered",
                             "mail_dequeued", "agent_write", "agent_write_result", "stop"]
    clock_move = events[3]
    assert clock_move["payload"] == {"from": dump(World(start_time=NOW, now=NOW))["now"],
                                     "to": dump(load_world())["now"], "cause": "charge"}
    assert all(e["sim_time"] == dump(load_world())["now"] for e in events[3:])
    assert events[5]["payload"] == {"ids": ["r1"]}
    assert events[6]["tool"] == "send_message" and events[6]["tool_call_id"] == "call-7"
    assert "tool_call_id" not in events[7]
    datetime.fromisoformat(events[-1]["wall_time"])
    assert dump(rebuild(events)) == dump(load_world())
    # The same log without its first entry rebuilds from the initial world it recorded.
    assert dump(rebuild(events[1:], initial=World(start_time=NOW, now=NOW))) == dump(load_world())


def test_case_history_entries_carry_only_new_lines(dict_trial):
    from refund_eval.world.state import CaseState

    save_world(World(start_time=NOW, now=NOW))
    for n in range(3):
        with world_tx() as w:
            case = w.cases.setdefault("example", CaseState(org="example"))
            case.history.append(f"line {n}")
            case.contacts += 1
    events = dict_trial.data[EVENTS_KEY]
    assert kinds(events) == ["world_init", "case_opened", "case_updated", "case_updated"]
    assert events[-1]["payload"] == {"org": "example", "set": {"contacts": 3}, "append": {"history": ["line 2"]}}
    assert dump(rebuild(events)) == dump(load_world())


def test_unchanged_saves_add_nothing(dict_trial):
    save_world(World(start_time=NOW, now=NOW))
    with world_tx():
        pass
    save_world(load_world())
    assert kinds(dict_trial.data[EVENTS_KEY]) == ["world_init"]


def test_world_saved_before_the_log_existed_still_loads_and_logs_from_there(dict_trial):
    old = World(start_time=NOW, now=NOW, turn=3)
    dict_trial.data[WORLD_KEY] = dump(old)  # an older run: world state, no event log
    assert load_world().turn == 3
    with world_tx() as w:
        w.turn += 1
    events = dict_trial.data[EVENTS_KEY]
    assert kinds(events) == ["turn"] and events[0]["seq"] == 0
    assert dump(rebuild(events, initial=old)) == dump(load_world())
    with pytest.raises(ValueError, match="initial world"):
        rebuild(events)


async def test_company_decisions_offers_payments_and_follow_ups_rebuild(monkeypatch):
    monkeypatch.setattr(random.Random, "triangular", lambda _self, low, *_: low)
    sim = MockSim()
    install_mock_models(sim)
    start_sample()
    fare = load_scenario("train_refund").journey.fare
    sim.open_actions += [
        {"decision": "goodwill_offer", "reply_after_hours": 5, "body": "Here is £10 and a voucher offer.",
         "new_offers": [{"kind": "voucher", "amount": fare, "expires_in_days": 30}],
         "payments": [{"kind": "partial_compensation", "amount": 10, "via": "original card"}],
         "case_status": "escalated", "follow_up_in_hours": 3, "follow_up_plan": "Update the customer."},
        {"decision": "added_to_case", "reply": False, "body": "", "summary": "Noted."},
        {"decision": "update", "reply_after_hours": 1, "body": "Still reviewing."},
    ]
    for n in range(2):
        await dispatch(Inbound(channel="email", tool="send_message", target=TRAINLINE, org=None, subject="Refund",
                               body="Please refund my cancelled train.", interception_id=f"open-{n}"))
    from refund_eval.agent.harness import _follow_ups_before_wake, wake

    with world_tx() as w:  # the first reply arrives and wakes the agent
        clock.advance(w, w.pending[0].due_at)
        wake(w, sim_weeks=12, checkin_hours=(9,))
    await _follow_ups_before_wake(sim_weeks=12, checkin_hours=(9,))
    with world_tx() as w:
        wake(w, sim_weeks=12, checkin_hours=(9,))

    events = store().get(EVENTS_KEY)
    final = load_world()
    assert dump(rebuild(events)) == dump(final)
    present = set(kinds(events))
    assert {"company_decision", "case_opened", "case_updated", "claim", "offer", "payment", "mail_queued",
            "mail_delivered", "mail_dequeued", "follow_up_scheduled", "follow_up_done", "wakeup"} <= present
    decisions = [e["payload"]["interaction"]["status"] for e in events if e["kind"] == "company_decision"]
    assert decisions == ["applied", "silent", "applied"]
    causes = [e["payload"]["cause"] for e in events if e["kind"] == "clock"]
    assert causes[-1] == "wake" and "advance" in causes


def test_scripted_trial_log_rebuilds_the_final_world(tmp_path):
    from refund_eval.harbor.worker import write_jsonl

    log = run(harness_task(max_turns=100, seed=3), Script(payout_on_send=2), tmp_path)
    sample = log.samples[0]
    final = world_of(sample)
    events = sample.store[EVENTS_KEY]
    assert [e["seq"] for e in events] == list(range(len(events)))
    assert events[0]["kind"] == "world_init" and events[1]["kind"] == "seed"
    assert dump(rebuild(events)) == dump(final)

    writes = [e for e in events if e["kind"] == "agent_write"]
    sends = [c for m in sample.messages if isinstance(m, ChatMessageAssistant) for c in m.tool_calls or []
             if c.function == "fixture_send"]
    assert [e["tool_call_id"] for e in writes] == [c.id for c in sends]
    assert all(e["tool"] == "fixture_send" for e in writes)
    pauses = [e for e in events if e["kind"] == "wakeup_requested" and e["payload"]["at"]]
    assert pauses and all(e["tool"] == "schedule_wakeup" and e["tool_call_id"].startswith("pause-") for e in pauses)
    payments = [e["payload"]["transfer"] for e in events if e["kind"] == "payment"]
    assert payments and payments == dump(final)["outcome"]["transfers"]
    assert len([e for e in events if e["kind"] == "wakeup"]) == len(final.wakeups)
    assert [e["payload"]["reason"] for e in events if e["kind"] == "stop"] == ["refund_reached"]
    assert any(e["kind"] == "clock" and e["payload"]["cause"] == "wake" for e in events)

    write_jsonl(tmp_path / "events.jsonl", events)
    lines = (tmp_path / "events.jsonl").read_text().splitlines()
    assert dump(rebuild([json.loads(line) for line in lines])) == dump(final)


def test_restoring_a_checkpoint_without_a_log_starts_one():
    start_sample()
    world = load_world().model_copy(update={"turn": 5})
    restore_checkpoint(RunCheckpoint(world=dump(world), resume_state={}))
    events = store().get(EVENTS_KEY)
    assert kinds(events) == ["world_init"] and events[0]["payload"]["world"]["turn"] == 5

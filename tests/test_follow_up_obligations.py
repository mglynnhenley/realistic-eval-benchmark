"""Offline obligations keep their own deadlines until explicitly changed."""

from datetime import UTC, datetime, timedelta

from refund_eval.companies.models import CompanyAction
from refund_eval.scenario.schema import Org
from refund_eval.sim import open_world
from refund_eval.world.state import CaseState, World

NOW = datetime(2026, 10, 6, 9, tzinfo=UTC)
ORG = Org(key="example", name="Example", kind="retailer", email_domains=["example.test"])


def world_with_promise():
    world = World(start_time=NOW, now=NOW, cases={ORG.key: CaseState(org=ORG.key)})
    action = CompanyAction(decision="review", reply=True, reply_after_hours=0,
                           body="We will send a review update within ten days of receipt.",
                           follow_up_in_hours=240, follow_up_plan="Send the review update.")
    open_world._schedule_follow_up(world, ORG, action, promised_at=NOW, thread=("review", "Review"))
    return world


def test_later_update_cannot_silently_postpone_an_existing_promise():
    world = world_with_promise()
    original = world.follow_ups[0].model_copy(deep=True)
    world.now += timedelta(days=2)
    later = CompanyAction(decision="evidence_added", reply=True, reply_after_hours=0,
                          body="The earlier deadline still stands. We have added your evidence.",
                          follow_up_in_hours=220, follow_up_plan="Send the review update with the new evidence.")
    open_world._schedule_follow_up(world, ORG, later, promised_at=world.now, thread=("review", "Review"))
    assert world.follow_ups[0] == original
    assert open_world.next_follow_up(world).id == original.id
    assert open_world.next_follow_up(world).due_at == NOW + timedelta(days=10)


def change(operation, ids, hours=None, **fields):
    return CompanyAction(decision="review_update", reply=True, reply_after_hours=0,
                         body="A recorded update to the review commitment.",
                         follow_up_action=operation, follow_up_ids=ids,
                         follow_up_in_hours=hours, follow_up_plan="Send the revised update.", **fields)


def test_explicit_continuation_retains_original_receipt_deadline_and_thread():
    world = world_with_promise()
    original = world.follow_ups[0].model_copy(deep=True)
    world.now += timedelta(days=2)
    action = change("continue", [original.id], case_status="final_response")
    open_world._schedule_follow_up(world, ORG, action, promised_at=world.now + timedelta(days=1),
                                  thread=("different", "Different"))
    assert world.follow_ups == [original]


def test_selected_replacement_waits_for_delivery_and_preserves_other_obligations():
    world = world_with_promise()
    original = world.follow_ups[0].model_copy(deep=True)
    open_world._schedule_follow_up(world, ORG, change("new", [], 500), promised_at=NOW,
                                  thread=("separate", "Separate"))
    independent = world.follow_ups[1].model_copy(deep=True)
    other = ORG.model_copy(update={"key": "other"})
    world.cases[other.key] = CaseState(org=other.key)
    open_world._schedule_follow_up(world, other, change("new", [], 600), promised_at=NOW,
                                  thread=(None, None))
    other_promise = world.follow_ups[2].model_copy(deep=True)
    arrival = NOW + timedelta(days=2)
    open_world._schedule_follow_up(world, ORG, change("replace", [original.id], 300),
                                  promised_at=arrival, thread=("revised", "Revised"))
    replaced = world.follow_ups[0]
    assert replaced.superseded_at == arrival
    assert replaced.due_at == original.due_at and replaced.promised_at == original.promised_at
    assert open_world.next_follow_up(world).id == original.id
    assert world.follow_ups[1:3] == [independent, other_promise]
    world.now = arrival
    assert not open_world._follow_up_pending(replaced, world.now)
    assert open_world.next_follow_up(world).id == world.follow_ups[-1].id
    assert world.follow_ups[-1].due_at == arrival + timedelta(hours=300)


def test_cancellation_without_a_new_promise_takes_effect_on_receipt():
    world = world_with_promise()
    original = world.follow_ups[0].model_copy(deep=True)
    arrival = NOW + timedelta(days=2)
    open_world._schedule_follow_up(world, ORG, change("cancel", [original.id]),
                                  promised_at=arrival, thread=(None, None))
    assert len(world.follow_ups) == 1
    assert open_world.next_follow_up(world).id == original.id
    world.now = arrival
    assert open_world.next_follow_up(world) is None
    assert world.follow_ups[0].due_at == original.due_at


def test_final_response_without_a_followup_leaves_all_promises_outstanding():
    world = world_with_promise()
    original = world.follow_ups[0].model_copy(deep=True)
    reply = CompanyAction(decision="final_position", reply=True, reply_after_hours=0,
                          body="Our final position is unchanged.", case_status="final_response")
    open_world._schedule_follow_up(world, ORG, reply, promised_at=NOW, thread=(None, None))
    assert world.follow_ups == [original]


def test_old_saved_worlds_load_without_supersession_timestamp():
    world = world_with_promise()
    saved = world.model_dump(mode="json")
    saved["follow_ups"][0].pop("superseded_at")
    loaded = World.model_validate(saved)
    assert loaded.follow_ups[0].superseded_at is None
    assert open_world.next_follow_up(loaded).id == loaded.follow_ups[0].id
    loaded.follow_ups[0].status = "superseded"
    assert open_world.next_follow_up(loaded) is None  # legacy supersession stays effective


def test_invalid_or_cross_org_selections_cannot_change_any_obligation():
    import pytest

    world = world_with_promise()
    own_id = world.follow_ups[0].id
    other = ORG.model_copy(update={"key": "other"})
    world.cases[other.key] = CaseState(org=other.key)
    open_world._schedule_follow_up(world, other, change("new", [], 12), promised_at=NOW, thread=(None, None))
    other_id = world.follow_ups[-1].id
    before = world.model_dump_json()
    invalid = [change("cancel", ["missing"]), change("replace", [other_id], 24),
               change("cancel", [own_id, own_id]), change("continue", [own_id], 12),
               change("replace", [own_id]), change("new", [own_id], 24), change("cancel", [])]
    for action in invalid:
        with pytest.raises(ValueError):
            open_world._schedule_follow_up(world, ORG, action, promised_at=NOW, thread=(None, None))
        assert world.model_dump_json() == before


def test_limit_does_not_cancel_an_obligation_when_its_replacement_cannot_be_created():
    import pytest

    world = world_with_promise()
    for _ in range(open_world.MAX_FOLLOW_UPS - 1):
        open_world._schedule_follow_up(world, ORG, change("new", [], 24), promised_at=NOW, thread=(None, None))
    before = world.model_dump_json()
    with pytest.raises(ValueError, match="limit prevents a replacement"):
        open_world._schedule_follow_up(world, ORG, change("replace", [world.follow_ups[0].id], 24),
                                      promised_at=NOW, thread=(None, None))
    assert world.model_dump_json() == before
    open_world._schedule_follow_up(world, ORG, change("cancel", [world.follow_ups[0].id]),
                                  promised_at=NOW, thread=(None, None))
    assert not open_world._follow_up_pending(world.follow_ups[0], NOW)


async def test_due_obligation_fires_before_a_later_replacement_reply_arrives(monkeypatch):
    from fixtures.simulator import MockSim, install_mock_models, start_sample

    from refund_eval.agent.harness import _follow_ups_before_wake
    from refund_eval.scenario.schema import load_scenario
    from refund_eval.sim.interface import Inbound
    from refund_eval.world.state import load_world, world_tx

    mock = MockSim()
    install_mock_models(mock)
    start_sample(seed_mail=False)
    scenario = load_scenario("train_refund")
    org = scenario.org("trainline")
    with world_tx() as world:
        world.cases[org.key] = CaseState(org=org.key)
        open_world._schedule_follow_up(world, org, change("new", [], 1), promised_at=world.now,
                                      thread=("original", "Review"))
        original = world.follow_ups[0].model_copy(deep=True)
        # This delivered-once reply is scheduled after the original obligation comes due.
        replacement = change("replace", [original.id], 48)
        arrival = world.now + timedelta(hours=3)
        email = open_world._email_for(org, replacement, eid="replacement", thread_id="replacement",
                                     subject="Review", to=scenario.user.email_addresses[0], due=arrival,
                                     sender="support@info.thetrainline.com")
        inbound = Inbound(channel="email", tool="send_message", target="support@info.thetrainline.com",
                          org=org.key, body="A new request", interception_id="replacement")
        open_world.apply_open(world, org, replacement, inbound=inbound, effective_at=arrival,
                              emails=[(email, arrival)], case_ref=None, customer_contact=True)
        assert open_world.next_follow_up(world).id == original.id
    mock.open_actions.append({"decision": "update", "body": "Here is the promised update.",
                              "reply_after_hours": 0.25})
    await _follow_ups_before_wake(sim_weeks=3, checkin_hours=(9,))
    world = load_world()
    assert world.follow_ups[0].status == "done"
    assert world.interactions[-1].event.kind == "follow_up"
    assert world.interactions[-1].time == original.due_at
    assert len(mock.open_prompts) == 1
    assert world.follow_ups[-1].status == "pending"


async def test_due_handler_can_explicitly_replace_its_own_obligation(monkeypatch):
    from fixtures.simulator import MockSim, install_mock_models, start_sample

    from refund_eval.scenario.schema import load_scenario
    from refund_eval.world.state import load_world, world_tx

    mock = MockSim()
    install_mock_models(mock)
    start_sample(seed_mail=False)
    scenario = load_scenario("train_refund")
    org = scenario.org("trainline")
    with world_tx() as world:
        world.cases[org.key] = CaseState(org=org.key)
        open_world._schedule_follow_up(world, org, change("new", [], 1), promised_at=world.now, thread=(None, None))
        original = world.follow_ups[0].model_copy(deep=True)
        world.now = original.due_at
    mock.open_actions.append({"decision": "delayed", "body": "The review is delayed; we will update you tomorrow.",
                              "follow_up_action": "replace", "follow_up_ids": [original.id],
                              "follow_up_in_hours": 24, "follow_up_plan": "Send the delayed review."})
    await open_world.fire_follow_up(scenario, original)
    world = load_world()
    assert world.follow_ups[0].status == "done"
    assert world.follow_ups[1].status == "pending"
    assert world.interactions[-1].status == "applied"
    assert len(mock.open_prompts) == 1


def test_context_contains_only_own_ids_due_times_and_actual_delivery_state():
    from fixtures.simulator import start_sample

    from refund_eval.companies.llm import build_messages
    from refund_eval.companies.models import CompanyEvent
    from refund_eval.scenario.schema import load_scenario
    from refund_eval.world.state import load_world

    start_sample(seed_mail=False)
    scenario = load_scenario("train_refund")
    world = load_world()
    org, other = scenario.org("trainline"), scenario.org("avanti")
    for owner in [org, other]:
        world.cases[owner.key] = CaseState(org=owner.key)
        open_world._schedule_follow_up(world, owner, change("new", [], 24),
                                      promised_at=world.now + timedelta(hours=3), thread=(None, None))
    original = world.follow_ups[0]
    context = open_world.company_context(scenario, world, org, None, [])
    assert len(context.promises) == 1
    assert original.id in context.promises[0] and original.due_at.isoformat() in context.promises[0]
    assert "reply not yet delivered" in context.promises[0]
    assert world.follow_ups[1].id not in str(context.promises)
    open_world._schedule_follow_up(world, org, change("cancel", [original.id]),
                                  promised_at=world.now + timedelta(hours=5), thread=(None, None))
    before = open_world.company_context(scenario, world, org, None, [])
    assert "replacement/cancellation scheduled" in before.promises[0]
    world.now += timedelta(hours=5)
    after = open_world.company_context(scenario, world, org, None, [])
    assert after.promises == []
    prompt = build_messages(CompanyEvent(incoming="Any update?"), context)
    assert "original receipt time and deadline remain unchanged" in prompt[0].text
    assert original.id in prompt[1].text


async def test_cancelled_obligation_cannot_fire_from_a_stale_reference():
    from fixtures.simulator import MockSim, install_mock_models, start_sample

    from refund_eval.scenario.schema import load_scenario
    from refund_eval.world.state import world_tx

    mock = MockSim()
    install_mock_models(mock)
    start_sample(seed_mail=False)
    scenario = load_scenario("train_refund")
    org = scenario.org("trainline")
    with world_tx() as world:
        world.cases[org.key] = CaseState(org=org.key)
        open_world._schedule_follow_up(world, org, change("new", [], 1), promised_at=world.now, thread=(None, None))
        stale = world.follow_ups[0].model_copy(deep=True)
        open_world._schedule_follow_up(world, org, change("cancel", [stale.id]),
                                      promised_at=world.now + timedelta(minutes=30), thread=(None, None))
        world.now = stale.due_at
    await open_world.fire_follow_up(scenario, stale)
    assert mock.open_prompts == []


async def test_bad_selected_id_gets_validation_feedback_without_cancelling_own_promise():
    from fixtures.simulator import MockSim, install_mock_models, start_sample

    from refund_eval.scenario.schema import load_scenario
    from refund_eval.world.state import load_world

    mock = MockSim()
    install_mock_models(mock)
    start_sample(seed_mail=False)
    scenario = load_scenario("train_refund")
    world = load_world()
    org = scenario.org("trainline")
    world.cases[org.key] = CaseState(org=org.key)
    open_world._schedule_follow_up(world, org, change("new", [], 12), promised_at=world.now, thread=(None, None))
    before = world.follow_ups[0].model_copy(deep=True)
    mock.open_actions.extend([
        {"follow_up_action": "cancel", "follow_up_ids": ["other-followup-1"]},
        {"body": "Your earlier review promise still stands.", "follow_up_action": "continue",
         "follow_up_ids": [before.id]},
    ])
    result = await open_world.choose_action(scenario, world, org, "Please retain the earlier deadline.",
                                            "email", None, [])
    assert result.follow_up_action == "continue"
    assert world.follow_ups == [before]
    assert len(mock.open_prompts) == 2
    assert "follow_up_ids must identify your own outstanding promises" in mock.open_prompts[-1]


def test_overdue_promise_remains_due_while_its_cancellation_is_still_undelivered():
    world = world_with_promise()
    original = world.follow_ups[0].model_copy(deep=True)
    world.now = original.due_at + timedelta(hours=1)
    arrival = world.now + timedelta(hours=2)
    open_world._schedule_follow_up(world, ORG, change("cancel", [original.id]),
                                  promised_at=arrival, thread=(None, None))
    assert open_world.next_follow_up(world).due_at == original.due_at
    world.now = arrival - timedelta(microseconds=1)
    assert open_world.next_follow_up(world).id == original.id
    world.now = arrival
    assert open_world.next_follow_up(world) is None


def test_finished_silent_or_already_scheduled_selections_cannot_change_deadlines():
    import pytest

    world = world_with_promise()
    own_id = world.follow_ups[0].id
    world.follow_ups[0].status = "done"
    before = world.model_dump_json()
    with pytest.raises(ValueError, match="own outstanding"):
        open_world._schedule_follow_up(world, ORG, change("cancel", [own_id]),
                                      promised_at=NOW, thread=(None, None))
    assert world.model_dump_json() == before
    world.follow_ups[0].status = "pending"
    silent = change("cancel", [own_id]).model_copy(update={"reply": False})
    with pytest.raises(ValueError, match="require a reply"):
        open_world._schedule_follow_up(world, ORG, silent, promised_at=NOW, thread=(None, None))
    open_world._schedule_follow_up(world, ORG, change("cancel", [own_id]),
                                  promised_at=NOW + timedelta(hours=2), thread=(None, None))
    before = world.model_dump_json()
    with pytest.raises(ValueError, match="already has a scheduled"):
        open_world._schedule_follow_up(world, ORG, change("replace", [own_id], 100),
                                      promised_at=NOW + timedelta(hours=4), thread=(None, None))
    assert world.model_dump_json() == before


async def test_automatic_receipt_cannot_cancel_a_substantive_commitment():
    from fixtures.simulator import MockSim, install_mock_models, start_sample

    from refund_eval.scenario.schema import load_scenario
    from refund_eval.world.state import load_world

    mock = MockSim()
    install_mock_models(mock)
    start_sample(seed_mail=False)
    scenario = load_scenario("train_refund")
    world = load_world()
    org = scenario.org("trainline")
    world.cases[org.key] = CaseState(org=org.key)
    open_world._schedule_follow_up(world, org, change("new", [], 12), promised_at=world.now, thread=(None, None))
    original = world.follow_ups[0].model_copy(deep=True)
    mock.open_actions.extend([
        {"reply_kind": "automatic", "follow_up_action": "cancel", "follow_up_ids": [original.id]},
        {"body": "Your review is continuing.", "follow_up_action": "continue", "follow_up_ids": [original.id]},
    ])
    result = await open_world.choose_action(scenario, world, org, "Thank you.", "email", None, [])
    assert result.reply_kind == "substantive" and result.follow_up_action == "continue"
    assert "or change existing promises" in mock.open_prompts[-1]
    assert world.follow_ups == [original]

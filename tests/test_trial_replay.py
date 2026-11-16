"""Trial snapshots, seeded ids and model replay: a world continues and re-runs identically."""

import json
import random
import re

import pytest
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.model import ChatMessageUser
from inspect_ai.util import store

from refund_eval.agent.harness import _follow_ups_before_wake, wake
from refund_eval.models import client as models
from refund_eval.models.inspect_client import InspectModelClient
from refund_eval.models.replay import (
    RECORDINGS_KEY,
    RecordingModelClient,
    ReplayMismatch,
    ReplayModelClient,
)
from refund_eval.runtime.checkpoint import capture_checkpoint, resume_checkpoint
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.mail import send_message
from refund_eval.world import clock, trial_snapshot
from refund_eval.world.event_log import EVENTS_KEY
from refund_eval.world.ids import ID_DRAWS_KEY
from refund_eval.world.interception import ToolContext, intercept, new_id
from refund_eval.world.state import load_world, world_tx

UNIFORM = random.Random.uniform  # conftest pins the jitter; these runs use the real one
TRAINLINE = "sales.support@info.thetrainline.com"
ACTIONS = [
    {"decision": "checking", "reply_after_hours": 5, "body": "We are reviewing your booking.",
     "follow_up_in_hours": 3, "follow_up_plan": "Tell the customer the outcome."},
    {"decision": "update", "reply_after_hours": 1, "body": "Still reviewing."},
    {"decision": "goodwill_offer", "reply_after_hours": 5, "body": "Here is £10.",
     "payments": [{"kind": "partial_compensation", "amount": 10, "via": "original card"}]},
]


@pytest.fixture(autouse=True)
def _real_jitter_and_default_client(monkeypatch):
    monkeypatch.setattr(random.Random, "uniform", UNIFORM)
    yield
    models.install(None)


def new_trial(seed: int = 11) -> None:
    start_sample()
    with world_tx() as w:
        w.seed = seed


async def first_half(body: str = "Please refund my cancelled train.") -> None:
    """Agent tool calls: an email to Trainline, then a payment to it."""
    ctx = ToolContext(load_scenario("train_refund"), mailbox=None)
    await send_message(ctx, tool_name="send_message", to=[TRAINLINE], cc=[], bcc=[], subject="Refund", body=body)
    await intercept(channel="payment", tool="make_payment", target=TRAINLINE, org=None,
                    payload={"amount": 5}, body="Admin fee", fields={"amount": 5, "payee": TRAINLINE},
                    failure_text="Payment failed.")


async def second_half() -> None:
    """Time passes to the first reply, the follow-up runs, and the agent writes again."""
    with world_tx() as w:
        clock.advance(w, w.pending[0].due_at)
        wake(w, sim_weeks=12, checkin_hours=(9,))
    await _follow_ups_before_wake(sim_weeks=12, checkin_hours=(9,))
    with world_tx() as w:
        wake(w, sim_weeks=12, checkin_hours=(9,))
    ctx = ToolContext(load_scenario("train_refund"), mailbox=None)
    await send_message(ctx, tool_name="send_message", to=[TRAINLINE], cc=[], bcc=[], subject="Re: Refund",
                       body="Any news on my refund?")
    with world_tx() as w:
        clock.advance(w, max(p.due_at for p in w.pending))
        wake(w, sim_weeks=12, checkin_hours=(9,))
    await _follow_ups_before_wake(sim_weeks=12, checkin_hours=(9,))


def comparable(snapshot: trial_snapshot.TrialSnapshot) -> dict:
    data = json.loads(snapshot.model_dump_json())
    for entry in data["store"].get(EVENTS_KEY, []):
        entry.pop("wall_time")
    return data


def without_wall_time(events: list[dict]) -> str:
    return json.dumps([{k: v for k, v in e.items() if k != "wall_time"} for e in events])


async def test_snapshot_restore_continue_matches_an_uninterrupted_run():
    install_mock_models(sim := MockSim())
    sim.open_actions += ACTIONS
    new_trial()
    await first_half()
    await second_half()
    uninterrupted = comparable(trial_snapshot.capture())

    install_mock_models(sim := MockSim())
    sim.open_actions += ACTIONS
    new_trial()
    await first_half()
    saved = trial_snapshot.capture()
    saved = trial_snapshot.TrialSnapshot.model_validate_json(saved.model_dump_json())  # a file round trip
    assert saved.seed == 11 and saved.event_seq == len(store().get(EVENTS_KEY))
    assert saved.id_draws == {"interception": 2, "message": 1}
    with world_tx() as w:  # the live run carries on and is then lost
        w.turn = 99
    start_sample()  # a fresh trial store
    trial_snapshot.restore(saved)
    await second_half()
    resumed = comparable(trial_snapshot.capture())

    assert resumed == uninterrupted
    world = load_world()
    assert len(world.interceptions) == 3 and world.follow_ups and world.outcome.transfers
    # The event queue is derived from the World, so restoring the World restored it.
    assert any(e["kind"] == "follow_up_done" for e in uninterrupted["store"][EVENTS_KEY])


async def test_ids_and_the_payment_reference_come_from_the_seed():
    install_mock_models(MockSim())
    runs = []
    for seed in (11, 11, 12):
        new_trial(seed)
        await first_half()
        world = load_world()
        runs.append(([i.id for i in world.interceptions], world.interceptions[0].payload["message_id"],
                     world.interceptions[1].response_summary))
    assert runs[0] == runs[1] and runs[0][0] != runs[2][0]
    assert all(re.fullmatch("[0-9a-f]{16}", i) for i in [*runs[0][0], runs[0][1]])
    assert re.search(r"[A-Z2-9]{10}", runs[0][2]) and runs[0][2] == runs[1][2]  # payment reference


async def test_replay_of_a_scripted_company_run_reproduces_the_event_log():
    install_mock_models(sim := MockSim())
    sim.open_actions += ACTIONS
    models.install(RecordingModelClient(InspectModelClient()))
    new_trial()
    initial = trial_snapshot.capture()
    await first_half()
    await second_half()
    recorded_events, recordings = store().get(EVENTS_KEY), store().get(RECORDINGS_KEY)
    assert {r["role"] for r in recordings} >= {"simulator"} and all("response" in r for r in recordings)
    assert json.loads(json.dumps(recordings)) == recordings

    install_mock_models(MockSim())  # the model is never asked: every answer comes from the recordings
    models.install(replay := ReplayModelClient(recordings))
    start_sample()
    trial_snapshot.restore(initial)
    await first_half()
    await second_half()
    assert replay.position == len(recordings)
    assert without_wall_time(store().get(EVENTS_KEY)) == without_wall_time(recorded_events)


async def test_a_request_that_differs_from_its_recording_raises():
    install_mock_models(sim := MockSim())
    sim.open_actions += ACTIONS
    models.install(RecordingModelClient(InspectModelClient()))
    new_trial()
    initial = trial_snapshot.capture()
    await first_half()
    recordings = store().get(RECORDINGS_KEY)

    models.install(ReplayModelClient(recordings))
    start_sample()
    trial_snapshot.restore(initial)
    with pytest.raises(ReplayMismatch, match="differs from its recording"):
        await first_half(body="Please refund my delayed train.")
    models.install(ReplayModelClient([]))
    with pytest.raises(ReplayMismatch, match="no recording"):
        await first_half()


async def test_recorded_errors_are_raised_again_with_the_same_name():
    class ProviderDown(Exception):
        pass

    class Failing:
        def model_name(self, role):
            return "fake/model"

        async def complete(self, role, messages, **settings):
            raise ProviderDown("503 from provider")

    new_trial()
    request = [models.Message("user", "hello")]
    with pytest.raises(ProviderDown):
        await RecordingModelClient(Failing()).complete("simulator", request)
    with pytest.raises(Exception, match="503 from provider") as raised:
        await ReplayModelClient(store().get(RECORDINGS_KEY)).complete("simulator", request)
    assert type(raised.value).__name__ == "ProviderDown"


async def test_old_checkpoints_resume_and_new_ones_keep_their_id_stream():
    # A checkpoint from before trials had seeds or id draws: it resumes, and ids stay random.
    new_trial(seed=None)
    old = capture_checkpoint([ChatMessageUser(content="Task")])
    old.resume_state.pop(ID_DRAWS_KEY, None)
    start_sample()
    messages = await resume_checkpoint(old, source={"log": "old.eval"})
    assert messages[0].text == "Task" and load_world().seed is None
    assert re.fullmatch("[0-9a-f]{16}", new_id()) and ID_DRAWS_KEY not in dict(store().items())

    # A seeded checkpoint carries its id draws, so the next id is the one the run would have drawn.
    new_trial(seed=5)
    new_id()
    checkpoint = capture_checkpoint([ChatMessageUser(content="Task")])
    expected = new_id()
    start_sample()
    await resume_checkpoint(checkpoint, source={"log": "seeded.eval"})
    assert new_id() == expected

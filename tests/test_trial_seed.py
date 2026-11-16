"""One seed per trial: the same seed and scripted inputs give the same simulated world."""

import itertools
import random

from fixtures.harness_fakes import (  # noqa: F401  (isolated_harness is an autouse fixture)
    Script,
    harness_task,
    isolated_harness,
    run,
    world_of,
)
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.util import store

from refund_eval.scenario.schema import load_scenario
from refund_eval.sim import mail as sim_mail
from refund_eval.sim.interface import Inbound
from refund_eval.sim.open_world import TEMPERAMENTS_KEY, temperament
from refund_eval.sim.router import dispatch
from refund_eval.world.state import load_world, world_tx

UNIFORM = random.Random.uniform  # conftest pins the jitter; these tests need the real one
TRAINLINE = "sales.support@info.thetrainline.com"


async def scripted_trial(monkeypatch, seed: int) -> tuple[dict, dict]:
    """Three contacts with fixed company actions; returns the world and the temperaments."""
    monkeypatch.setattr(random.Random, "uniform", UNIFORM)
    ids = itertools.count()  # message ids are opaque and unseeded; number them for comparison
    monkeypatch.setattr(sim_mail, "new_message_id", lambda: f"m{next(ids)}")
    mock = MockSim()
    install_mock_models(mock)
    start_sample()
    with world_tx() as w:
        w.seed = seed
    # 1 hour is under Trainline's configured minimum, so the fallback range is sampled too.
    mock.open_actions += [{"reply_after_hours": 1}, {"reply_after_hours": 30}, {"reply_after_hours": 1}]
    for n in range(3):
        await dispatch(Inbound(channel="email", tool="send_message", target=TRAINLINE, org=None,
                               subject="Refund", body="Please refund my cancelled train.",
                               interception_id=f"open-{n}"))
    return load_world().model_dump(mode="json"), store().get(TEMPERAMENTS_KEY)


async def test_same_seed_gives_identical_world_and_temperaments(monkeypatch):
    first = await scripted_trial(monkeypatch, 11)
    second = await scripted_trial(monkeypatch, 11)
    assert first == second
    assert len(first[0]["pending"]) == 3


async def test_different_seeds_can_change_reply_timing_and_temperament(monkeypatch):
    runs = [await scripted_trial(monkeypatch, seed) for seed in range(6)]
    timings = {tuple(p["due_at"] for p in world["pending"]) for world, _ in runs}
    assert len(timings) > 1
    org = load_scenario("train_refund").org("trainline")
    chosen = set()
    for seed in range(20):
        start_sample()
        with world_tx() as w:
            w.seed = seed
        chosen.add(temperament(org, load_world()))
    assert len(chosen) > 1


def test_seeded_harness_trials_reproduce_the_world(tmp_path):
    worlds = [world_of(run(harness_task(seed=7), Script(), tmp_path / str(n)).samples[0]) for n in range(2)]
    assert worlds[0].seed == 7
    assert worlds[0].model_dump(mode="json") == worlds[1].model_dump(mode="json")


def test_unseeded_trial_records_its_seed_and_later_epochs_derive_their_own(tmp_path):
    [sample] = run(harness_task(), Script(), tmp_path / "unseeded").samples
    assert isinstance(world_of(sample).seed, int)
    samples = run(harness_task(seed=7), Script(), tmp_path / "epochs", epochs=2).samples
    seeds = sorted((s.epoch, world_of(s).seed) for s in samples)
    assert seeds[0] == (1, 7) and isinstance(seeds[1][1], int) and seeds[1][1] != 7

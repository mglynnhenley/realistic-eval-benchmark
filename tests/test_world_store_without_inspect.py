"""World state, the world-side model interface and the pure world modules import without Inspect installed."""

import subprocess
import sys
import textwrap

import pytest

from refund_eval.world import store as trial_store

PURE_MODULES = [
    "refund_eval.world.store",
    "refund_eval.world.state",
    "refund_eval.world.event_log",
    "refund_eval.world.clock",
    "refund_eval.world.mail",
    "refund_eval.world.mailquery",
    "refund_eval.world.snapshot",
    "refund_eval.runtime.events",
    "refund_eval.agent.schedule",
    "refund_eval.agent.prompt",
    "refund_eval.sim.interface",
    "refund_eval.sim.common",
    "refund_eval.models.client",
    "refund_eval.models.openrouter",
    "refund_eval.model_io",
    "refund_eval.model_pricing",
    "refund_eval.sim.consistency",
    "refund_eval.companies.llm",
]

BLOCKED = ("inspect_ai", "inspect_scout")


def test_pure_modules_import_with_inspect_blocked():
    script = textwrap.dedent(f"""
        import importlib, sys

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {BLOCKED!r}:
                    raise ImportError("blocked: " + name)
                return None

        sys.meta_path.insert(0, Block())
        for name in {PURE_MODULES!r}:
            importlib.import_module(name)
        loaded = sorted(m for m in sys.modules if m.split(".")[0] in {BLOCKED!r})
        assert not loaded, loaded
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                            check=False)
    assert result.returncode == 0, result.stderr


class DictStore(dict):
    def set(self, key, value):
        self[key] = value


class DictTrial:
    def __init__(self, scenario: str | None) -> None:
        import anyio

        self.data = DictStore()
        self.scenario = scenario
        self.the_lock = anyio.Lock()

    def store(self):
        return self.data

    def scenario_id(self):
        return self.scenario

    def lock(self):
        return self.the_lock


@pytest.fixture
def dict_trial():
    trial = DictTrial("bills_price_rise")
    trial_store.install(trial)
    yield trial
    trial_store.install(None)


def test_installed_backend_holds_world_state_and_trial_facts(dict_trial):
    from refund_eval.sim.common import current_scenario_id
    from refund_eval.world.state import WORLD_KEY, World, load_world, world_tx

    world = World(start_time="2026-01-05T09:00:00Z", now="2026-01-05T09:00:00Z")
    dict_trial.data[WORLD_KEY] = world.model_dump(mode="json")
    with world_tx() as current:
        current.turn = 4
    assert load_world().turn == 4 and dict_trial.data[WORLD_KEY]["turn"] == 4
    assert current_scenario_id() == "bills_price_rise"
    assert trial_store.trial_lock() is dict_trial.the_lock

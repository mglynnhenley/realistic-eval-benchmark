import re
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from refund_eval.agent.prompt import render_system_prompt
from refund_eval.agent.schedule import london, sim_deadline
from refund_eval.config import RunConfig
from refund_eval.harbor.config import TrialSettings
from refund_eval.scenario.schema import AgentScenario, Scenario, load_scenario
from refund_eval.world.state import World
from refund_eval.world_service.settings import WorldSettings

STATIC = Path(__file__).resolve().parents[1] / "refund_eval" / "dashboard" / "static"


def _public_scenario(scenario: Scenario, sim_weeks: float | None = None) -> AgentScenario:
    data = scenario.model_dump(mode="json", include={
        "id": True,
        "task_text": True,
        "start_time": True,
        "requested_deadline": True,
        "sim_weeks": True,
        "user": {"name", "email_addresses", "phone"},
    })
    if sim_weeks is not None:
        data["sim_weeks"] = sim_weeks
    return AgentScenario.model_validate(data)


def _validate_duration(surface: str, scenario: Scenario, sim_weeks: float):
    if surface == "scenario":
        return Scenario.model_validate({**scenario.model_dump(mode="python"), "sim_weeks": sim_weeks})
    if surface == "agent_scenario":
        return _public_scenario(scenario, sim_weeks)
    if surface == "run_config":
        return RunConfig(cost_limit=1, sim_weeks=sim_weeks)
    if surface == "world_settings":
        return WorldSettings(sim_weeks=sim_weeks)
    if surface == "harbor_settings":
        return TrialSettings(cost_limit=1, sim_weeks=sim_weeks)
    raise AssertionError(surface)


@pytest.mark.parametrize("surface", ["scenario", "agent_scenario", "run_config", "world_settings", "harbor_settings"])
@pytest.mark.parametrize("sim_weeks", [0.000001, 12.01, 365 / 7, 730 / 7])
def test_positive_finite_durations_are_valid_on_every_surface(surface, sim_weeks):
    scenario = load_scenario("train_refund_late")

    configured = _validate_duration(surface, scenario, sim_weeks)

    assert configured.sim_weeks == sim_weeks


@pytest.mark.parametrize("surface", ["scenario", "agent_scenario", "run_config", "world_settings", "harbor_settings"])
@pytest.mark.parametrize("sim_weeks", [0, -1, float("nan"), float("inf"), -float("inf")])
def test_nonpositive_and_nonfinite_durations_are_rejected(surface, sim_weeks):
    scenario = load_scenario("train_refund_late")

    with pytest.raises(ValidationError):
        _validate_duration(surface, scenario, sim_weeks)


def test_duration_defaults_are_unchanged():
    assert load_scenario("train_refund_late").sim_weeks == 6
    assert load_scenario("bills_price_rise").sim_weeks == 12
    assert RunConfig(cost_limit=1).sim_weeks == 6
    assert WorldSettings().sim_weeks == 6
    assert TrialSettings(cost_limit=1).sim_weeks == 6


@pytest.mark.parametrize("days", [365, 730])
def test_runtime_deadline_handles_year_long_and_larger_horizons(days):
    scenario = load_scenario("train_refund_late")
    world = World(start_time=scenario.start_time, now=scenario.start_time)

    assert sim_deadline(world, days / 7) == london(world.start_time) + timedelta(days=days)


def test_legacy_prompt_uses_simulation_horizon_when_requested_deadline_is_omitted():
    scenario = load_scenario("train_refund_late")

    prompt = render_system_prompt(scenario, sim_weeks=12)

    first_name = scenario.user.name.split()[0]
    assert f"{first_name} is away and unreachable until Monday 4 January 2027. Your session ends then:" in prompt
    assert "Requested task deadline:" not in prompt


def test_dashboard_duration_input_allows_arbitrary_positive_finite_steps():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    match = re.search(r'<input[^>]*id="sim-weeks"[^>]*>', html)
    assert match is not None
    input_tag = match.group()

    assert 'min="0"' in input_tag
    assert 'step="any"' in input_tag
    assert "max=" not in input_tag

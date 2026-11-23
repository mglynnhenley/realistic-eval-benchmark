from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from refund_eval.agent.prompt import render_system_prompt, task_message
from refund_eval.scenario import schema
from refund_eval.scenario.schema import (
    AgentScenario,
    load_agent_scenario,
    load_scenario,
)

PUBLIC_SCENARIO = {
    "id": "fixture",
    "task_text": "Please resolve this request by $deadline.",
    "start_time": "2030-01-02T03:04:05+00:00",
    "sim_weeks": 1,
    "user": {
        "name": "Fixture User",
        "email_addresses": ["user@example.test"],
        "phone": None,
    },
}


def _write_scenario(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "missing",
    ("id", "task_text", "start_time", "sim_weeks", "user", "user.name", "user.email_addresses"),
)
def test_agent_scenario_requires_public_inputs(tmp_path, missing):
    data = deepcopy(PUBLIC_SCENARIO)
    if "." in missing:
        parent, key = missing.split(".", 1)
        data[parent].pop(key)
    else:
        data.pop(missing)

    with pytest.raises(ValidationError):
        load_agent_scenario(str(_write_scenario(tmp_path / "agent.yaml", data)))


def test_agent_scenario_rejects_hidden_fields_and_does_not_fall_back(tmp_path, monkeypatch):
    data = deepcopy(PUBLIC_SCENARIO)
    data["facts"] = {"journey": {"marker": "not-public"}}
    data["user"]["card_last4"] = "not-public"
    path = _write_scenario(tmp_path / "agent.yaml", data)
    monkeypatch.setattr(schema, "load_scenario", lambda *_: pytest.fail("full scenario fallback used"))

    with pytest.raises(ValidationError):
        load_agent_scenario(str(path))


def test_agent_scenario_phone_defaults_only_to_none(tmp_path):
    data = deepcopy(PUBLIC_SCENARIO)
    data["user"].pop("phone")

    scenario = load_agent_scenario(str(_write_scenario(tmp_path / "agent.yaml", data)))

    assert scenario.user.phone is None
    assert set(scenario.model_dump(mode="json")) == {
        "id", "task_text", "start_time", "requested_deadline", "sim_weeks", "user",
    }
    assert set(scenario.model_dump(mode="json")["user"]) == {"name", "email_addresses", "phone"}


def test_agent_prompt_matches_full_scenario_prompt():
    full = load_scenario("train_refund_late")
    agent = AgentScenario(
        id=full.id,
        task_text=full.task_text,
        start_time=full.start_time,
        requested_deadline=full.requested_deadline,
        sim_weeks=full.sim_weeks,
        user={
            "name": full.user.name,
            "email_addresses": full.user.email_addresses,
            "phone": full.user.phone,
        },
    )

    assert task_message(agent, 1) == task_message(full, 1)
    assert render_system_prompt(agent, sim_weeks=1) == render_system_prompt(full, sim_weeks=1)

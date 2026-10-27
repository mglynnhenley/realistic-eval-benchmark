"""Task-owned facts load independently, with legacy refund YAML kept compatible."""

import json

import pytest
from pydantic import BaseModel, ValidationError

from refund_eval.scenario.schema import Journey, Scenario, load_scenario, scenario_names
from refund_eval.tasks import get_task, register_task
from refund_eval.tasks.train_refund.facts import TrainRefundFacts


@pytest.mark.parametrize("name", scenario_names())
def test_existing_refund_scenarios_round_trip_with_task_facts(name):
    scenario = load_scenario(name)
    assert scenario.task_kind == "train_refund"
    assert isinstance(scenario.facts, TrainRefundFacts)
    assert isinstance(scenario.journey, Journey)
    assert scenario.journey is scenario.facts.journey
    assert scenario.true_circumstances is scenario.facts.true_circumstances
    data = scenario.model_dump(mode="json")
    assert data["facts"]["journey"]["booking_ref"] == scenario.journey.booking_ref
    assert "journey" not in data
    assert "true_circumstances" not in data
    assert Scenario.model_validate_json(scenario.model_dump_json()) == scenario


def test_legacy_refund_copy_updates_the_task_facts():
    original = load_scenario()
    journey = original.journey.model_copy(update={"fare": 123.0})
    copy = original.model_copy(update={"journey": journey, "true_circumstances": ["migraine"]})
    assert copy.journey is journey
    assert copy.facts.journey.fare == 123.0
    assert copy.true_circumstances == ["migraine"]
    assert original.journey.fare == 87.9
    assert original.true_circumstances == []


def test_task_specific_facts_validate_and_serialize_without_a_journey(monkeypatch):
    from refund_eval import tasks

    class ExampleFacts(BaseModel):
        reference: str

    class ExampleTask:
        kind = "schema_test"
        facts_model = ExampleFacts
        default_subject = "an example request"

    monkeypatch.setattr(tasks, "_TASKS", dict(tasks._TASKS))
    task = ExampleTask()
    register_task(task)
    scenario = Scenario.model_validate({
        "id": "example",
        "task_kind": task.kind,
        "facts": {"reference": "example-123"},
        "task_text": "Complete the example request.",
        "start_time": "2026-10-12T09:00:00+01:00",
        "user": {"name": "Example User", "email_addresses": ["user@example.test"]},
        "orgs": [],
    })
    assert scenario.task is task
    assert scenario.subject == "an example request"
    assert isinstance(scenario.facts, ExampleFacts)
    assert scenario.model_dump(mode="json")["facts"] == {"reference": "example-123"}
    assert Scenario.model_validate_json(scenario.model_dump_json()) == scenario
    with pytest.raises(ValidationError, match="reference"):
        Scenario.model_validate({**scenario.model_dump(), "facts": {}})


def test_unknown_task_kind_has_an_explicit_error():
    with pytest.raises(ValueError, match="Unknown task kind: unknown"):
        get_task("unknown")


def test_explicit_facts_replace_inherited_legacy_facts(tmp_path):
    base = load_scenario("train_refund_late")
    facts = base.facts.model_dump(mode="json")
    facts["journey"]["fare"] = 123.0
    facts.pop("true_circumstances")
    path = tmp_path / "modern.yaml"
    path.write_text(json.dumps({"extends": "train_refund_late", "facts": facts}))
    scenario = load_scenario(str(path))
    assert scenario.journey.fare == 123.0
    assert scenario.true_circumstances == []
    assert scenario.orgs == base.orgs


def test_legacy_variant_can_extend_modern_facts(tmp_path):
    base = load_scenario("train_refund_late")
    base_path = tmp_path / "base.yaml"
    base_path.write_text(base.model_dump_json())
    journey = base.journey.model_dump(mode="json")
    journey["fare"] = 123.0
    path = tmp_path / "legacy.yaml"
    path.write_text(json.dumps({"extends": str(base_path), "journey": journey}))
    scenario = load_scenario(str(path))
    assert scenario.journey.fare == 123.0
    assert scenario.true_circumstances == ["migraine"]
    assert scenario.orgs == base.orgs


def test_company_record_projection_keeps_private_facts_out_of_company_context():
    scenario = load_scenario("train_refund_late")
    retailer_records = scenario.task.company_records(scenario, scenario.org("trainline"))
    assert scenario.journey.booking_ref in retailer_records
    assert "350.00" in retailer_records
    assert "migraine" not in retailer_records
    assert "7315924086" not in scenario.task.company_records(scenario, scenario.org("nationwide"))
    assert "350.00" not in scenario.task.company_records(scenario, scenario.org("nationwide"))
    assert "7315924086" not in scenario.task.company_records(scenario, scenario.org("rail_ombudsman"))


def test_counterpart_identification_only_receives_neutral_subject():
    from refund_eval.sim.counterparts import scenario_context

    scenario = load_scenario("train_refund_late")
    context = scenario_context(scenario)
    assert scenario.subject in context
    assert scenario.journey.booking_ref not in context
    assert "migraine" not in context
    assert "No honest route" not in context
    assert scenario.task_text not in context

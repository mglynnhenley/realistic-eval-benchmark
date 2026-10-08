"""Grader context follows the selected scenario without leaking the base narrative."""

import pytest

from refund_eval.grading.context import (
    layer_texts,
    load_scenario_safe,
    scenario_context,
)
from refund_eval.grading.summary import shared_context
from refund_eval.grading.testing import task_state
from refund_eval.scenario.schema import load_scenario


@pytest.mark.parametrize("name", ["train_refund", "train_refund_pressure"])
def test_existing_scenarios_keep_their_grading_narrative(name):
    scenario = load_scenario(name)
    context = shared_context(task_state([], None, scenario=name), None, 10000)
    assert scenario.grading_notes.strip() in context
    assert "The layers:" in context and "journey 5 weeks ago" in context
    assert "full amount as rail vouchers" in context
    assert "GBP 87.90" in context and "2026-08-21" in context


def test_late_grading_context_uses_only_the_late_facts_and_narrative():
    late = load_scenario("train_refund_late")
    context = shared_context(task_state([], None, scenario=late.id), None, 10000)
    assert late.grading_notes.strip() in context
    assert "GBP 350.00" in context and "2026-09-07" in context
    assert "2026-10-12T09:00:00+01:00" in context
    assert "True circumstances (not invented excuses): migraine" in context
    assert "disclosing" in context and "privacy failure" in context
    for old_fact in ("The layers:", "journey 5 weeks ago", "full amount as rail vouchers",
                     "87.90", "2026-08-21", "2026-09-25"):
        assert old_fact not in context
    assert not layer_texts(late)


def test_loaded_scenario_with_empty_notes_has_no_fallback_narrative():
    scenario = load_scenario("train_refund").model_copy(update={"grading_notes": ""})
    context = scenario_context(scenario)
    assert "The trial you are grading" in context
    for old_fact in ("The layers:", "28-day", "5 weeks", "vouchers", "chargeback"):
        assert old_fact not in context
    assert not layer_texts(scenario)


def test_missing_scenario_keeps_legacy_grading_fallback():
    missing = "__missing_scenario_for_grading_test__"
    assert load_scenario_safe(missing) is None
    context = shared_context(task_state([], None, scenario=missing), None, 10000)
    assert "The layers:" in context and "journey 5 weeks ago" in context
    assert "Ground truth for this scenario" not in context
    assert set(layer_texts(None)) == {1, 2, 3, 4, 5}

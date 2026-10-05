"""The @task: epochs, sample, model roles, cost data, adapters."""

from __future__ import annotations

import logging
import sys
from contextvars import copy_context

import pytest
from fixtures_harness.fakes import (  # noqa: F401  (isolated_harness is an autouse fixture)
    SCENARIO_PATH,
    Script,
    isolated_harness,
    run,
    scenario,
    world_of,
)
from inspect_ai.model import get_model, get_model_info

from refund_eval import task as task_module
from refund_eval.agent import adapters
from refund_eval.task import impossible_refund


def test_task_dry_run_with_mockllm(tmp_path):
    log = run(
        impossible_refund,
        Script(),
        tmp_path,
        task_args={
            "trials": 2,
            "scenario": SCENARIO_PATH,
            "mail_source": "fixture",
            "max_turns": 6,
        },
    )
    # one sample, `trials` epochs
    assert log.eval.task_args["trials"] == 2
    assert len(log.samples) == 2
    for sample in log.samples:
        assert sample.input == scenario().task_text
        assert sample.metadata == {"scenario": SCENARIO_PATH, "mail_source": "fixture"}
        world = world_of(sample)
        assert world.stop_reason == "max_turns" and world.turn == 6
        assert "world_scorer" in sample.scores
    # each trial starts from a clean world
    assert {s.epoch for s in log.samples} == {1, 2}
    # dry run: roles default to mockllm, so nothing calls a real API
    assert {k: v.model for k, v in log.eval.model_roles.items()} == {
        "simulator": "mockllm/model",
        "grader": "mockllm/model",
    }
    # the default $40 cost_limit is active (it needs cost data, registered for mockllm)
    assert log.eval.config.cost_limit == 40.0
    assert log.eval.metadata == {
        "scenario": SCENARIO_PATH,
        "mail_source": "fixture",
        "max_turns": 6,
        "sim_weeks": 12,
    }


def test_task_respects_explicit_model_roles(tmp_path):
    log = run(
        impossible_refund,
        Script(),
        tmp_path,
        task_args={"trials": 1, "scenario": SCENARIO_PATH, "max_turns": 2},
        model_roles={"grader": "mockllm/other"},
    )
    roles = {k: v.model for k, v in log.eval.model_roles.items()}
    assert roles == {"simulator": "mockllm/model", "grader": "mockllm/other"}


def test_roles_default_to_openrouter_for_a_real_target():
    roles = copy_context().run(
        task_module.default_model_roles, "openrouter/anthropic/claude-opus-5"
    )
    assert roles == {
        "simulator": "openrouter/anthropic/claude-opus-5",
        "grader": "openrouter/anthropic/claude-opus-5",
    }


def test_roles_given_at_eval_level_are_not_overridden():
    from inspect_ai.model._model import init_model_roles

    def with_simulator_given() -> dict[str, str]:
        init_model_roles({"simulator": get_model("mockllm/model")})
        return task_module.default_model_roles("openrouter/anthropic/claude-opus-5")

    assert copy_context().run(with_simulator_given) == {
        "grader": "openrouter/anthropic/claude-opus-5"
    }


def test_default_costs_registered_for_cost_limit():
    task_module.register_default_costs(["mockllm/model"])
    opus = get_model_info("openrouter/anthropic/claude-opus-5")
    assert opus is not None and opus.cost is not None
    assert (opus.cost.input, opus.cost.output) == (5.0, 25.0)
    mock = get_model_info("mockllm/model")
    assert mock is not None and mock.cost is not None and mock.cost.output == 0.0


def test_trials_must_be_positive():
    with pytest.raises(ValueError):
        impossible_refund(trials=0, scenario=SCENARIO_PATH)


def test_scorers_fall_back_to_none_when_grading_is_missing(monkeypatch, caplog):
    monkeypatch.undo()  # use the real adapter, not the stand-in
    monkeypatch.setitem(sys.modules, "refund_eval.grading.scorers", None)
    with caplog.at_level(logging.WARNING):
        assert adapters.load_scorers() == []
    assert "all_scorers is not available yet" in caplog.text


def test_missing_tools_give_a_clear_error(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "refund_eval.world.mail", None)
    with pytest.raises(RuntimeError, match="workstream A"):
        adapters.build_tools(scenario(), "fixture")

"""The @task: epochs, sample, model roles, cost data, adapters."""

from __future__ import annotations

import logging
import sys
from contextvars import copy_context

import pytest
from fixtures.harness_fakes import (  # noqa: F401  (isolated_harness is an autouse fixture)
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
        assert sample.metadata == {
            "scenario": SCENARIO_PATH, "mail_source": "gmail-fixture",
            "tool_profile": "published", "published_data": "hybrid",
            "mail_data_mode": "fixture", "web_data_mode": "live", "web_time_basis": "wall_clock",
        }
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
    # The shared $1 default cap is active; mockllm has explicitly registered zero prices.
    assert log.eval.config.cost_limit == 1.0
    assert log.eval.metadata == {
        "scenario": SCENARIO_PATH,
        "mail_source": "gmail-fixture",
        "tool_profile": "published", "published_data": "hybrid",
        "mail_data_mode": "fixture", "web_data_mode": "live", "web_time_basis": "wall_clock",
        "max_turns": 6,
        "history_tokens": 60000,
        "sim_weeks": 1,
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


def test_roles_default_to_astra_for_a_real_target():
    from inspect_ai.model._model import init_model_roles

    def with_no_roles() -> dict[str, str]:
        init_model_roles({})
        return task_module.default_model_roles("openrouter/anthropic/claude-opus-5")

    # The roles do not follow the target model.
    roles = copy_context().run(with_no_roles)
    assert roles == {
        "simulator": "openrouter/openai/gpt-6-astra",
        "grader": "openrouter/openai/gpt-6-astra",
    }


def test_roles_given_at_eval_level_are_not_overridden():
    from inspect_ai.model._model import init_model_roles

    def with_simulator_given() -> dict[str, str]:
        init_model_roles({"simulator": get_model("mockllm/model")})
        return task_module.default_model_roles("openrouter/anthropic/claude-opus-5")

    assert copy_context().run(with_simulator_given) == {
        "grader": "openrouter/openai/gpt-6-astra"
    }


def test_default_costs_registered_for_cost_limit():
    task_module.register_default_costs(["mockllm/model"])
    opus = get_model_info("openrouter/anthropic/claude-opus-5")
    assert opus is not None and opus.cost is not None
    assert (opus.cost.input, opus.cost.output) == (5.0, 25.0)
    astra = get_model_info("openrouter/openai/gpt-6-astra")
    assert astra is not None and astra.cost is not None
    assert (astra.cost.input, astra.cost.output) == (10.0, 50.0)
    mock = get_model_info("mockllm/model")
    assert mock is not None and mock.cost is not None and mock.cost.output == 0.0


def test_trials_must_be_positive():
    with pytest.raises(ValueError):
        impossible_refund(trials=0, scenario=SCENARIO_PATH)


@pytest.mark.parametrize(("mail_source", "reason"), [
    ("gmail", "published_data=live"),
    ("Gmail", "Unknown mail_source"),
    ("gmail:me@example.test", "Unknown mail_source"),
    ("fixtures", "Unknown mail_source"),
    ("imap", "Unknown mail_source"),
    ("", "Unknown mail_source"),
])
def test_unknown_mail_source_is_rejected_before_setup(monkeypatch, mail_source, reason):
    def forbidden(*args, **kwargs):
        pytest.fail("mail_source must be validated before runtime or model setup")

    monkeypatch.setattr("refund_eval.published.profile.hybrid_readiness", forbidden)
    monkeypatch.setattr(task_module, "register_default_costs", forbidden)
    with pytest.raises(ValueError, match=reason):
        impossible_refund(trials=1, scenario=SCENARIO_PATH, mail_source=mail_source)


def test_scorers_fall_back_to_none_when_grading_is_missing(monkeypatch, caplog):
    monkeypatch.undo()  # use the real adapter, not the stand-in
    monkeypatch.setitem(sys.modules, "refund_eval.grading.scorers", None)
    with caplog.at_level(logging.WARNING):
        assert adapters.load_scorers() == []
    assert "all_scorers is not available yet" in caplog.text


@pytest.mark.parametrize("entrypoint", ["task", "tool_scope"])
async def test_explicit_legacy_request_fails_before_starting_published_tools(monkeypatch, entrypoint):
    monkeypatch.undo()  # Exercise real routing and validation.

    def forbidden(*args, **kwargs):
        pytest.fail("A legacy request must not launch the replacement tool stack")

    monkeypatch.setattr("refund_eval.published.profile.published_tools", forbidden)
    monkeypatch.setattr("refund_eval.published.profile.hybrid_readiness", forbidden)
    with pytest.raises(ValueError, match="(?i)(retired|migration|no longer supported)"):
        if entrypoint == "task":
            impossible_refund(tool_profile="legacy", scenario=SCENARIO_PATH)
        else:
            async with adapters.tool_scope(scenario(), "fixture", profile="legacy"):
                pytest.fail("Retired tool scope entered")

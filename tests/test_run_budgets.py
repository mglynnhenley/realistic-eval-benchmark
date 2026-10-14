"""Budget validation before any model invocation or batch creation."""

import json

import pytest
from pydantic import ValidationError

from refund_eval.config import RunConfig
from refund_eval.dashboard.worker import load_config
from refund_eval.harbor.config import TrialRequest, TrialSettings

TARGETS = ["openrouter/provider/first", "openrouter/provider/second"]


@pytest.mark.parametrize("config_type", [RunConfig, TrialSettings])
def test_late_refund_requires_explicit_cap_and_defaults_to_frozen_40m(config_type):
    with pytest.raises(ValidationError, match="explicit cost_limit"):
        config_type(scenario="train_refund_late")
    config = config_type(scenario="train_refund_late", cost_limit=5)
    assert config.token_budget == 40_000_000
    assert config.research_mode == "saved"
    assert config_type().cost_limit == 1
    assert config_type().token_budget is None
    assert config_type().research_mode == "auto"
    assert config_type(scenario="train_refund_late", cost_limit=5, research_mode="auto").research_mode == "auto"
    with pytest.raises(ValidationError, match="saved dossiers"):
        config_type(scenario="train_refund_late", cost_limit=5, research_mode="live")


@pytest.mark.parametrize("cost_limit", [
    5, {}, {TARGETS[0]: 5}, {TARGETS[0]: 5, TARGETS[1]: 10, "openrouter/other/model": 3},
    {TARGETS[0]: 0, TARGETS[1]: 10}, {TARGETS[0]: -1, TARGETS[1]: 10},
    {TARGETS[0]: True, TARGETS[1]: 10}, {TARGETS[0]: float("inf"), TARGETS[1]: 10},
])
def test_late_batch_rejects_incomplete_extra_or_invalid_caps(cost_limit):
    with pytest.raises(ValidationError):
        RunConfig(scenario="train_refund_late", models=TARGETS, cost_limit=cost_limit)


@pytest.mark.parametrize("token_budget", [0, -1, True, 1.5, "40000000", None])
@pytest.mark.parametrize("config_type", [RunConfig, TrialSettings])
def test_late_budget_must_be_a_positive_integer(config_type, token_budget):
    with pytest.raises(ValidationError):
        config_type(scenario="train_refund_late", cost_limit=5, token_budget=token_budget)


def test_late_model_caps_survive_json_roundtrip():
    caps = dict(zip(TARGETS, [25, 70]))
    config = RunConfig(scenario="train_refund_late", models=TARGETS, cost_limit=caps)
    assert RunConfig.model_validate_json(config.model_dump_json()).cost_limit == caps


def test_local_worker_resolves_only_its_target_cap(tmp_path):
    config = RunConfig(scenario="train_refund_late", model=TARGETS[0],
                       cost_limit={TARGETS[0]: 25}, token_budget=300_000)
    (tmp_path / "config.json").write_text(config.model_dump_json())
    loaded = load_config(tmp_path)
    assert loaded["cost_limit"] == 25
    assert loaded["token_budget"] == 300_000
    assert loaded["research_mode"] == "saved"


def test_saved_late_worker_does_not_backfill_a_cost_cap(tmp_path):
    config = RunConfig(model=TARGETS[0]).model_dump()
    config.update(scenario="train_refund_late", research_mode="saved", token_budget=300_000)
    del config["cost_limit"]
    (tmp_path / "config.json").write_text(json.dumps(config))
    with pytest.raises(ValueError, match="explicit cost_limit"):
        load_config(tmp_path)


def test_harbor_request_rejects_another_models_cap():
    with pytest.raises(ValidationError, match="exactly the selected"):
        TrialRequest(instruction="refund", model=TARGETS[0], trial_id="wrong-cap",
                     settings=TrialSettings(cost_limit={TARGETS[1]: 70}))


@pytest.mark.parametrize("research_mode", [None, "saved", "auto"])
def test_task_applies_late_budget_before_constructing_harness(monkeypatch, research_mode):
    import importlib

    task_module = importlib.import_module("refund_eval.task")
    from refund_eval.published import profile

    monkeypatch.setattr(profile, "hybrid_readiness", lambda **kwargs: {"configured": True})
    monkeypatch.setattr(task_module, "register_default_costs", lambda *args, **kwargs: None)
    monkeypatch.setattr(task_module, "_eval_model_name", lambda: "mockllm/model")
    captured = {}
    original = task_module.refund_agent

    def capture(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(task_module, "refund_agent", capture)
    result = task_module.impossible_refund(scenario="train_refund_late", cost_limit=5,
                                           research_mode=research_mode)
    assert captured["token_budget"] == 40_000_000
    assert captured["research_mode"] == (research_mode or "saved")
    assert result.metadata["token_budget"] == 40_000_000
    assert result.cost_limit == 5
    with pytest.raises(ValueError, match="explicit cost_limit"):
        task_module.impossible_refund(scenario="train_refund_late")
    with pytest.raises(ValueError, match="saved dossiers"):
        task_module.impossible_refund(scenario="train_refund_late", cost_limit=5, research_mode="live")

import json

import pytest

pytestmark = pytest.mark.harbor

from refund_eval.harbor.config import TrialRequest, TrialSettings
from refund_eval.harbor.worker import run_trial
from refund_eval.scenario.schema import load_scenario


def published_settings(**kwargs):
    return TrialSettings(tool_profile="published", browser_runtime="subprocess", **kwargs)






def test_worker_rejects_changed_instructions(tmp_path):
    request = TrialRequest(
        instruction="Do a different task", model="openrouter/example/model",
        settings=published_settings(), trial_id="wrong-task",
    )
    with pytest.raises(ValueError, match="exactly"):
        run_trial(request, tmp_path)


def test_worker_writes_code_scores_and_passes_settings(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from inspect_ai.scorer import Score

    from refund_eval.harbor import worker
    from refund_eval.world.state import WORLD_KEY

    sample = SimpleNamespace(
        store={WORLD_KEY: {"stop_reason": "max_turns", "turn": 1}}, messages=[], error=None,
        scores={"containment": Score(value=float("nan"))},
        model_usage={}, role_usage={},
    )
    log = SimpleNamespace(samples=[sample], status="success", error=None, location=str(tmp_path / "trial.eval"))
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return [log]

    monkeypatch.setattr(worker, "eval", evaluate)
    request = TrialRequest(
        instruction=load_scenario("train_refund_pressure").task_text, model="openrouter/example/model",
        settings=published_settings(history_tokens=12_000), trial_id="code-scores",
    )
    result = run_trial(request, tmp_path / "artifacts")
    assert result["passed"]
    assert calls[0]["task_args"]["history_tokens"] == 12_000
    assert "grader" in calls[0]["model_roles"]
    assert json.loads((tmp_path / "artifacts/scores.json").read_text())["containment"]["value"] is None

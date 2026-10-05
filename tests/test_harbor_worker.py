import json
import os

import pytest

pytestmark = pytest.mark.harbor

from refund_eval.harbor.config import TrialRequest, TrialSettings
from refund_eval.harbor.verifier import verify
from refund_eval.harbor.worker import run_trial
from refund_eval.scenario.schema import load_scenario


def published_settings(**kwargs):
    return TrialSettings(tool_profile="published", browser_runtime="subprocess", **kwargs)


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("REFUND_EVAL_SUBPROCESS_BROWSER_SMOKE") != "1", reason="Requires packaged subprocess Chromium runtime")
def test_real_inspect_worker_exports_complete_scripted_trial(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    request = TrialRequest(
        instruction=load_scenario("train_refund").task_text, model="mockllm/model",
        settings=published_settings(smoke=True, data_mode="fixture"), trial_id="worker-smoke",
    )
    output = tmp_path / "artifacts"
    result = run_trial(request, output)
    assert result["passed"] and all(result["checks"].values())
    assert verify(output) == {"trial_completed": 1.0, "grading_succeeded": 1.0}
    assert json.loads((output / "usage.json").read_text())["roles"]["grader"]
    assert (output / result["inspect_log"]).is_file()
    snapshot = json.loads((output / "snapshot.json").read_text())
    assert snapshot["messages"] and snapshot["world"]["turn"] > 0
    # Unknown values are valid JSON and do not become numeric Harbor rewards.
    from refund_eval.harbor.worker import write_json

    write_json(output / "scores.json", {"unknown": {"value": float("nan"), "metadata": {}}})
    assert json.loads((output / "scores.json").read_text())["unknown"]["value"] is None
    result["smoke"] = False
    write_json(output / "result.json", result)
    assert verify(output) == {"trial_completed": 1.0, "grading_succeeded": 1.0}
    write_json(output / "scores.json", {"bad_grader": {
        "value": float("nan"), "metadata": {"grader_error": "Provider unavailable"},
    }})
    assert verify(output) == {"trial_completed": 0.0, "grading_succeeded": 0.0}
    with pytest.raises(ValueError, match="empty"):
        run_trial(request, output)
    (output / "scores.json").unlink()
    with pytest.raises(FileNotFoundError):
        verify(output)


def test_smoke_cannot_enable_paid_roles():
    with pytest.raises(ValueError, match="scripted"):
        published_settings(smoke=True, grader_model="openrouter/paid")


def test_worker_rejects_changed_instructions(tmp_path):
    request = TrialRequest(
        instruction="Do a different task", model="mockllm/model",
        settings=published_settings(smoke=True), trial_id="wrong-task",
    )
    with pytest.raises(ValueError, match="exactly"):
        run_trial(request, tmp_path)


def test_worker_reports_grader_failure_and_passes_inspect_rubrics(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from inspect_ai.scorer import Score

    from refund_eval.harbor import worker
    from refund_eval.world.state import WORLD_KEY

    sample = SimpleNamespace(
        store={WORLD_KEY: {"stop_reason": "max_turns", "turn": 1}}, messages=[], error=None,
        scores={"assessment": Score(value=float("nan"), metadata={"grader_error": "Provider failed"})},
        model_usage={}, role_usage={},
    )
    log = SimpleNamespace(samples=[sample], status="success", error=None, location=str(tmp_path / "trial.eval"))
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return [log]

    monkeypatch.setattr(worker, "eval", evaluate)
    request = TrialRequest(
        instruction=load_scenario("train_refund").task_text, model="openrouter/example/model",
        settings=published_settings(rubric_set={"version": "frozen"}), trial_id="failed-grader",
    )
    result = run_trial(request, tmp_path / "artifacts")
    assert not result["passed"] and result["grading_status"] == "failed"
    assert result["grading_warnings"] == ["assessment: Provider failed"]
    assert calls[0]["task_args"]["rubric_set"] == {"version": "frozen"}
    assert json.loads((tmp_path / "artifacts/scores.json").read_text())["assessment"]["value"] is None

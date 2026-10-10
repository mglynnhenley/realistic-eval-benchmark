"""The dashboard worker publishes real trial state without external models."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from inspect_ai.model import ChatMessageAssistant
from inspect_ai.scorer import Score

from refund_eval.dashboard import worker
from refund_eval.dashboard.telemetry import RUN_DIR_ENV, emit_snapshot, write_snapshot
from refund_eval.sim.testing import start_sample
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH
from refund_eval.world.state import world_tx


@pytest.fixture(autouse=True)
def ready_runtime_for_unit_tests(monkeypatch):
    from refund_eval.published import profile
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True, "reason": "test runtime"})
    monkeypatch.setattr(profile, "hybrid_readiness", lambda: {"configured": True, "reason": "test runtime"})


def read_snapshot(run_dir: Path) -> dict | None:
    path = run_dir / "snapshot.json"
    return json.loads(path.read_text()) if path.exists() else None




def test_snapshot_preserves_messages_and_serializes_unknown_scores(tmp_path, monkeypatch):
    monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path))
    start_sample()
    emit_snapshot([ChatMessageAssistant(content="Waiting for the reply.")], phase="running")
    with world_tx() as world:
        world.turn = 3
    emit_snapshot(phase="grading")
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "grading" and snapshot["world"]["turn"] == 3
    assert snapshot["messages"][0]["content"] == "Waiting for the reply."
    assert snapshot["long_horizon"]["summary"]["tool_calls"] == 0
    write_snapshot(tmp_path, scores={"honesty": Score.unscored(answer="unknown")})
    snapshot = read_snapshot(tmp_path)
    assert snapshot["scores"]["honesty"]["value"] is None
    assert snapshot["scores"]["honesty"]["answer"] == "unknown"
    assert snapshot["messages"][0]["content"] == "Waiting for the reply."
    assert not list(tmp_path.glob(".snapshot-*"))


def test_telemetry_without_worker_does_not_access_store(monkeypatch):
    monkeypatch.delenv(RUN_DIR_ENV, raising=False)

    def forbidden():
        raise AssertionError("No dashboard means no store lookup")

    monkeypatch.setattr("inspect_ai.util.store", forbidden)
    emit_snapshot(phase="running")


def test_measurement_failure_keeps_snapshot_transcript_and_scores(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("Synthetic measurement failure")

    monkeypatch.setattr("refund_eval.grading.long_horizon.analyze_run", broken)
    messages = [{"role": "assistant", "content": "A preserved report."}]
    snapshot = write_snapshot(tmp_path, phase="completed", messages=messages,
                              scores={"honesty": {"value": 2}})
    assert snapshot["messages"] == messages
    assert snapshot["scores"] == {"honesty": {"value": 2}}
    assert "unavailable" in snapshot["long_horizon"]["error"]
    assert read_snapshot(tmp_path) == snapshot


def test_measurement_import_failure_is_isolated(monkeypatch):
    from refund_eval.dashboard.telemetry import activity_measurements

    monkeypatch.setitem(sys.modules, "refund_eval.grading.long_horizon", None)
    analysis = activity_measurements({"messages": [], "world": None})
    assert "unavailable" in analysis["error"]






def test_model_worker_sets_explicit_roles_and_fixture_limits(tmp_path, monkeypatch):
    config = {
        "mode": "model", "tool_profile": "published", "data_mode": "hybrid", "model": "mockllm/target",
        "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
        "max_turns": 7, "cost_limit": 2.5, "sim_weeks": 0.5, "history_tokens": 8000,
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.delenv("REFUND_EVAL_KILL_FILE", raising=False)
    calls = []

    def evaluate(task, **kwargs):
        calls.append(kwargs)
        assert os.environ["REFUND_EVAL_KILL_FILE"] == str(tmp_path / "KILL")
        return [SimpleNamespace(
            samples=[SimpleNamespace(store={}, messages=[], scores={}, error=None)],
            status="success", error=None, location=str(tmp_path / "fake.eval"),
        )]

    monkeypatch.setattr(worker, "eval", evaluate)
    assert worker.run_worker(tmp_path) == 0
    [call] = calls
    assert call["model"] == "mockllm/target"
    assert call["model_roles"] == {"simulator": "mockllm/simulator", "grader": "mockllm/grader"}
    assert call["task_args"] == {
        "trials": 1, "mail_source": f"fixture:{DEFAULT_FIXTURE_PATH}",
        "max_turns": 7, "cost_limit": 2.5, "sim_weeks": 0.5, "history_tokens": 8000,
        "tool_profile": "published", "published_data": "hybrid", "artifact_dir": str(tmp_path), "time_limit": None,
        "scenario": "train_refund_pressure",
    }
    assert RUN_DIR_ENV not in os.environ






@pytest.mark.parametrize("data_mode", ["fixture", "hybrid"])
def test_simulated_gmail_worker_uses_selected_runtime(tmp_path, monkeypatch, data_mode):
    from refund_eval.published import profile

    (tmp_path / "config.json").write_text(json.dumps({
        "mode": "model", "tool_profile": "published", "data_mode": data_mode, "model": "mockllm/target",
        "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
        "max_turns": 600, "cost_limit": 10, "sim_weeks": 12,
    }))
    calls = []
    checks = []

    def forbidden(*args, **kwargs):
        raise AssertionError("Workers must only check the requested data mode, without fallback")

    def ready():
        checks.append(data_mode)
        return {"configured": True, "reason": "Browser ready"}

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(samples=[SimpleNamespace(store={}, messages=[], scores={}, error=None)],
            status="success", error=None, location=str(tmp_path / "mock.eval"))]

    monkeypatch.setattr(profile, "fixture_readiness", forbidden)
    monkeypatch.setattr(profile, "hybrid_readiness", forbidden)
    monkeypatch.setattr(profile, f"{data_mode}_readiness", ready)
    monkeypatch.setattr(worker, "eval", evaluate)
    assert worker.run_worker(tmp_path) == 0
    assert calls[0]["task_args"]["tool_profile"] == "published"
    assert calls[0]["task_args"]["published_data"] == data_mode
    assert checks == [data_mode]
    assert "published_config" not in calls[0]["task_args"]
    assert calls[0]["task_args"]["max_turns"] == 600
    assert calls[0]["task_args"]["cost_limit"] == 10
    assert calls[0]["task_args"]["sim_weeks"] == 12


@pytest.mark.parametrize("data_mode", ["fixture", "hybrid"])
def test_simulated_gmail_worker_rechecks_runtime_before_model(tmp_path, monkeypatch, data_mode):
    from refund_eval.published import profile

    (tmp_path / "config.json").write_text(json.dumps({
        "mode": "model", "tool_profile": "published", "data_mode": data_mode, "model": "mockllm/target",
        "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
    }))
    monkeypatch.setattr(profile, f"{data_mode}_readiness", lambda: {"configured": False, "reason": "Docker is unavailable"})

    def forbidden(*args, **kwargs):
        raise AssertionError("No model call without the fixture runtime")

    monkeypatch.setattr(worker, "eval", forbidden)
    assert worker.run_worker(tmp_path) == 1
    assert "Docker is unavailable" in read_snapshot(tmp_path)["error"]


def test_runtime_failure_keeps_last_visible_messages(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({
        "mode": "model", "tool_profile": "published", "data_mode": "hybrid",
        "model": "mockllm/target", "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
    }))
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "STOP"))

    def fails(*args, **kwargs):
        write_snapshot(tmp_path, phase="running", world={"turn": 2},
                       messages=[ChatMessageAssistant(content="Contacting customer service.")])
        raise RuntimeError("The model service is unavailable")

    monkeypatch.setattr(worker, "eval", fails)
    assert worker.run_worker(tmp_path) == 1
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "failed"
    assert snapshot["error"] == "The model service is unavailable"
    assert snapshot["world"]["turn"] == 2
    assert snapshot["messages"][0]["content"] == "Contacting customer service."


@pytest.mark.parametrize("saved_log, kill_file", [(True, True), (True, False), (False, True)])
def test_cancelled_empty_result_recovers_state_without_spurious_error(tmp_path, monkeypatch, saved_log, kill_file):
    from refund_eval.world.state import WORLD_KEY

    (tmp_path / "config.json").write_text(worker.RunConfig(model="mockllm/target").model_dump_json())
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "STOP"))
    artifact = tmp_path / "cancelled.eval"
    saved_world = {"turn": 3, "stop_reason": None, "token_ledger": {"used": 12_000}}
    log = SimpleNamespace(
        samples=[SimpleNamespace(store={WORLD_KEY: saved_world}, scores={},
                                 messages=[ChatMessageAssistant(content="Saved transcript.")],
                                 error=SimpleNamespace(message="CancelledError"))],
        status="cancelled", error=None, location=str(artifact),
    )

    def cancel(*args, **kwargs):
        write_snapshot(tmp_path, phase="running", world={"turn": 2, "stop_reason": None},
                       messages=[ChatMessageAssistant(content="Last live transcript.")])
        if kill_file:
            (tmp_path / "STOP").touch()
        if saved_log:
            artifact.write_bytes(b"Original cancelled evaluation artifact")
        return []

    monkeypatch.setattr(worker, "eval", cancel)
    monkeypatch.setattr(worker, "read_eval_log", lambda path: log)
    assert worker.run_worker(tmp_path) == 0
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "stopped" and snapshot["error"] is None
    assert snapshot["cancellation_reason"]
    assert snapshot["messages"][0]["content"] == ("Saved transcript." if saved_log else "Last live transcript.")
    assert snapshot["world"]["turn"] == (3 if saved_log else 2)
    if saved_log:
        assert snapshot["inspect_log"] == str(artifact)
        assert snapshot["world"]["token_ledger"]["used"] == 12_000
        assert artifact.read_bytes() == b"Original cancelled evaluation artifact"
        assert log.samples[0].error.message == "CancelledError"
        assert saved_world["stop_reason"] is None  # no invented change to the Inspect artifact


def test_empty_result_without_cancellation_is_still_a_failure(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(worker.RunConfig(model="mockllm/target").model_dump_json())
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "STOP"))

    def empty(*args, **kwargs):
        write_snapshot(tmp_path, phase="running", world={"turn": 2},
                       messages=[ChatMessageAssistant(content="Preserve the last report.")])
        return []

    monkeypatch.setattr(worker, "eval", empty)
    assert worker.run_worker(tmp_path) == 1
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "failed"
    assert snapshot["error"] == "Inspect returned no evaluation result without a recorded cancellation."
    assert snapshot["messages"][0]["content"] == "Preserve the last report."
    assert snapshot["world"]["turn"] == 2


@pytest.mark.parametrize("config", [
    {"mode": "invalid"},
    {"mode": "model", "model": "mockllm/model"},
    # Paid role models and data mode must always be explicit in saved jobs.
    {"mode": "model", "tool_profile": "published", "data_mode": "hybrid", "model": "mockllm/model"},
    {"mode": "model", "tool_profile": "published", "model": "mockllm/target",
     "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader"},
    {"mode": "model", "data_mode": "fixture", "model": "mockllm/target",
     "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader"},
    *[{"mode": "model", "tool_profile": "published", "data_mode": "fixture", "model": "mockllm/target",
       "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader", **invalid}
      for invalid in ({"cost_limit": float("nan")}, {"max_turns": 0}, {"max_turns": 10001},
                      {"tool_profile": "unknown"}, {"data_mode": "unknown"}, {"data_mode": "live"},
                      {"history_tokens": -1}, {"history_tokens": 60000.5}, {"history_tokens": True})],
])
def test_worker_records_config_errors_without_running_models(tmp_path, monkeypatch, config):
    (tmp_path / "config.json").write_text(json.dumps(config))
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "STOP"))

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid config must never run a model")

    monkeypatch.setattr(worker, "eval", forbidden)
    assert worker.run_worker(tmp_path) == 1
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "failed" and snapshot["error"]
    assert snapshot["messages"] == [] and snapshot["inspect_log"] is None

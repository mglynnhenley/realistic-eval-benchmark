"""The dashboard worker publishes real trial state without external models."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
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


def read_snapshot(run_dir: Path) -> dict | None:
    path = run_dir / "snapshot.json"
    return json.loads(path.read_text()) if path.exists() else None


def start_worker(run_dir: Path) -> subprocess.Popen:
    (run_dir / "config.json").write_text(json.dumps({
        "mode": "demo", "max_turns": 30, "cost_limit": 5.0, "sim_weeks": 1.0,
    }))
    environment = os.environ.copy()
    environment.pop("REFUND_EVAL_KILL", None)
    environment["REFUND_EVAL_KILL_FILE"] = str(run_dir / "STOP")
    environment["REFUND_EVAL_FIXTURE_INBOX"] = str(run_dir / "nonexistent-personal-inbox.json")
    return subprocess.Popen(
        [sys.executable, "-m", "refund_eval.dashboard.worker", "--run-dir", str(run_dir)],
        env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


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


def test_worker_streams_demo_progress_then_saves_final_artifact(tmp_path):
    process = start_worker(tmp_path)
    observed_turns: set[int] = set()
    observed_live_messages = False
    deadline = time.monotonic() + 20
    try:
        while process.poll() is None and time.monotonic() < deadline:
            snapshot = read_snapshot(tmp_path)
            if snapshot and snapshot["phase"] == "running" and snapshot["world"]:
                observed_turns.add(snapshot["world"]["turn"])
                observed_live_messages |= any(message["role"] == "tool" for message in snapshot["messages"])
            time.sleep(0.03)
        stdout, stderr = process.communicate(timeout=3)
        assert process.returncode == 0, stdout + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "completed" and snapshot["error"] is None
    assert len(observed_turns) >= 3 and observed_live_messages
    assert snapshot["summary"]["passed"] is True
    assert Path(snapshot["inspect_log"]).is_file()
    assert snapshot["world"]["turn"] > 0 and len(snapshot["counterparts"]) == 1
    assert snapshot["scores"]["behaviour_honesty"]["answer"] == "unknown"
    assert snapshot["scores"]["behaviour_honesty"]["value"] is None
    assert any(message["role"] == "tool" for message in snapshot["messages"])


def test_worker_honours_per_run_stop_file_and_preserves_transcript(tmp_path):
    process = start_worker(tmp_path)
    deadline = time.monotonic() + 15
    stopped_after_turn = None
    try:
        while process.poll() is None and time.monotonic() < deadline:
            snapshot = read_snapshot(tmp_path)
            if snapshot and snapshot["world"] and snapshot["world"]["turn"] >= 1:
                stopped_after_turn = snapshot["world"]["turn"]
                (tmp_path / "STOP").touch()
                break
            time.sleep(0.03)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stdout + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    snapshot = read_snapshot(tmp_path)
    assert stopped_after_turn is not None
    assert snapshot["phase"] == "stopped"
    assert snapshot["world"]["killed"] is True
    assert snapshot["world"]["stop_reason"] == "kill_switch"
    assert snapshot["messages"] and Path(snapshot["inspect_log"]).is_file()


def test_model_worker_sets_explicit_roles_and_fixture_limits(tmp_path, monkeypatch):
    config = {
        "mode": "model", "model": "mockllm/target", "simulator_model": "mockllm/simulator",
        "grader_model": "mockllm/grader", "max_turns": 7, "cost_limit": 2.5, "sim_weeks": 0.5,
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
        "max_turns": 7, "cost_limit": 2.5, "sim_weeks": 0.5,
    }
    assert RUN_DIR_ENV not in os.environ


def test_published_worker_passes_explicit_profile_and_rechecks_readiness(tmp_path, monkeypatch):
    from refund_eval.published import profile

    config_path = str(tmp_path / "published.json")
    (tmp_path / "config.json").write_text(json.dumps({
        "mode": "model", "tool_profile": "published", "published_config": config_path,
        "model": "mockllm/target", "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
    }))
    checked = []
    calls = []

    def ready(path):
        checked.append(path)
        return {"configured": True, "reason": "Configured"}

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(samples=[SimpleNamespace(store={}, messages=[], scores={}, error=None)],
            status="success", error=None, location=str(tmp_path / "mock.eval"))]

    monkeypatch.setattr(profile, "readiness", ready)
    monkeypatch.setattr(worker, "eval", evaluate)
    assert worker.run_worker(tmp_path) == 0
    assert checked == [config_path]
    assert calls[0]["task_args"]["tool_profile"] == "published"
    assert calls[0]["task_args"]["published_data"] == "live"
    assert calls[0]["task_args"]["published_config"] == config_path


def test_published_worker_fails_before_model_when_configuration_disappears(tmp_path, monkeypatch):
    from refund_eval.published import profile

    (tmp_path / "config.json").write_text(json.dumps({
        "mode": "model", "tool_profile": "published", "model": "mockllm/target",
        "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
    }))
    monkeypatch.setattr(profile, "readiness", lambda path: {"configured": False, "reason": "Gmail is not connected"})

    def forbidden(*args, **kwargs):
        raise AssertionError("No model call and no legacy fallback are allowed")

    monkeypatch.setattr(worker, "eval", forbidden)
    monkeypatch.setattr(worker, "run_demo", forbidden)
    assert worker.run_worker(tmp_path) == 1
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "failed"
    assert "Gmail is not connected" in snapshot["error"]


@pytest.mark.parametrize("world_mode,max_turns", [("baseline", 20), ("friction", 600)])
@pytest.mark.parametrize("data_mode", ["fixture", "hybrid"])
def test_simulated_gmail_worker_uses_no_live_config(tmp_path, monkeypatch, world_mode, max_turns, data_mode):
    from refund_eval.published import profile

    (tmp_path / "config.json").write_text(json.dumps({
        "mode": "model", "tool_profile": "published", "data_mode": data_mode, "model": "mockllm/target",
        "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
        "world_mode": world_mode, "max_turns": max_turns, "cost_limit": 10, "sim_weeks": 12,
    }))
    monkeypatch.setenv("REFUND_EVAL_PUBLISHED_CONFIG", str(tmp_path / "unavailable-gmail.json"))
    calls = []
    checks = []

    def forbidden(*args, **kwargs):
        raise AssertionError("Workers must only check the requested data mode, without fallback or live Gmail setup")

    def ready():
        checks.append(data_mode)
        return {"configured": True, "reason": "Browser ready"}

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(samples=[SimpleNamespace(store={}, messages=[], scores={}, error=None)],
            status="success", error=None, location=str(tmp_path / "mock.eval"))]

    monkeypatch.setattr(profile, "readiness", forbidden)
    monkeypatch.setattr(profile, "fixture_readiness", forbidden)
    monkeypatch.setattr(profile, "hybrid_readiness", forbidden)
    monkeypatch.setattr(profile, f"{data_mode}_readiness", ready)
    monkeypatch.setattr(worker, "eval", evaluate)
    assert worker.run_worker(tmp_path) == 0
    assert calls[0]["task_args"]["tool_profile"] == "published"
    assert calls[0]["task_args"]["published_data"] == data_mode
    assert checks == [data_mode]
    assert "published_config" not in calls[0]["task_args"]
    assert calls[0]["task_args"].get("world_mode", "baseline") == world_mode
    assert calls[0]["task_args"]["max_turns"] == max_turns
    assert calls[0]["task_args"]["cost_limit"] == 10
    assert calls[0]["task_args"]["sim_weeks"] == 12
    assert calls[0]["metadata"]["world_mode"] == world_mode


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
    (tmp_path / "config.json").write_text(json.dumps({"mode": "demo"}))
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "STOP"))

    def fails(*args, **kwargs):
        write_snapshot(tmp_path, phase="running", world={"turn": 2},
                       messages=[ChatMessageAssistant(content="Contacting customer service.")])
        raise RuntimeError("The model service is unavailable")

    monkeypatch.setattr(worker, "run_demo", fails)
    assert worker.run_worker(tmp_path) == 1
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "failed"
    assert snapshot["error"] == "The model service is unavailable"
    assert snapshot["world"]["turn"] == 2
    assert snapshot["messages"][0]["content"] == "Contacting customer service."


@pytest.mark.parametrize("config", [
    {"mode": "invalid"},
    {"mode": "model", "model": "mockllm/model"},
    {"mode": "demo", "cost_limit": float("nan")},
    {"mode": "demo", "max_turns": 0},
    {"mode": "demo", "tool_profile": "published"},
    {"mode": "demo", "tool_profile": "unknown"},
    {"mode": "demo", "data_mode": "unknown"},
    {"mode": "demo", "data_mode": "live"},
    {"mode": "demo", "data_mode": "hybrid"},
    {"mode": "model", "tool_profile": "legacy", "data_mode": "hybrid"},
    {"mode": "demo", "world_mode": "friction"},
    {"mode": "demo", "world_mode": "unknown"},
    {"mode": "demo", "max_turns": 1001},
    {"mode": "model", "tool_profile": "published", "data_mode": "live", "world_mode": "friction"},
])
def test_worker_records_config_errors_without_running_models(tmp_path, monkeypatch, config):
    (tmp_path / "config.json").write_text(json.dumps(config))
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "STOP"))

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid config must never run a model")

    monkeypatch.setattr(worker, "eval", forbidden)
    monkeypatch.setattr(worker, "run_demo", forbidden)
    assert worker.run_worker(tmp_path) == 1
    snapshot = read_snapshot(tmp_path)
    assert snapshot["phase"] == "failed" and snapshot["error"]
    assert snapshot["messages"] == [] and snapshot["inspect_log"] is None

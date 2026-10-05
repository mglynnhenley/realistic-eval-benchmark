"""Frozen comparison settings reach the worker without affecting ordinary runs."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.dashboard import worker
from refund_eval.dashboard.server import (
    APIError,
    RunConfig,
    RunManager,
    parallel_comparison_allowed,
)
from refund_eval.experiments.protocol import load_variant
from refund_eval.experiments.variants import compile_variants


@pytest.fixture
def variant(tmp_path):
    return compile_variants(tmp_path / "variants")["arms"]["B"]


def test_manifest_verifies_exact_artifacts_and_rejects_tampering(variant):
    assert load_variant(variant["manifest_path"])["arm"] == "B"
    inbox = Path(variant["inbox_path"])
    inbox.chmod(0o644)
    inbox.write_text(inbox.read_text() + "\n")
    with pytest.raises(ValueError, match="artifact changed: inbox.json"):
        load_variant(variant["manifest_path"])


def test_manifest_rejects_runtime_changes(variant, monkeypatch):
    from refund_eval.experiments import protocol

    original = protocol.digest
    monkeypatch.setattr(protocol, "digest", lambda path: "changed" if path.name == "task.py" else original(path))
    with pytest.raises(ValueError, match="Runtime changed"):
        load_variant(variant["manifest_path"])


def test_demo_cannot_silently_ignore_experiment(tmp_path, variant):
    manager = RunManager(tmp_path / "runs")
    try:
        with pytest.raises(APIError, match="published tools and fixture data"):
            manager.start(RunConfig(mode="demo", experiment_manifest=variant["manifest_path"]))
        assert not manager.list()
    finally:
        manager.close()


def test_worker_uses_frozen_scenario_inbox_and_records_manifest(tmp_path, variant, monkeypatch):
    directory = tmp_path / "run"
    directory.mkdir()
    config = {
        "mode": "model", "tool_profile": "published", "data_mode": "fixture", "world_mode": "friction",
        "model": "mockllm/target", "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
        "experiment_manifest": variant["manifest_path"],
    }
    (directory / "config.json").write_text(json.dumps(config))
    monkeypatch.setattr("refund_eval.published.profile.fixture_readiness", lambda: {"configured": True})
    calls = []

    def evaluate(*args, **kwargs):
        calls.append(kwargs)
        return [SimpleNamespace(samples=[SimpleNamespace(store={}, messages=[], scores={}, error=None)],
                                status="success", error=None, location=str(directory / "mock.eval"))]

    monkeypatch.setattr(worker, "eval", evaluate)
    assert worker.run_worker(directory) == 0
    assert calls[0]["task_args"]["scenario"] == variant["scenario_path"]
    assert calls[0]["task_args"]["fixture_path"] == variant["inbox_path"]
    assert calls[0]["task_args"]["initial_mail_thread_ids"] == variant["manifest"]["startup_context"]["thread_ids"]
    assert calls[0]["metadata"]["experiment"]["arm"] == "B"


def test_worker_refuses_changed_fixture_before_eval(tmp_path, variant, monkeypatch):
    directory = tmp_path / "run"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({
        "mode": "model", "tool_profile": "published", "data_mode": "fixture",
        "model": "mockllm/target", "simulator_model": "mockllm/simulator", "grader_model": "mockllm/grader",
        "experiment_manifest": variant["manifest_path"],
    }))
    fixture = Path(variant["inbox_path"])
    fixture.chmod(0o644)
    fixture.write_text("{}")
    monkeypatch.setattr(worker, "eval", lambda *a, **kw: pytest.fail("No model call after integrity failure"))
    assert worker.run_worker(directory) == 1
    snapshot = json.loads((directory / "snapshot.json").read_text())
    assert snapshot["phase"] == "failed"
    assert "artifact changed" in snapshot["error"]


def test_parallel_trials_require_same_group_and_settings(tmp_path):
    def config(arm, **changes):
        return RunConfig(mode="model", tool_profile="published", data_mode="fixture",
                         experiment_manifest=str(tmp_path / "variants" / arm / "manifest.json"),
                         **changes).model_dump()

    assert parallel_comparison_allowed(config("C"), [config("A"), config("B")])
    assert not parallel_comparison_allowed(config("C"), [config("C")])
    assert not parallel_comparison_allowed(config("C", max_turns=80), [config("A")])
    assert not parallel_comparison_allowed(config("C", experiment_replicate=1), [config("A")])
    assert not parallel_comparison_allowed(config("C"), [RunConfig(mode="demo").model_dump()])
    assert not parallel_comparison_allowed(config("C"), [{**config("A"), "experiment_manifest": str(tmp_path / "other" / "A" / "manifest.json")}])
    assert not parallel_comparison_allowed(config("C"), [config("A"), config("B"), config("C")])

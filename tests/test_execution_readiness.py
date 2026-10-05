"""Location-specific preflight cannot accidentally require nested Docker."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.dashboard import server
from refund_eval.harbor.config import TrialRequest, TrialSettings
from refund_eval.scenario.schema import load_scenario


def test_modal_credentials_preflight_does_not_run_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: pytest.fail("Modal consulted Docker"))
    monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
    monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
    assert not server.execution_readiness("modal")["configured"]
    monkeypatch.setenv("MODAL_TOKEN_ID", "test-id")
    assert not server.execution_readiness("modal")["configured"]
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "test-secret")
    assert server.execution_readiness("modal")["configured"]
    assert "test-secret" not in json.dumps(server.execution_readiness("modal"))


def test_modal_selected_profile_must_have_complete_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/bin/uv")
    for name in ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".modal.toml").write_text('[ready]\ntoken_id="test"\ntoken_secret="secret"\nactive=true\n[empty]\n')
    monkeypatch.setenv("MODAL_PROFILE", "empty")
    assert not server.execution_readiness("modal")["configured"]
    monkeypatch.setenv("MODAL_PROFILE", "ready")
    assert server.execution_readiness("modal")["configured"]


def test_modal_published_launch_preserves_mode_and_skips_host_browser(tmp_path, monkeypatch):
    from refund_eval.published import profile

    monkeypatch.setenv("MODAL_TOKEN_ID", "test-id")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(server.shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(profile, "hybrid_readiness", lambda: pytest.fail("Modal checked host browser"))
    monkeypatch.setattr(server.RunManager, "_monitor", lambda *args: None)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: SimpleNamespace(poll=lambda: 0, wait=lambda **k: 0))
    manager = server.RunManager(tmp_path)
    try:
        run = manager.start(server.RunConfig(mode="model", execution="modal", tool_profile="published",
                                            data_mode="hybrid", trials=2, concurrency=2))
        saved = json.loads((manager.directory(run["id"]) / "config.json").read_text())
        assert saved["tool_profile"] == "published" and saved["data_mode"] == "hybrid"
        assert saved["browser_runtime"] == "subprocess"
    finally:
        manager.close()


def test_published_harbor_request_cannot_use_docker_or_live_gmail():
    for settings in ({"browser_runtime": "docker"}, {"browser_runtime": "subprocess", "data_mode": "live"}):
        with pytest.raises(ValueError):
            TrialSettings(tool_profile="published", **settings)
    request = TrialRequest(instruction=load_scenario().task_text, model="openrouter/test/model",
                           settings=TrialSettings(tool_profile="published", data_mode="hybrid",
                                                  browser_runtime="subprocess"), trial_id="test")
    restored = TrialRequest.model_validate_json(request.model_dump_json())
    assert restored.settings.tool_profile == "published"
    assert restored.settings.data_mode == "hybrid" and restored.settings.browser_runtime == "subprocess"

"""Paid launch failures are reported before any worker or model call starts."""

import json
from types import SimpleNamespace

import pytest

from refund_eval.dashboard import server


def test_old_run_detail_adds_measurements_without_rewriting_recorded_files(tmp_path):
    run_id = "abcdef123456"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    recorded = {
        "run.json": {"id": run_id, "status": "completed", "created_at": "2026-09-01T00:00:00Z"},
        "snapshot.json": {"phase": "completed", "world": None,
                          "messages": [{"role": "assistant", "content": "Historical report.", "tool_calls": None}],
                          "scores": {}, "updated_at": "2026-09-01T01:00:00Z"},
    }
    for name, content in recorded.items():
        (run_dir / name).write_text(json.dumps(content))
    before = {name: ((run_dir / name).read_bytes(), (run_dir / name).stat().st_mtime_ns) for name in recorded}
    manager = server.RunManager(tmp_path)
    try:
        detail = manager.detail(run_id)
        assert detail["snapshot"]["long_horizon"]["summary"]["status_reports"] == 1
        assert detail["snapshot"]["messages"] == recorded["snapshot.json"]["messages"]
        assert detail["snapshot"]["updated_at"] == recorded["snapshot.json"]["updated_at"]
        assert {name: ((run_dir / name).read_bytes(), (run_dir / name).stat().st_mtime_ns)
                for name in recorded} == before
    finally:
        manager.close()


def test_scenario_organisation_labels_are_available_to_dashboard():
    organisations = server.public_config()["scenario"]["organisations"]
    by_key = {org["key"]: org for org in organisations}
    assert by_key["trainline"] == {"key": "trainline", "name": "Trainline", "kind": "retailer"}
    assert by_key["avanti"] == {"key": "avanti", "name": "Avanti West Coast", "kind": "operator"}
    assert all(set(org) == {"key", "name", "kind"} for org in organisations)


def test_openrouter_key_launches_all_default_roles(tmp_path, monkeypatch):
    monkeypatch.delenv("REFUND_EVAL_DEFAULT_MODEL", raising=False)
    for keys in server.PROVIDER_KEYS.values():
        for key in keys:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    launched = {}

    def start_worker(command, **kwargs):
        launched.update(command=command, **kwargs)
        return SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0)

    monkeypatch.setattr(server.subprocess, "Popen", start_worker)
    monkeypatch.setattr(server.RunManager, "_monitor", lambda *args: None)
    manager = server.RunManager(tmp_path)
    try:
        record = manager.start(server.RunConfig(mode="model"))
        config = json.loads((manager.directory(record["id"]) / "config.json").read_text())
        for role in ("model", "simulator_model", "grader_model"):
            assert config[role] == "openrouter/anthropic/claude-opus-5"
        assert launched["env"]["OPENROUTER_API_KEY"] == "test-openrouter-key"
        assert "ANTHROPIC_API_KEY" not in launched["env"]
        assert "OPENAI_API_KEY" not in launched["env"]
        assert "test-openrouter-key" not in json.dumps(config)
        public = server.public_config()
        provider = next(p for p in public["providers"] if p["name"] == "openrouter")
        assert provider["configured"]
        assert "test-openrouter-key" not in json.dumps(public)
    finally:
        manager.close()


def test_anthropic_key_does_not_satisfy_openrouter_launch(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    manager = server.RunManager(tmp_path)
    try:
        with pytest.raises(server.APIError, match="Set OPENROUTER_API_KEY") as error:
            manager.start(server.RunConfig(mode="model"))
        assert error.value.status == 422
        assert not manager.list() and not manager.processes
    finally:
        manager.close()


def test_missing_provider_sdk_is_rejected_before_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    monkeypatch.setattr(server, "sdk_available", lambda provider: False)
    manager = server.RunManager(tmp_path)
    try:
        config = server.RunConfig(mode="model", model="openai/example", simulator_model="mockllm/model",
                                  grader_model="mockllm/model")
        with pytest.raises(server.APIError, match="uv sync --extra providers") as error:
            manager.start(config)
        assert error.value.status == 422
        assert not manager.list() and not manager.processes
        providers = server.public_config()["providers"]
        openai = next(provider for provider in providers if provider["name"] == "openai")
        assert openai["credentials_available"] and not openai["sdk_available"] and not openai["configured"]
    finally:
        manager.close()


def test_unsupported_provider_alias_is_not_advertised(tmp_path):
    assert "xai" not in {provider["name"] for provider in server.public_config()["providers"]}
    manager = server.RunManager(tmp_path)
    try:
        with pytest.raises(server.APIError, match="Unsupported model provider"):
            manager.start(server.RunConfig(mode="model", model="xai/example"))
        assert not manager.processes
    finally:
        manager.close()


def test_published_profile_status_is_local_and_not_connected(tmp_path, monkeypatch):
    from refund_eval.published import profile

    monkeypatch.setenv("REFUND_EVAL_PUBLISHED_CONFIG", str(tmp_path / "missing.json"))
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True, "reason": "Browser ready"})
    monkeypatch.setattr(profile, "hybrid_readiness", lambda: {"configured": True, "reason": "Docker ready; Exa verified at startup"})
    profiles = {item["name"]: item for item in server.public_config()["tool_profiles"]}
    assert profiles["legacy"]["configured"] is True
    assert profiles["published"]["configured"] is True
    modes = {item["name"]: item for item in profiles["published"]["data_modes"]}
    assert profiles["published"]["default_data_mode"] == "hybrid"
    assert modes["fixture"]["configured"] is True
    assert modes["hybrid"]["configured"] is True
    assert modes["hybrid"]["label"] == "Simulated Gmail + live Exa web"
    assert modes["live"]["configured"] is False
    assert "config file" in modes["live"]["reason"]


def test_unavailable_published_profile_never_starts_a_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_PUBLISHED_CONFIG", str(tmp_path / "missing.json"))

    def forbidden(*args, **kwargs):
        raise AssertionError("No worker may start without published tool setup")

    monkeypatch.setattr(server.subprocess, "Popen", forbidden)
    manager = server.RunManager(tmp_path / "runs")
    try:
        with pytest.raises(server.APIError, match="Published tools are not configured") as caught:
            manager.start(server.RunConfig(mode="model", tool_profile="published", data_mode="live"))
        assert caught.value.status == 422
        assert not manager.list()
    finally:
        manager.close()


def test_published_selection_persists_server_config_path_without_fallback(tmp_path, monkeypatch):
    from refund_eval.published import profile

    config_path = tmp_path / "published.json"
    monkeypatch.setenv("REFUND_EVAL_PUBLISHED_CONFIG", str(config_path))
    checked = []

    def ready(path):
        checked.append(path)
        return {"configured": True, "reason": "Local setup is present"}

    monkeypatch.setattr(profile, "readiness", ready)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0))
    monkeypatch.setattr(server.RunManager, "_monitor", lambda *args: None)
    manager = server.RunManager(tmp_path / "runs")
    try:
        record = manager.start(server.RunConfig(mode="model", tool_profile="published", data_mode="live", model="mockllm/target",
            simulator_model="mockllm/sim", grader_model="mockllm/grader"))
        saved = json.loads((manager.directory(record["id"]) / "config.json").read_text())
        assert saved["tool_profile"] == record["tool_profile"] == "published"
        assert saved["published_config"] == str(config_path)
        assert checked == [str(config_path)]
        assert saved["data_mode"] == record["data_mode"] == "live"
        assert record["label"] == "Published tools · live data"
    finally:
        manager.close()


def test_demo_cannot_claim_to_use_published_tools(tmp_path):
    manager = server.RunManager(tmp_path)
    try:
        with pytest.raises(server.APIError, match="offline demo uses legacy"):
            manager.start(server.RunConfig(mode="demo", tool_profile="published"))
        assert not manager.list()
    finally:
        manager.close()


@pytest.mark.parametrize("data_mode,label", [
    ("fixture", "Published tools · simulated data"),
    ("hybrid", "Simulated Gmail + live Exa web"),
])
def test_simulated_gmail_never_reads_live_config(tmp_path, monkeypatch, data_mode, label):
    from refund_eval.published import profile

    def forbidden(*args, **kwargs):
        raise AssertionError("Simulated data must not read Gmail configuration")

    monkeypatch.setattr(profile, "fixture_readiness", forbidden)
    monkeypatch.setattr(profile, "hybrid_readiness", forbidden)
    monkeypatch.setattr(profile, f"{data_mode}_readiness", lambda: {"configured": True, "reason": "Browser ready"})
    monkeypatch.setattr(profile, "readiness", forbidden)
    monkeypatch.setattr(server, "published_config_path", forbidden)
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0))
    monkeypatch.setattr(server.RunManager, "_monitor", lambda *args: None)
    manager = server.RunManager(tmp_path / "runs")
    try:
        record = manager.start(server.RunConfig(mode="model", tool_profile="published", data_mode=data_mode, model="mockllm/target",
            simulator_model="mockllm/sim", grader_model="mockllm/grader"))
        saved = json.loads((manager.directory(record["id"]) / "config.json").read_text())
        assert saved["data_mode"] == record["data_mode"] == data_mode
        assert "published_config" not in saved
        assert record["label"] == label
    finally:
        manager.close()


@pytest.mark.parametrize("data_mode", ["fixture", "hybrid"])
def test_unavailable_simulated_gmail_runtime_cannot_start_worker(tmp_path, monkeypatch, data_mode):
    from refund_eval.published import profile

    monkeypatch.setattr(profile, f"{data_mode}_readiness", lambda: {"configured": False, "reason": "Docker is unavailable"})

    def forbidden(*args, **kwargs):
        raise AssertionError("No worker may start before browser setup")

    monkeypatch.setattr(server.subprocess, "Popen", forbidden)
    manager = server.RunManager(tmp_path / "runs")
    try:
        with pytest.raises(server.APIError, match="Docker is unavailable"):
            manager.start(server.RunConfig(mode="model", tool_profile="published", data_mode=data_mode))
        assert not manager.list()
    finally:
        manager.close()


@pytest.mark.parametrize("data_mode", ["live", "hybrid"])
def test_legacy_cannot_claim_live_data(tmp_path, data_mode):
    manager = server.RunManager(tmp_path)
    try:
        with pytest.raises(server.APIError, match=f"{data_mode.title()} data requires"):
            manager.start(server.RunConfig(mode="model", tool_profile="legacy", data_mode=data_mode))
        assert not manager.list()
    finally:
        manager.close()


def test_dashboard_model_override_is_runtime_scoped_and_keeps_explicit_models(monkeypatch):
    from refund_eval.published import profile
    from refund_eval.task import DEFAULT_ROLE_MODEL

    monkeypatch.delenv("REFUND_EVAL_DEFAULT_MODEL", raising=False)
    assert server.RunConfig().model == DEFAULT_ROLE_MODEL
    chosen = "openrouter/anthropic/claude-haiku-4.5"
    monkeypatch.setenv("REFUND_EVAL_DEFAULT_MODEL", chosen)
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True, "reason": "Browser ready"})
    config = server.RunConfig(mode="model")
    assert config.model == config.simulator_model == config.grader_model == chosen
    assert server.public_config()["default_model"] == chosen
    assert (config.cost_limit, config.max_turns, config.sim_weeks) == (1.0, 20, 1.0)
    explicit = server.RunConfig(model="mockllm/target", simulator_model="mockllm/simulator", grader_model="mockllm/grader")
    assert (explicit.model, explicit.simulator_model, explicit.grader_model) == (
        "mockllm/target", "mockllm/simulator", "mockllm/grader",
    )
    assert DEFAULT_ROLE_MODEL == "openrouter/anthropic/claude-opus-5"


def test_long_preset_uses_runtime_models_and_persists_full_horizon(tmp_path, monkeypatch):
    from refund_eval.published import profile

    model = "openrouter/anthropic/claude-haiku-4.5"
    monkeypatch.setenv("REFUND_EVAL_DEFAULT_MODEL", model)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-placeholder")
    monkeypatch.setattr(server, "sdk_available", lambda provider: True)
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True, "reason": "Browser ready"})
    monkeypatch.setattr(profile, "hybrid_readiness", lambda: {"configured": True, "reason": "Docker ready"})
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0))
    monkeypatch.setattr(server.RunManager, "_monitor", lambda *args: None)
    presets = {item["name"]: item["settings"] for item in server.public_config()["run_presets"]}
    quick = server.RunConfig.model_validate(presets["quick"])
    longer = server.RunConfig.model_validate(presets["long"])
    assert (quick.max_turns, quick.cost_limit, quick.sim_weeks, quick.world_mode) == (20, 1, 1, "baseline")
    assert (longer.max_turns, longer.cost_limit, longer.sim_weeks, longer.world_mode) == (600, 10, 12, "friction")
    for preset in (quick, longer):
        assert preset.tool_profile == "published" and preset.data_mode == "hybrid"
        assert preset.model == preset.simulator_model == preset.grader_model == model
    manager = server.RunManager(tmp_path / "runs")
    try:
        record = manager.start(longer)
        saved = json.loads((manager.directory(record["id"]) / "config.json").read_text())
        assert saved["world_mode"] == record["world_mode"] == "friction"
        assert saved["data_mode"] == record["data_mode"] == "hybrid"
        assert "published_config" not in saved
        assert (saved["max_turns"], saved["cost_limit"], saved["sim_weeks"]) == (600, 10, 12)
        assert "realistic friction" in record["label"]
    finally:
        manager.close()


@pytest.mark.parametrize("settings", [
    {"mode": "demo", "world_mode": "friction"},
    {"mode": "model", "tool_profile": "published", "data_mode": "live", "world_mode": "friction"},
])
def test_friction_requires_simulated_model_trial_before_setup_or_launch(tmp_path, monkeypatch, settings):
    from refund_eval.published import profile

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid world mode must fail before setup or worker launch")

    monkeypatch.setattr(profile, "readiness", forbidden)
    monkeypatch.setattr(server.subprocess, "Popen", forbidden)
    manager = server.RunManager(tmp_path)
    try:
        with pytest.raises(server.APIError, match="Realistic friction requires"):
            manager.start(server.RunConfig(**settings))
        assert not manager.list()
    finally:
        manager.close()

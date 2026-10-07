"""Paid launch failures are reported before any worker or model call starts."""

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.config import (
    DEFAULT_GRADER_MODEL,
    DEFAULT_ROLE_MODEL,
    DEFAULT_TARGET_MODEL,
)
from refund_eval.dashboard import server

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "refund_eval" / "dashboard" / "static"


@pytest.fixture(autouse=True)
def ready_runtime_for_unit_tests(monkeypatch):
    from refund_eval.published import profile
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True, "reason": "test runtime"})
    monkeypatch.setattr(profile, "hybrid_readiness", lambda: {"configured": True, "reason": "test runtime"})
    monkeypatch.setattr(server, "execution_readiness", lambda _: {"configured": True, "reason": "test location"})


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
    monkeypatch.setattr(server, "execution_readiness", lambda _: {"configured": True, "reason": "test"})
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
        assert config["model"] == "openrouter/anthropic/claude-opus-5"
        assert config["simulator_model"] == "openrouter/openai/gpt-6-astra"
        assert config["grader_model"] == "openrouter/anthropic/claude-haiku-4.5"
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
    assert set(profiles) == {"published"}
    assert profiles["published"]["configured"] is True
    modes = {item["name"]: item for item in profiles["published"]["data_modes"]}
    assert profiles["published"]["default_data_mode"] == "hybrid"
    assert modes["fixture"]["configured"] is True
    assert modes["hybrid"]["configured"] is True
    assert modes["hybrid"]["label"] == "Simulated Gmail + live web"
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


def test_scripted_demo_is_labelled_with_published_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0))
    monkeypatch.setattr(server.RunManager, "_monitor", lambda *args: None)
    manager = server.RunManager(tmp_path / "runs")
    try:
        record = manager.start(server.RunConfig(mode="demo"))
        assert record["label"] == "Scripted demo · published tools"
        assert record["tool_profile"] == "published"
        assert record["data_mode"] == "fixture"
    finally:
        manager.close()


def test_demo_cannot_request_retired_legacy_tools(tmp_path):
    manager = server.RunManager(tmp_path)
    try:
        with pytest.raises(ValueError, match="Legacy execution is retired"):
            manager.start(server.RunConfig(mode="demo", tool_profile="legacy"))
        assert not manager.list()
    finally:
        manager.close()


@pytest.mark.parametrize("data_mode,label", [
    ("fixture", "Published tools · simulated data"),
    ("hybrid", "Simulated Gmail + live web"),
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
        with pytest.raises(ValueError, match="Legacy execution is retired"):
            manager.start(server.RunConfig(mode="model", tool_profile="legacy", data_mode=data_mode))
        assert not manager.list()
    finally:
        manager.close()


def test_dashboard_model_override_is_runtime_scoped_and_keeps_explicit_models(monkeypatch):
    from refund_eval.published import profile

    monkeypatch.delenv("REFUND_EVAL_DEFAULT_MODEL", raising=False)
    assert server.RunConfig().model == DEFAULT_TARGET_MODEL
    chosen = "openrouter/anthropic/claude-haiku-4.5"
    monkeypatch.setenv("REFUND_EVAL_DEFAULT_MODEL", chosen)
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True, "reason": "Browser ready"})
    config = server.RunConfig(mode="model")
    # The override moves the target only; the roles keep their own default.
    assert config.model == chosen
    assert (config.simulator_model, config.grader_model) == (DEFAULT_ROLE_MODEL, DEFAULT_GRADER_MODEL)
    public = server.public_config()
    assert (public["default_model"], public["default_role_model"]) == (chosen, DEFAULT_ROLE_MODEL)
    assert (config.cost_limit, config.max_turns, config.sim_weeks) == (1.0, 20, 1.0)
    explicit = server.RunConfig(model="mockllm/target", simulator_model="mockllm/simulator", grader_model="mockllm/grader")
    assert (explicit.model, explicit.simulator_model, explicit.grader_model) == (
        "mockllm/target", "mockllm/simulator", "mockllm/grader",
    )
    assert DEFAULT_TARGET_MODEL == "openrouter/anthropic/claude-opus-5"
    assert DEFAULT_ROLE_MODEL == "openrouter/openai/gpt-6-astra"


def test_long_preset_uses_runtime_models_and_persists_full_horizon(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "execution_readiness", lambda _: {"configured": True, "reason": "test"})
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
        assert preset.history_tokens == server.RunConfig().history_tokens == 60_000
        assert preset.tool_profile == "published" and preset.data_mode == "hybrid"
        assert preset.model == model
        assert (preset.simulator_model, preset.grader_model) == (DEFAULT_ROLE_MODEL, DEFAULT_GRADER_MODEL)
    manager = server.RunManager(tmp_path / "runs")
    try:
        record = manager.start(longer)
        saved = json.loads((manager.directory(record["id"]) / "config.json").read_text())
        assert saved["world_mode"] == record["world_mode"] == "friction"
        assert saved["data_mode"] == record["data_mode"] == "hybrid"
        assert "published_config" not in saved
        assert (saved["max_turns"], saved["cost_limit"], saved["sim_weeks"]) == (600, 10, 12)
        assert saved["history_tokens"] == 60_000
        record = manager.start(longer.model_copy(update={"history_tokens": 0}))
        assert json.loads((manager.directory(record["id"]) / "config.json").read_text())["history_tokens"] == 0
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


@pytest.mark.parametrize(('settings', 'message'), [
    ({'execution': 'local', 'trials': 2}, 'multiple trials'),
    ({'execution': 'modal', 'data_mode': 'live'}, 'Live Gmail is local-only'),
    ({'execution': 'modal', 'trials': 1, 'concurrency': 2}, 'Concurrency'),
    ({'execution': 'modal', 'max_turns': 3}, 'at least 12'),
    ({'execution': 'modal', 'mode': 'model', 'model': 'anthropic/example'}, 'OpenRouter'),
    ({'execution': 'modal', 'experiment_manifest': 'comparison.json'}, 'local only'),
])
def test_invalid_batch_launch_is_rejected_before_worker(tmp_path, settings, message):
    manager = server.RunManager(tmp_path)
    try:
        with pytest.raises(server.APIError, match=message):
            manager.start(server.RunConfig(**settings))
        assert not manager.list() and not manager.processes
    finally:
        manager.close()


def test_modal_launch_dispatches_isolated_controller_with_frozen_rubrics(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(server.subprocess, 'Popen', lambda command, **kwargs: (
        commands.append(command) or SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0)))
    monkeypatch.setattr(server.RunManager, '_monitor', lambda *args: None)
    monkeypatch.setattr(server.shutil, 'which', lambda executable: '/usr/bin/uv')
    manager = server.RunManager(tmp_path)
    try:
        record = manager.start(server.RunConfig(execution='modal', trials=4, concurrency=2))
        directory = manager.directory(record['id'])
        assert commands[0][0].endswith('/scripts/harbor')
        assert commands[0][3] == 'refund_eval.harbor.dashboard'
        assert json.loads((directory / 'rubrics.json').read_text()) == manager.behaviours()
        assert record['config']['trials'] == 4 and record['config']['concurrency'] == 2
    finally:
        manager.close()


def test_dead_batch_controller_finalizes_active_children(tmp_path):
    manager = server.RunManager(tmp_path)
    parent = 'abcdef123456'
    child = 'abcdef123457'
    try:
        for run_id, extra in [(parent, {'config': {'execution': 'modal'}}),
                              (child, {'parent_batch_id': parent})]:
            directory = tmp_path / run_id
            directory.mkdir()
            server.write_json(directory / 'run.json', {'id': run_id, 'status': 'running', **extra})
        server.write_json(tmp_path / child / 'snapshot.json', {'phase': 'running', 'messages': ['partial']})
        server.write_json(tmp_path / parent / 'snapshot.json', {'phase': 'running', 'batch': {
            'counts': {'running': 1, 'queued': 2}, 'trials': [{'id': child, 'status': 'running'}],
            'models': [{'model': 'openrouter/a/b', 'counts': {'running': 1, 'queued': 2}}],
        }})
        manager._monitor(parent, SimpleNamespace(wait=lambda: 9))
        assert manager.describe(parent)['status'] == 'failed'
        detail = manager.detail(child)
        assert detail['run']['status'] == 'failed'
        assert detail['snapshot']['messages'] == ['partial']
        assert manager.detail(parent)['snapshot']['batch']['counts']['failed'] == 3
        assert manager.detail(parent)['snapshot']['batch']['models'][0]['counts']['failed'] == 3
    finally:
        manager.close()


@pytest.mark.parametrize('settings', [
    {'models': []}, {'models': ['openrouter/a/b', 'openrouter/a/b']},
    {'models': ['anthropic/a']}, {'models': ['openrouter/a/b'] * 17},
])
def test_invalid_model_list_is_rejected(settings):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        server.RunConfig(**settings)


@pytest.mark.parametrize('settings, message', [
    ({'mode': 'model', 'execution': 'local'}, 'Harbor model batch'),
    ({'mode': 'demo', 'execution': 'modal'}, 'Harbor model batch'),
    ({'mode': 'model', 'execution': 'modal', 'trials': 51}, '100 total trials'),
])
def test_multi_model_scope_and_total_limits(tmp_path, settings, message):
    manager = server.RunManager(tmp_path)
    try:
        with pytest.raises(server.APIError, match=message):
            manager.start(server.RunConfig(models=['openrouter/a/first', 'openrouter/a/second'], **settings))
        assert not manager.processes
    finally:
        manager.close()


def test_multi_model_launch_counts_trials_and_checks_all_models(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-only-key')
    monkeypatch.setattr(server.shutil, 'which', lambda command: '/usr/bin/uv')
    monkeypatch.setattr(server, 'sdk_available', lambda provider: True)
    monkeypatch.setattr(server.subprocess, 'Popen', lambda *a, **k: SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0))
    monkeypatch.setattr(server.RunManager, '_monitor', lambda *a: None)
    manager = server.RunManager(tmp_path)
    try:
        models = ['openrouter/a/first', 'openrouter/b/second']
        record = manager.start(server.RunConfig(mode='model', execution='modal', models=models,
                                               trials=1, concurrency=2))
        assert record['config']['models'] == models
        assert record['model'] == models[0]
        batch = manager.detail(record['id'])['snapshot']['batch']
        assert batch['total'] == 2 and batch['counts']['queued'] == 2
    finally:
        manager.close()


def test_hybrid_data_mode_label_matches_the_picker_everywhere(tmp_path, monkeypatch):
    """The saved-run label, /api/config and every app.js subtitle read exactly as the data picker does."""
    monkeypatch.setenv("REFUND_EVAL_PUBLISHED_CONFIG", str(tmp_path / "missing.json"))
    picker = re.search(r'<option value="hybrid"[^>]*>([^<]+)</option>', (STATIC / "index.html").read_text()).group(1)
    assert picker == "Simulated Gmail + live web"
    modes = {item["name"]: item for item in server.public_config()["tool_profiles"][0]["data_modes"]}
    assert modes["hybrid"]["label"] == picker
    app_source = (STATIC / "app.js").read_text()
    assert picker in app_source
    for source in (app_source, Path(server.__file__).read_text()):
        assert "live Exa web" not in source


def contact_attempts(attempts: list[dict]) -> list[bool]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise dashboard contact classification")
    result = subprocess.run(
        [node, "-e", """
const fs = require('node:fs');
const vm = require('node:vm');
const context = {window: {}}; // No document: the pure helpers must stand alone.
vm.runInNewContext(fs.readFileSync('./refund_eval/dashboard/static/app.js', 'utf8'), context);
const attempts = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(attempts.map((attempt) => context.window.RefundActivity.contactAttempt(attempt))));
"""],
        input=json.dumps(attempts), capture_output=True, text=True, cwd=ROOT, check=True, timeout=10,
    )
    return json.loads(result.stdout)


def test_published_interceptions_count_as_organisation_contact():
    """Published sends, replies and simulated form posts appear on the counterpart cards; blocked and inbox-only actions do not."""
    org = "trainline"
    attempts = [
        {"tool": "send_message", "channel": "email", "target": "refunds@trainline.example", "org": org},
        {"tool": "reply", "channel": "email", "target": "refunds@trainline.example", "org": org},
        {"tool": "browser_request", "channel": "form", "target": "https://www.trainline.example/contact", "org": org},
        {"tool": "send_email", "channel": "email", "target": "refunds@trainline.example", "org": org},  # saved legacy run
        {"tool": "browser_request", "channel": "web_action", "target": "https://www.trainline.example/api", "org": org},  # blocked POST
        {"tool": "manage_email", "channel": "inbox_change", "target": "Follow up", "org": None},
        {"tool": "send_message", "channel": "email", "target": "me@example.com", "org": None, "to_user": True},
        {"tool": "search_threads", "channel": "email", "target": "", "org": None},
    ]
    assert contact_attempts(attempts) == [True, True, True, True, False, False, False, False]
    # The rendered "On the other side" cards must use this same classification.
    assert "window.RefundActivity.contactAttempt(attempt)" in (STATIC / "app.js").read_text()


def test_unknown_model_ids_are_caught_before_launch_with_suggestions():
    from refund_eval.dashboard.models import unknown_models

    catalog = {"all_ids": ["openrouter/openai/gpt-6-astra", "openrouter/anthropic/claude-opus-5.5",
                           "openrouter/anthropic/claude-haiku-4.5", "openrouter/some/embedder"],
               "models": [{"id": "openrouter/openai/gpt-6-astra"}, {"id": "openrouter/anthropic/claude-opus-5.5"},
                          {"id": "openrouter/anthropic/claude-haiku-4.5"}]}
    ok = {"target": "openrouter/anthropic/claude-opus-5.5", "simulator": "openrouter/openai/gpt-6-astra",
          "grader": "openrouter/anthropic/claude-haiku-4.5"}
    assert unknown_models(ok, catalog) == []
    [problem] = unknown_models({**ok, "simulator": "openrouter/openai/gpt-astra-5"}, catalog)
    assert problem.startswith("simulator: openrouter/openai/gpt-astra-5 is not an OpenRouter model.")
    assert "Did you mean openrouter/openai/gpt-6-astra" in problem
    assert "can't call tools" in unknown_models({**ok, "target": "openrouter/some/embedder"}, catalog)[0]
    assert unknown_models({"simulator": "openrouter/x/y"}, {"all_ids": [], "models": []}) == []  # offline: allow

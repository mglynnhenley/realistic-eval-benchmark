"""A scenario added as YAML is immediately available to every launch entry point."""

import shutil
import sys
import tomllib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from refund_eval.agent.schedule import sim_deadline
from refund_eval.config import RunConfig
from refund_eval.dashboard import server
from refund_eval.harbor import __main__ as harbor_cli
from refund_eval.harbor.config import TrialSettings
from refund_eval.scenario import schema
from refund_eval.world.state import World
from refund_eval.world_service.settings import WorldSettings


@pytest.mark.parametrize("settings_class", [RunConfig, TrialSettings, WorldSettings, "task", "native_writer",
                                           "native_cli", "harbor_cli", "world_cli", "world_env"])
@pytest.mark.parametrize(("name", "overrides", "weeks"), [
    ("bills_price_rise", {}, 12),
    ("bills_price_rise", {"sim_weeks": 4}, 4),
    ("bills_price_rise", {"sim_weeks": 6}, 6),
    ("train_refund_late", {}, 6),
])
def test_scenario_default_deadline(settings_class, name, overrides, weeks, monkeypatch, tmp_path):
    scenario = schema.load_scenario(name)
    flags = ["--sim-weeks", str(overrides["sim_weeks"])] if overrides else []
    if settings_class == "task":
        from refund_eval import task
        from refund_eval.published import profile

        monkeypatch.setattr(profile, "fixture_readiness", lambda **_: {"configured": True})
        monkeypatch.setattr(task, "register_default_costs", lambda *_, **__: None)
        monkeypatch.setattr(task, "default_model_roles", lambda _: {})
        resolved = task.impossible_refund(scenario=name, cost_limit=1, published_data="fixture", **overrides)
        sim_weeks = resolved.metadata["sim_weeks"]
    elif settings_class in {"native_writer", "native_cli"}:
        from refund_eval.harbor import adapter

        def copy_source(destination):
            destination.mkdir()
            return "test-source"

        monkeypatch.setattr(adapter, "copy_source", copy_source)
        monkeypatch.setattr(adapter, "stage_agent", lambda *args: None)
        monkeypatch.setattr(adapter, "validate_and_index", lambda *args: None)
        if settings_class == "native_cli":
            assert adapter.main(["--scenario", name, "--out", str(tmp_path), *flags]) == 0
        else:
            adapter.write_task(tmp_path, scenario=name, data_mode="fixture", max_turns=1000,
                               research_mode="auto", simulator_model="mockllm/model", grader_model="mockllm/model",
                               agent_timeout=3600, verifier_timeout=600, cpus=2, memory_mb=4096, **overrides)
        config = tomllib.loads((tmp_path / name / "task.toml").read_text())
        sim_weeks = config["metadata"]["sim_weeks"]
        assert float(config["environment"]["env"]["WORLD_SIM_WEEKS"]) == sim_weeks
    elif settings_class == "harbor_cli":
        prepared = {}
        monkeypatch.setattr(sys, "argv", ["refund_eval.harbor", "--output", str(tmp_path),
                                        "--model", "openrouter/example/model", "--scenario", name, "--cost-limit", "1", *flags])
        monkeypatch.setattr(harbor_cli, "prepare", lambda destination, **kw: prepared.update(kw) or destination / "job.json")
        harbor_cli.main()
        sim_weeks = prepared["settings"].sim_weeks
    elif settings_class in {"world_cli", "world_env"}:
        from refund_eval.harbor.world_entrypoint import world_argv
        from refund_eval.world_service import __main__ as world_cli
        from refund_eval.world_service import server as world_server
        from refund_eval.world_service import trial

        captured = {}

        class Trial:
            def __init__(self, settings, **kwargs):
                captured["settings"] = settings

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        async def serve(*args, **kwargs):
            pass

        monkeypatch.setattr(trial, "WorldTrial", Trial)
        monkeypatch.setattr(world_server, "serve_until_stopped", serve)
        monkeypatch.setenv("WORLD_ADMIN_TOKEN", "test-admin")
        monkeypatch.setenv("WORLD_TURN_TOKEN", "test-turn")
        argv = ["--scenario", name, *flags]
        if settings_class == "world_env":
            argv = world_argv({"WORLD_SCENARIO": name,
                               **({"WORLD_SIM_WEEKS": str(overrides["sim_weeks"])} if overrides else {})}, None)
        assert world_cli.main(argv) == 0
        sim_weeks = captured["settings"].sim_weeks
    else:
        budget = {"cost_limit": 1} if settings_class in (RunConfig, TrialSettings) else {}
        sim_weeks = settings_class(scenario=name, **budget, **overrides).sim_weeks
    world = World(start_time=scenario.start_time, now=scenario.start_time)
    deadline = sim_deadline(world, sim_weeks)
    assert sim_weeks == weeks
    if name == "bills_price_rise" and not overrides:
        assert deadline == datetime(2027, 1, 25, 9, tzinfo=ZoneInfo("Europe/London"))
    else:
        assert deadline == scenario.start_time.astimezone(ZoneInfo("Europe/London")) + timedelta(weeks=weeks)


@pytest.fixture
def scenario_dir(tmp_path, monkeypatch):
    directory = tmp_path / "scenarios"
    directory.mkdir()
    for path in schema.SCENARIO_DIR.glob("*.yaml"):
        shutil.copyfile(path, directory / path.name)
    shutil.copytree(schema.SCENARIO_DIR / "archive", directory / "archive")
    monkeypatch.setattr(schema, "SCENARIO_DIR", directory)
    return directory


def add_scenario(directory, *, label="New refund scenario"):
    path = directory / "foo.yaml"
    path.write_text(f"extends: train_refund\nid: foo_id\nlabel: {label!r}\n")
    return path


def test_discovery_reads_current_yaml_files_in_name_order(scenario_dir):
    before = schema.scenario_names()
    assert "foo" not in before
    path = add_scenario(scenario_dir)
    (scenario_dir / "ignored.txt").write_text("not a scenario")
    assert schema.scenario_names() == sorted([*before, "foo"])
    path.unlink()
    assert schema.scenario_names() == before


@pytest.mark.parametrize("name", ["train_refund", "train_refund_pressure"])
def test_archived_scenarios_are_loadable_but_not_launch_choices(name):
    assert name not in schema.scenario_names()
    assert schema.load_scenario(name).id == name
    for settings_class in (RunConfig, TrialSettings):
        with pytest.raises(ValueError, match="Unknown scenario"):
            settings_class(scenario=name)
    assert RunConfig(scenario=name, resume_log="old.eval").scenario == name
    assert TrialSettings(scenario=name, resume_sha256="a" * 64).scenario == name


@pytest.mark.parametrize("name", ["train_refund", "train_refund_pressure"])
def test_archived_scenarios_load_from_old_absolute_paths(name):
    path = schema.SCENARIO_DIR / f"{name}.yaml"
    assert not path.exists()
    assert schema.load_scenario(str(path)) == schema.load_scenario(name)


def test_missing_unarchived_scenario_path_still_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        schema.load_scenario(str(tmp_path / "unknown.yaml"))


def test_existing_path_with_archived_name_takes_precedence(tmp_path):
    path = tmp_path / "train_refund.yaml"
    path.write_text((schema.SCENARIO_DIR / "bills_price_rise.yaml").read_text())
    assert schema.load_scenario(str(path)).id == "bills_price_rise"


def test_default_scenario_is_shared_and_dashboard_leaves_cap_blank():
    from refund_eval.sim.common import DEFAULT_SCENARIO
    from refund_eval.world_service.settings import WorldSettings

    assert schema.load_scenario().id == DEFAULT_SCENARIO == WorldSettings().scenario == "train_refund_late"
    defaults = server.run_defaults()
    assert defaults["scenario"] == "train_refund_late"
    assert defaults["cost_limit"] is None
    with pytest.raises(ValueError):
        RunConfig.model_validate(defaults)


@pytest.mark.parametrize("settings_class", [RunConfig, TrialSettings])
def test_run_settings_accept_only_current_scenario_names(scenario_dir, settings_class):
    with pytest.raises(ValueError, match="Unknown scenario 'foo'"):
        settings_class(scenario="foo")
    path = add_scenario(scenario_dir)
    assert settings_class(scenario="foo").scenario == "foo"
    for invalid in ("foo.yaml", "../foo", str(path)):
        with pytest.raises(ValueError, match="Unknown scenario"):
            settings_class(scenario=invalid)
    path.unlink()
    with pytest.raises(ValueError, match="Unknown scenario 'foo'"):
        settings_class(scenario="foo")


def test_dashboard_discovers_scenario_label_and_falls_back_to_id(scenario_dir, monkeypatch):
    from refund_eval.published import profile

    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True})
    monkeypatch.setattr(profile, "hybrid_readiness", lambda: {"configured": True})
    monkeypatch.setattr(server, "execution_readiness", lambda _: {"configured": True})
    assert "foo" not in {item["name"] for item in server.public_config()["scenarios"]}
    add_scenario(scenario_dir)
    choices = server.public_config()["scenarios"]
    assert [item["name"] for item in choices] == schema.scenario_names()
    labels = lambda choices: {item["name"]: item["label"] for item in choices}
    assert labels(choices)["foo"] == "New refund scenario"
    add_scenario(scenario_dir, label="")
    assert labels(server.public_config()["scenarios"])["foo"] == "foo_id"


def test_harbor_cli_discovers_new_yaml_on_each_invocation(scenario_dir, tmp_path, monkeypatch, capsys):
    output = tmp_path / "job"
    monkeypatch.setattr(sys, "argv", [
        "refund_eval.harbor", "--output", str(output), "--model", "openrouter/example/model",
        "--scenario", "foo",
    ])
    with pytest.raises(SystemExit) as exited:
        harbor_cli.main()
    assert exited.value.code == 2
    assert "invalid choice: 'foo'" in capsys.readouterr().err
    assert not output.exists()

    add_scenario(scenario_dir)
    prepared = {}

    def prepare(destination, **kwargs):
        prepared.update(kwargs)
        return destination / "job.json"

    monkeypatch.setattr(harbor_cli, "prepare", prepare)
    harbor_cli.main()
    assert prepared["settings"].scenario == "foo"
    assert "Run with:" in capsys.readouterr().out

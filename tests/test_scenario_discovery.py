"""A scenario added as YAML is immediately available to every launch entry point."""

import shutil
import sys

import pytest

from refund_eval.config import RunConfig
from refund_eval.dashboard import server
from refund_eval.harbor import __main__ as harbor_cli
from refund_eval.harbor.config import TrialSettings
from refund_eval.scenario import schema


@pytest.fixture
def scenario_dir(tmp_path, monkeypatch):
    directory = tmp_path / "scenarios"
    directory.mkdir()
    for path in schema.SCENARIO_DIR.glob("*.yaml"):
        shutil.copyfile(path, directory / path.name)
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

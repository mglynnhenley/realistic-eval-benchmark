from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.harbor
pytest.importorskip("harbor.models.job.config")

from refund_eval.harbor import adapter, batch
from scripts import laura_trial

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def saved_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", "off")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    values = {
        "scenario": "train_refund_late",
        "execution": "modal",
        "model": "openrouter/test/target",
        "simulator_model": "openrouter/test/simulator",
        "grader_model": "openrouter/test/grader",
        "openrouter_key_mode": "shared_roles",
        "requested_deadline": "2027-01-07",
        "shared_allowance_usd": 41,
        "cost_limit": 19,
        "world_cost_limit": 37,
        "agent_max_output_tokens": 1234,
        "time_limit": 4321,
        "sim_weeks": 7,
        "max_turns": 123,
        "token_budget": 43210,
        "history_tokens": 1500,
    }
    path = tmp_path / "approved.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    return path, tmp_path / "prepared", values


def _args(path, out):
    return ["prepare", "--run-config", str(path), "--prepared-dir", str(out)]


def test_offline_prepare_command_packages_and_loads_exact_public_wheel(saved_spec):
    path, out, values = saved_spec
    original_spec = path.read_bytes()
    result = subprocess.run(
        [str(ROOT / "scripts" / "harbor"), "python", "-m", "scripts.laura_trial", *_args(path, out)],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stderr
    assert "installed_public_loader=true launch_started=false launch_approval=false" in result.stdout
    assert path.read_bytes() == original_spec
    record = json.loads((out / "run.json").read_bytes())
    assert record["installed_public_loader"]["checker"] == "refund_eval.task.load_agent_scenario"
    wheel = Path(record["config"]["agent_wheel"])
    assert wheel.is_relative_to(out)
    assert record["installed_public_loader"]["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    with zipfile.ZipFile(wheel) as archive:
        descriptor = archive.read(record["installed_public_loader"]["scenario_member"])
    assert record["installed_public_loader"]["scenario_sha256"] == hashlib.sha256(descriptor).hexdigest()
    manifest = out / "laura-setup.json"
    job = laura_trial.prepare_fresh_job(manifest, "offline-proof")
    task = job.tasks[0].path
    task_env = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))["environment"]["env"]
    world = laura_trial._world_environment(task, job, task_env)
    assert world["WORLD_STRICT_BUDGET"] == "true"
    assert float(world["WORLD_COST_LIMIT"]) == values["world_cost_limit"]
    assert int(world["WORLD_MAX_TURNS"]) == values["max_turns"]
    assert float(world["WORLD_SIM_WEEKS"]) == values["sim_weeks"]
    assert int(job.agents[0].env["REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS"]) == values["agent_max_output_tokens"]
    for key in ("cost_limit", "time_limit", "token_budget", "history_tokens"):
        assert job.agents[0].kwargs[key] == values[key]
    assert not (out / "jobs").exists()


@pytest.mark.parametrize("missing", ["openrouter_key_mode", "token_budget", "time_limit"])
def test_prepare_rejects_missing_approved_limits(saved_spec, missing, capsys):
    path, out, values = saved_spec
    del values[missing]
    path.write_text(json.dumps(values), encoding="utf-8")
    assert laura_trial.main(_args(path, out)) == 2
    assert f"saved_run_spec_missing_{missing}" in capsys.readouterr().err
    assert not out.exists()


def test_prepare_rejects_planning_caps_the_manifest_runner_would_not_read(saved_spec, capsys):
    path, out, values = saved_spec
    for key in ("agent_max_output_tokens", "requested_deadline", "shared_allowance_usd"):
        del values[key]
    values.update(openrouter_key_mode="shared", trial_spend_cap_usd=60, key_spend_cap_usd=60)
    path.write_text(json.dumps(values), encoding="utf-8")
    assert laura_trial.main(_args(path, out)) == 2
    assert "combined_planning_caps_unsupported_by_manifest_runner" in capsys.readouterr().err
    assert not out.exists()


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"simulator_model": "mockllm/simulator"}, "all_active_models_must_use_openrouter"),
    ({"grader_model": "mockllm/grader"}, "all_active_models_must_use_openrouter"),
    ({"world_cost_limit": 31.01}, "agent_and_world_caps_exceed_runtime_trial_delta"),
])
def test_prepare_rejects_unlaunchable_shared_key_spec(saved_spec, overrides, reason, monkeypatch, capsys):
    path, out, values = saved_spec
    for key in ("agent_max_output_tokens", "requested_deadline", "shared_allowance_usd"):
        del values[key]
    values.update(openrouter_key_mode="shared", world_cost_limit=31)
    values.update(overrides)
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setattr(adapter, "copy_source", lambda *_: pytest.fail("started building an unlaunchable spec"))
    assert laura_trial.main(_args(path, out)) == 2
    assert f"laura_prepare_failed={reason}" in capsys.readouterr().err
    assert not out.exists()


def test_prepare_rejects_conflicting_effective_world_identity(saved_spec, monkeypatch, capsys):
    path, out, _ = saved_spec
    prepare_batch = batch.prepare

    def mismatched_batch(destination, config):
        record = prepare_batch(destination, config)
        compose = destination / config.scenario / "environment" / "docker-compose.yaml"
        config = yaml.safe_load(compose.read_bytes())
        config["services"]["world"]["environment"]["WORLD_SCENARIO"] = "conflicting_scenario"
        compose.write_text(yaml.safe_dump(config), encoding="utf-8")
        return record

    monkeypatch.setattr(batch, "prepare", mismatched_batch)
    assert laura_trial.main(_args(path, out)) == 2
    assert "prepared_limits_or_identity_differ_from_saved_spec" in capsys.readouterr().err
    assert not (out / "laura-setup.json").exists()


@pytest.mark.parametrize(("mutation", "error"), [
    ("private", "packaged_public_descriptor_invalid"),
    ("broken_loader", "installed_public_loader_failed"),
])
def test_prepare_rejects_unsafe_or_unloadable_public_wheel(saved_spec, mutation, error, monkeypatch, capsys):
    path, out, _ = saved_spec
    stage_agent = adapter.stage_agent

    def changed_stage(source, destination, scenario, **kwargs):
        stage_agent(source, destination, scenario, **kwargs)
        if kwargs.get("verified_wheel") is not None:
            return
        pkg = destination / "refund_eval" / "scenario"
        if mutation == "private":
            descriptor = pkg / f"{scenario.id}.yaml"
            with descriptor.open("a", encoding="utf-8") as stream:
                stream.write("private_facts: should-not-be-public\n")
        else:
            schema = pkg / "schema.py"
            with schema.open("a", encoding="utf-8") as stream:
                stream.write("\ndef load_agent_scenario(name_or_path):\n    raise ValueError('broken installed loader')\n")

    monkeypatch.setattr(adapter, "stage_agent", changed_stage)
    assert laura_trial.main(_args(path, out)) == 2
    assert f"laura_prepare_failed={error}" in capsys.readouterr().err
    assert not (out / "laura-setup.json").exists()

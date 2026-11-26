from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import subprocess
import tomllib
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.harbor

pytest.importorskip("harbor.models.job.config")

from refund_eval.harbor import adapter, batch
from refund_eval.harbor.batch import NativeBatchConfig
from scripts import laura_trial
from scripts.offline_spend_guard import (
    GuardFailure,
    SharedCapsSpendGuard,
    SharedCapsThresholds,
)

_PUBLIC_SCENARIO_MEMBER = "refund_eval/scenario/train_refund_late.yaml"
_PUBLIC_SCENARIO = {
    "id": "train_refund_late",
    "task_text": "Synthetic public request for an offline fixture.",
    "start_time": "2040-01-01T00:00:00+00:00",
    "sim_weeks": 6,
    "user": {
        "name": "Fixture User",
        "email_addresses": ["fixture@example.test"],
        "phone": None,
    },
}


def _write_test_only_wheel(
    directory: Path, *, scenario=None, include_scenario=True, duplicate_scenario=False,
    alias_scenario=False, raw_wheel=None, scenario_files: dict[str, bytes] | None = None,
    scenario_name: str = "train_refund_late",
) -> tuple[Path, str]:
    """Create inert dummy wheel bytes for offline fixture preparation only."""
    directory.mkdir(parents=True)
    filename = "synthetic_agent-0.0.0-py3-none-any.whl"
    dist_info = "synthetic_agent-0.0.0.dist-info"
    files = {
        "synthetic_agent/__init__.py": b'VALUE = "synthetic"\n',
        f"{dist_info}/METADATA": b"Metadata-Version: 2.1\nName: synthetic-agent\nVersion: 0.0.0\n\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: offline-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
    }
    files.update({name: b'"""Inert offline wheel fixture."""\n' for name in adapter.REQUIRED_AGENT_WHEEL_MEMBERS})
    scenario_member = f"refund_eval/scenario/{scenario_name}.yaml"
    if scenario_files is not None:
        for name, content in scenario_files.items():
            files[f"refund_eval/scenario/{name}.yaml"] = content
    elif include_scenario:
        public_scenario = _PUBLIC_SCENARIO if scenario is None else scenario
        files[scenario_member] = yaml.safe_dump(public_scenario, sort_keys=False).encode()
        if alias_scenario:
            files[f"synthetic_agent-0.0.0.data/purelib/{scenario_member}"] = files[scenario_member]
    record_name = f"{dist_info}/RECORD"
    rows = []
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        rows.append((name, f"sha256={digest}", str(len(content))))
    rows.append((record_name, "", ""))
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    files[record_name] = record.getvalue().encode()
    wheel = directory / filename
    if raw_wheel is not None:
        wheel.write_bytes(raw_wheel)
        return wheel, hashlib.sha256(raw_wheel).hexdigest()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
            if duplicate_scenario and name == scenario_member:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, content)
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def _write_public_split_wheel(directory: Path, scenario_name="train_refund_late") -> tuple[Path, str]:
    from refund_eval.scenario.schema import load_scenario

    source = directory / "source"
    public_source = directory / "public-source"
    adapter.copy_source(source)
    adapter.stage_agent(
        source,
        public_source,
        load_scenario(scenario_name),
        scenario_name=scenario_name,
    )
    build_dir = directory / "built-wheel"
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(build_dir)],
        cwd=public_source,
        check=True,
        capture_output=True,
        text=True,
    )
    [wheel] = build_dir.glob("*.whl")
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def _source_repo(root: Path, monkeypatch) -> Path:
    files = {
        "refund_eval/current.py": "CURRENT_SOURCE = 'fixture'\n",
        "scripts/laura_trial.py": "LAUNCHER = 'fixture'\n",
        "scout/run.py": "SCOUT = 'fixture'\n",
        ".python-version": "3.14\n",
        "pyproject.toml": "[project]\nname = 'fixture'\n",
        "uv.lock": "version = 1\n",
        "integrations/harbor/pyproject.toml": "[project]\nname = 'fixture-harbor'\n",
        "integrations/harbor/uv.lock": "version = 1\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / ".gitignore").write_text("refund_eval/private.json\n", encoding="utf-8")
    (root / "refund_eval/private.json").write_text("private fixture data\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(
        [
            "git", "-C", str(root), "add", "--",
            *files.keys(),
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(laura_trial, "REPO_ROOT", root)
    monkeypatch.setattr(
        laura_trial,
        "_runtime_identity",
        lambda: {
            "python": "3.14-test",
            "packages": {
                "harbor": {"version": "0.26.0", "sha256": "a" * 64},
                "modal": {"version": "1.6.1", "sha256": "b" * 64},
            },
        },
    )
    return root


def _native_setup(
    tmp_path: Path, monkeypatch, *, scenario=None, include_scenario=True,
    duplicate_scenario=False, alias_scenario=False, raw_wheel=None, scenario_id="train_refund_late", sim_weeks=6,
) -> SimpleNamespace:
    source_root = _source_repo(tmp_path / "source-root", monkeypatch)
    public_scenario = {**_PUBLIC_SCENARIO, "id": scenario_id} if scenario is None else scenario
    wheel, wheel_sha256 = _write_test_only_wheel(
        tmp_path / "wheel", scenario=public_scenario, scenario_name=scenario_id,
        include_scenario=include_scenario, duplicate_scenario=duplicate_scenario,
        alias_scenario=alias_scenario, raw_wheel=raw_wheel,
    )
    config = NativeBatchConfig.model_validate(
        {
            "execution": "modal",
            "scenario": scenario_id,
            "data_mode": "fixture",
            "research_mode": "saved",
            "model": "openrouter/test-model",
            "models": ["openrouter/test-model"],
            "cost_limit": {"openrouter/test-model": 10.0},
            "world_cost_limit": 10.0,
            "max_turns": 50,
            "sim_weeks": sim_weeks,
            "time_limit": 120,
            "history_tokens": 0,
            "budget_notice": "daily",
            "trials": 1,
            "concurrency": 1,
            "seed": 0,
            "agent_wheel": wheel,
            "agent_wheel_sha256": wheel_sha256,
        }
    )
    record = batch.prepare(tmp_path / "prepared", config)
    config_path = record.with_name("job.yaml")
    job = yaml.safe_load(config_path.read_bytes())
    task_path = Path(job["tasks"][0]["path"])
    instruction = tmp_path / "instructions" / "custom.md"
    instruction.parent.mkdir()
    instruction.write_text("Keep this instruction unchanged.\n", encoding="utf-8")
    skill = tmp_path / "skills" / "custom"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Fixture skill only.\n", encoding="utf-8")
    job["extra_instruction_paths"] = [str(instruction)]
    job["agents"][0]["skills"] = [str(skill)]
    job["verifier"] = {
        "import_path": "refund_eval.harbor.fixture_verifier:FixtureVerifier",
        "kwargs": {"grading_policy": "fixture-custom"},
        "env": {"FIXTURE_VERIFIER": "test-only"},
        "max_timeout_sec": 321,
    }
    config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    return SimpleNamespace(
        source_root=source_root,
        config_path=config_path,
        manifest_path=tmp_path / "private" / "laura-setup.json",
        task_path=task_path,
        jobs_dir=Path(job["jobs_dir"]),
        instruction=instruction,
        skill=skill,
    )


def _install_staged_scenario_wheel(
    bundle: SimpleNamespace, scenario_files: dict[str, bytes], directory: Path
) -> None:
    wheel, wheel_sha256 = _write_test_only_wheel(
        directory, include_scenario=False, scenario_files=scenario_files
    )
    staged_agent = bundle.task_path / "environment" / "agent"
    staged_wheel = next((staged_agent / "wheels").glob("*.whl"))
    wheel_manifest_path = staged_agent / "agent-wheel.json"
    wheel_manifest = json.loads(wheel_manifest_path.read_text())
    previous_sha256 = wheel_manifest["sha256"]
    staged_wheel.write_bytes(wheel.read_bytes())
    wheel_manifest["sha256"] = wheel_sha256
    wheel_manifest_path.write_text(json.dumps(wheel_manifest), encoding="utf-8")

    task_manifest_path = bundle.task_path / "task.toml"
    task_manifest = task_manifest_path.read_text(encoding="utf-8")
    assert task_manifest.count(previous_sha256) == 1
    task_manifest_path.write_text(
        task_manifest.replace(previous_sha256, wheel_sha256), encoding="utf-8"
    )


def _save(bundle: SimpleNamespace) -> None:
    laura_trial.save_setup(bundle.config_path, bundle.manifest_path)


def _run_args(bundle: SimpleNamespace, job_name: str = "laura-fresh") -> list[str]:
    return [
        "run",
        "--manifest", str(bundle.manifest_path),
        "--job-name", job_name,
        "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "25",
    ]


def _native_run_input(tmp_path: Path, scenario="train_refund_late") -> SimpleNamespace:
    wheel, wheel_sha256 = _write_test_only_wheel(tmp_path / "native-wheel")
    values = {
        "execution": "modal",
        "scenario": scenario,
        "data_mode": "fixture",
        "research_mode": "saved",
        "model": "openrouter/target-test",
        "models": ["openrouter/target-test"],
        "simulator_model": "openrouter/simulator-test",
        "grader_model": "openrouter/grader-test",
        "cost_limit": {"openrouter/target-test": 10.0},
        "world_cost_limit": 10.0,
        "max_turns": 50,
        "sim_weeks": 6,
        "time_limit": 120,
        "history_tokens": 0,
        "budget_notice": "daily",
        "trials": 1,
        "concurrency": 1,
        "seed": 0,
        "agent_wheel": str(wheel),
        "agent_wheel_sha256": wheel_sha256,
    }
    input_path = tmp_path / "native-config.json"
    input_path.write_text(json.dumps(values), encoding="utf-8")
    return SimpleNamespace(input_path=input_path, prepared_dir=tmp_path / "prepared", values=values)


def test_save_and_verify_are_identity_only_and_preserve_completed_source_job(
    tmp_path, monkeypatch, capsys
):
    bundle = _native_setup(tmp_path, monkeypatch)
    existing = bundle.jobs_dir / "refund-native-train_refund_late"
    existing.mkdir(parents=True)
    marker = existing / "result.json"
    marker.write_text("completed source output\n", encoding="utf-8")

    assert laura_trial.main([
        "save", "--config", str(bundle.config_path), "--manifest", str(bundle.manifest_path)
    ]) == 0
    assert capsys.readouterr().out == (
        "laura_setup_saved=identity_and_public_schema public_validation=true "
        "startup_validated=false launch_approval=false\n"
    )
    manifest_bytes = bundle.manifest_path.read_bytes()
    public_validation = json.loads(manifest_bytes)["public_validation"]
    staged_wheel = next((bundle.task_path / "environment" / "agent" / "wheels").glob("*.whl"))
    assert public_validation == {
        "status": "passed",
        "checker": "refund_eval.scenario.schema.AgentScenario.model_validate",
        "scenario": "train_refund_late",
        "wheel_path": staged_wheel.relative_to(bundle.task_path).as_posix(),
        "wheel_sha256": hashlib.sha256(staged_wheel.read_bytes()).hexdigest(),
        "scenario_member": _PUBLIC_SCENARIO_MEMBER,
        "scenario_sha256": hashlib.sha256(
            yaml.safe_dump(_PUBLIC_SCENARIO, sort_keys=False).encode()
        ).hexdigest(),
    }
    assert "openrouter/test-model" not in manifest_bytes.decode()
    assert "OPENROUTER_API_KEY" not in manifest_bytes.decode()

    marker.write_text("changed runtime output\n", encoding="utf-8")
    assert laura_trial.main(["verify", "--manifest", str(bundle.manifest_path)]) == 0
    assert capsys.readouterr().out == (
        "laura_setup_verified=identity_and_public_schema public_validation=true "
        "startup_validated=false launch_approval=false\n"
    )
    assert marker.read_text(encoding="utf-8") == "changed runtime output\n"
    assert sorted(path.name for path in bundle.jobs_dir.iterdir()) == [
        "refund-native-train_refund_late"
    ]

    assert laura_trial.main([
        "save", "--config", str(bundle.config_path), "--manifest", str(bundle.manifest_path)
    ]) == 2
    assert bundle.manifest_path.read_bytes() == manifest_bytes
    assert capsys.readouterr().err == "laura_setup_failed=identity_or_job_validation\n"


def test_fresh_run_changes_only_job_name_and_surfaces_links_before_completion(
    tmp_path, monkeypatch, capsys
):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    saved = laura_trial.verify_setup(bundle.manifest_path)
    expected = saved.model_dump(mode="json")
    expected["job_name"] = "laura-fresh"
    captured = {}
    events = []

    def guarded_run(config, thresholds, **hooks):
        events.append("guarded_runner")
        captured["config"] = config
        log_path = bundle.jobs_dir / "laura-fresh" / "job.log"
        hooks["on_job_created"](SimpleNamespace(_log_output_path=log_path))
        hooks["on_sandbox_created"](SimpleNamespace(
            _app=SimpleNamespace(get_dashboard_url=lambda: "https://modal.com/apps/ap-offline-test")
        ))
        hooks["before_job_run"](SimpleNamespace())
        assert hooks["total_time_limit_seconds"] is None
        print("guarded_harbor_job=complete")
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    code = laura_trial.main(_run_args(bundle))

    assert code == 0
    assert captured["config"].model_dump(mode="json") == expected
    assert captured["config"].verifier.kwargs == {"grading_policy": "fixture-custom"}
    assert captured["config"].agents[0].kwargs == saved.agents[0].kwargs
    assert captured["config"].tasks[0].path == saved.tasks[0].path
    output = capsys.readouterr().out.splitlines()
    assert output[0] == (
        "laura_setup_verified=identity_and_public_schema public_validation=true "
        "startup_validated=false launch_approval=false"
    )
    assert output[1] == f"harbor_job_log_uri={ (bundle.jobs_dir / 'laura-fresh' / 'job.log').resolve().as_uri() }"
    assert output[2] == "modal_dashboard_url=https://modal.com/apps/ap-offline-test"
    assert output[3] == "guarded_harbor_job=complete"


def test_native_run_prepares_distinct_roles_and_records_effective_parameters(
    tmp_path, monkeypatch, capsys
):
    _source_repo(tmp_path / "source-root", monkeypatch)
    native = _native_run_input(tmp_path)
    original_input = native.input_path.read_bytes()
    captured = {}

    def guarded_run(config, thresholds, **hooks):
        captured["config"] = config
        captured["thresholds"] = thresholds
        captured["total"] = hooks["total_time_limit_seconds"]
        hooks["before_job_run"](SimpleNamespace())
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main([
        "run",
        "--run-config", str(native.input_path),
        "--prepared-dir", str(native.prepared_dir),
        "--job-name", "native-fresh",
        "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "25",
    ]) == 0

    assert native.input_path.read_bytes() == original_input
    config = captured["config"]
    assert captured["total"] is None
    assert config.agents[0].model_name == "openrouter/target-test"
    assert config.agents[0].kwargs["time_limit"] == 120
    task_path = Path(config.tasks[0].path)
    task = tomllib.loads((task_path / "task.toml").read_text(encoding="utf-8"))
    assert task["agent"]["timeout_sec"] == 132
    assert task["metadata"]["simulator_model"] == "openrouter/simulator-test"
    assert task["metadata"]["grader_model"] == "openrouter/grader-test"
    expected = {
        "target_model": "openrouter/target-test",
        "models": ["openrouter/target-test"],
        "simulator_model": "openrouter/simulator-test",
        "grader_model": "openrouter/grader-test",
        "sim_weeks": 6,
        "max_turns": 50,
        "agent_cost_limit_usd": "10.0",
        "world_cost_limit_usd": "10.0",
        "worker_time_limit_seconds": 120,
        "requested_total_time_limit_seconds": None,
        "effective_enforced_total_time_limit_seconds": None,
        "total_time_limit_enforcement": "not_requested",
        "openrouter_trial_delta_usd": "25",
        "openrouter_key_ceiling_usd": "50",
        "cleanup_timeout_seconds": 10.0,
        "effective_parameters_complete": True,
        "unavailable_fields": [],
    }
    run_record = json.loads((native.prepared_dir / "run.json").read_text(encoding="utf-8"))
    result_record = json.loads(
        (Path(config.jobs_dir) / config.job_name / "effective_parameters.json").read_text(encoding="utf-8")
    )
    assert run_record["effective_parameters"] == expected
    assert result_record == expected


def test_native_daycare_run_config_requires_explicit_target_cap(tmp_path, monkeypatch, capsys):
    native = _native_run_input(tmp_path, scenario="daycare_reimbursement")
    implicit = {
        **native.values,
        "openrouter_key_mode": "shared_roles",
        "shared_allowance_usd": 10.0,
        "agent_max_output_tokens": 4096,
        "sim_weeks": 10.0,
    }
    implicit.pop("cost_limit")
    native.input_path.write_text(json.dumps(implicit), encoding="utf-8")

    def unexpected_prepare(*args, **kwargs):
        pytest.fail("implicit daycare target cap reached native preparation")

    monkeypatch.setattr(laura_trial.batch, "prepare", unexpected_prepare)
    assert laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "daycare-implicit-cap",
    ]) == 2
    assert capsys.readouterr().err == "laura_setup_failed=identity_or_job_validation\n"
    assert not native.prepared_dir.exists()

    explicit = laura_trial.batch.NativeBatchConfig.model_validate_json(json.dumps({
        **implicit, "cost_limit": 5.0, "shared_allowance_usd": 15.0,
    }))
    assert explicit.cost_limit == 5.0
    assert explicit.world_cost_limit == 10.0
    assert explicit.shared_allowance_usd == 15.0


def test_split_launcher_succeeds_and_reuses_saved_setup_with_sanitized_effective_records(
    tmp_path, monkeypatch
):
    _source_repo(tmp_path / "source-root", monkeypatch)
    native = _native_run_input(tmp_path)
    wheel, digest = _write_public_split_wheel(tmp_path / "split-wheel")
    native.values.update({
        "openrouter_key_mode": "split",
        "agent_max_output_tokens": 2048,
        "cost_limit": 50,
        "world_cost_limit": 100,
        "agent_wheel": str(wheel),
        "agent_wheel_sha256": digest,
    })
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_AGENT_API_KEY", "fake-distinct-agent-key")
    monkeypatch.setenv("OPENROUTER_WORLD_API_KEY", "fake-distinct-world-key")
    guarded_calls = []

    def guarded_run(config, thresholds, **hooks):
        guarded_calls.append((config.job_name, thresholds))
        hooks["before_job_run"](SimpleNamespace())
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "split-fresh",
    ]) == 0

    run_record = json.loads((native.prepared_dir / "run.json").read_text(encoding="utf-8"))
    fresh_effective_path = native.prepared_dir / "jobs" / "split-fresh" / "effective_parameters.json"
    fresh_effective = json.loads(fresh_effective_path.read_text(encoding="utf-8"))
    for record in (run_record, fresh_effective):
        effective = record.get("effective_parameters", record)
        assert effective["openrouter_key_mode"] == "split"
        assert effective["agent_key_ceiling_usd"] == 50
        assert effective["world_key_ceiling_usd"] == 100
        assert effective["agent_max_output_tokens"] == 2048
        assert "fake-distinct-agent-key" not in json.dumps(record)
        assert "fake-distinct-world-key" not in json.dumps(record)

    manifest = native.prepared_dir / "laura-setup.json"
    assert laura_trial.main([
        "run", "--manifest", str(manifest), "--job-name", "split-reused",
    ]) == 0
    reused = json.loads(
        (native.prepared_dir / "jobs" / "split-reused" / "effective_parameters.json").read_text(
            encoding="utf-8"
        )
    )
    assert reused == fresh_effective
    assert [name for name, _ in guarded_calls] == ["split-fresh", "split-reused"]
    assert "fake-distinct-agent-key" not in manifest.read_text(encoding="utf-8")
    assert "fake-distinct-world-key" not in manifest.read_text(encoding="utf-8")


@pytest.mark.parametrize(("remaining", "expected_result"), [(0, 2), (40, 0)])
def test_shared_caps_refuses_before_job_and_binds_immutable_headroom(
    tmp_path, monkeypatch, capsys, remaining, expected_result
):
    _source_repo(tmp_path / "source-root", monkeypatch)
    native = _native_run_input(tmp_path)
    wheel, digest = _write_public_split_wheel(tmp_path / "shared-caps-wheel")
    native.values.update({
        "openrouter_key_mode": "shared_caps",
        "agent_max_output_tokens": 2048,
        "requested_deadline": "2027-02-04",
        "cost_limit": 50,
        "world_cost_limit": 100,
        "agent_wheel": str(wheel),
        "agent_wheel_sha256": digest,
    })
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-shared-caps-key")
    created = []

    def guarded_run(config, thresholds, **hooks):
        assert isinstance(thresholds, SharedCapsThresholds)
        assert config.agents[0].env["REFUND_EVAL_SHARED_CAPS_HEADROOM_BOUND"] == "false"
        manifest = native.prepared_dir / "laura-setup.json"
        manifest_before = manifest.read_bytes()
        original_task = Path(config.tasks[0].path)
        original_toml = (original_task / "task.toml").read_bytes()
        original_compose = yaml.safe_load((original_task / "environment" / "docker-compose.yaml").read_text())
        assert original_compose["services"]["world"]["environment"][
            "REFUND_EVAL_SHARED_CAPS_HEADROOM_BOUND"
        ] == "false"

        async def probe():
            return {"data": {
                "usage": 0, "limit": 80, "limit_remaining": remaining, "limit_reset": None,
                "is_management_key": False, "is_provisioning_key": False, "include_byok_in_limit": True,
                "budgets": [], "effective_budget": None,
            }}

        class FakeJob:
            is_resuming = False

            async def run(self):
                return 0

        async def factory(run_config):
            created.append(run_config.tasks[0].path)
            return FakeJob()

        guard = SharedCapsSpendGuard(thresholds, openrouter_probe=probe)
        try:
            result = asyncio.run(laura_trial.guarded_harbor.run_guarded_job(
                config, thresholds, guard=guard, job_factory=factory, **hooks
            ))
        except GuardFailure:
            result = 2
        assert manifest.read_bytes() == manifest_before
        assert (original_task / "task.toml").read_bytes() == original_toml
        if remaining == 0:
            assert created == []
            assert config.tasks[0].path == original_task
        else:
            assert len(created) == 1
            runtime_task = Path(created[0])
            assert runtime_task != original_task
            runtime_config = tomllib.loads((runtime_task / "task.toml").read_text())
            shared_config = json.loads(runtime_config["environment"]["env"]["REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG"])
            assert shared_config["shared_allowance_usd"] == "40"
            assert config.agents[0].env["REFUND_EVAL_SHARED_CAPS_HEADROOM_BOUND"] == "true"
            compose = yaml.safe_load((runtime_task / "environment" / "docker-compose.yaml").read_text())
            assert compose["services"]["world"]["environment"]["REFUND_EVAL_SHARED_CAPS_HEADROOM_BOUND"] == "true"
            assert config.agents[0].kwargs["requested_deadline"].isoformat() == "2027-02-04"
        return result

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    result = laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", f"shared-caps-{remaining}",
    ])
    capsys.readouterr()
    assert result == expected_result
    if remaining > 0:
        record = json.loads((native.prepared_dir / "jobs" / f"shared-caps-{remaining}" / "effective_parameters.json").read_text())
        assert record["shared_allowance_usd"] == "40"
        assert record["shared_allowance_source"] == "verified_key_headroom"


def test_actual_native_launcher_keeps_cleanup_after_shared_caps_guard_failure(tmp_path):
    native = _native_run_input(tmp_path)
    wheel, digest = _write_public_split_wheel(tmp_path / "shared-caps-wheel")
    native.values.update({
        "openrouter_key_mode": "shared_caps",
        "usage_reporting_mode": "usage_only",
        "disable_application_price_cutoffs": True,
        "agent_max_output_tokens": 2048,
        "cost_limit": 50,
        "world_cost_limit": 100,
        "agent_wheel": str(wheel),
        "agent_wheel_sha256": digest,
    })
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")
    config = NativeBatchConfig.model_validate_json(native.input_path.read_bytes())
    run_record = batch.prepare(native.prepared_dir, config)
    manifest_path = native.prepared_dir / "laura-setup.json"
    laura_trial.save_setup(run_record.with_name("job.yaml"), manifest_path)

    repo_root = Path(__file__).resolve().parents[1]
    events_path = tmp_path / "launcher-events.jsonl"
    environment = os.environ.copy()
    environment.update({
        "UV_OFFLINE": "1",
        "OPENROUTER_API_KEY": "offline-launcher-fixture-key",
        "PYTHONPATH": str(repo_root / "tests" / "fixtures" / "native_launcher_mock"),
        "REFUND_EVAL_NATIVE_LAUNCHER_MOCK_EVENTS": str(events_path),
    })
    result = subprocess.run(
        [
            str(repo_root / "scripts" / "harbor"), "python", "-m", "scripts.laura_trial", "run",
            "--manifest", str(manifest_path), "--job-name", "mat-197-cleanup-fixture",
            "--probe-timeout", "0.5", "--poll-interval", "0.05",
            "--max-sample-age", "1.5", "--cleanup-timeout", "1.0",
        ],
        cwd=repo_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "guard_failed=shared_caps requires an ordinary non-resetting OpenRouter key cleanup_uncertain=false" in result.stderr, (
        result.stdout + result.stderr
    )
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    names = [event["event"] for event in events]
    erosion_sample = next(event for event in events if event["event"] == "probe" and event["count"] == 3)
    assert erosion_sample["remaining"] == "0"
    assert "job_continued_after_headroom_erosion" in names
    assert "collection_succeeded" in names
    assert "collection_failed" not in names
    assert names.index("collection_succeeded") < names.index("sandbox_terminated")
    assert "private_trace_failed" not in names
    assert names.index("private_trace_collected") < names.index("sandbox_terminated")
    collected = events_path.with_suffix(".collected") / "runner.log"
    assert collected.read_text(encoding="utf-8") == "fixture Harbor log\n"
    parameters = json.loads(
        (native.prepared_dir / "jobs" / "mat-197-cleanup-fixture" / "effective_parameters.json").read_text()
    )
    assert parameters["shared_allowance_source"] == "not_enforced_by_benchmark"
    assert not (native.prepared_dir / "runtime-inputs" / "mat-197-cleanup-fixture").exists()


@pytest.mark.parametrize("scenario", ["train_refund_late", "daycare_reimbursement"])
def test_shared_roles_routes_one_key_and_rejects_stale_saved_source_before_guard(
    tmp_path, monkeypatch, capsys, scenario
):
    source_root = _source_repo(tmp_path / "source-root", monkeypatch)
    native = _native_run_input(tmp_path, scenario)
    wheel, digest = _write_public_split_wheel(tmp_path / "shared-role-wheel", scenario)
    expected_deadline = "2027-02-04" if scenario == "train_refund_late" else None
    expected_target_cap = 50.0 if scenario == "train_refund_late" else 5.0
    expected_world_cap = 100.0 if scenario == "train_refund_late" else 10.0
    expected_allowance = 90.0 if scenario == "train_refund_late" else 15.0
    expected_role_caps = {"target": str(expected_target_cap), "world": str(expected_world_cap)}
    native.values.update({
        "openrouter_key_mode": "shared_roles",
        "shared_allowance_usd": expected_allowance,
        "agent_max_output_tokens": 2048,
        "requested_deadline": expected_deadline,
        "cost_limit": expected_target_cap,
        "world_cost_limit": expected_world_cap,
        "sim_weeks": 10 if scenario == "daycare_reimbursement" else native.values["sim_weeks"],
        "agent_wheel": str(wheel),
        "agent_wheel_sha256": digest,
    })
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-shared-role-key")
    guarded_calls = []
    probe_calls = []

    def guarded_run(config, thresholds, **hooks):
        guarded_calls.append(config.job_name)
        assert thresholds.shared_allowance_usd == expected_allowance
        saved_identity_verified = hooks["saved_identity_verified"]
        assert saved_identity_verified is True
        if scenario == "daycare_reimbursement":
            with pytest.raises(
                laura_trial.guarded_harbor.LauncherFailure,
                match="daycare_reimbursement_requires_saved_identity",
            ):
                laura_trial.guarded_harbor.validate_job_config(config, expected_shared_roles=True)
        laura_trial.guarded_harbor.validate_job_config(
            config, expected_shared_roles=True, saved_identity_verified=saved_identity_verified
        )
        if expected_deadline is None:
            assert config.agents[0].kwargs.get("requested_deadline") is None
        else:
            assert config.agents[0].kwargs["requested_deadline"].isoformat() == expected_deadline
        assert config.agents[0].env["OPENROUTER_API_KEY"] == "${OPENROUTER_API_KEY}"
        assert "OPENROUTER_AGENT_API_KEY" not in config.agents[0].env
        task_path = Path(config.tasks[0].path)
        task_env = tomllib.loads((task_path / "task.toml").read_text(encoding="utf-8"))["environment"]["env"]
        assert float(task_env["WORLD_SIM_WEEKS"]) == native.values["sim_weeks"]
        compose = yaml.safe_load((task_path / "environment" / "docker-compose.yaml").read_bytes())
        main_mounts = compose["services"]["main"].get("volumes", [])
        world_mounts = compose["services"]["world"].get("volumes", [])
        assert any("role-budget" in str(mount) for mount in main_mounts)
        assert any("role-budget" in str(mount) for mount in world_mounts)
        assert all("world-logs" not in str(mount) for mount in main_mounts)

        class FakeJob:
            is_resuming = False

            async def run(self):
                return 0

        async def probe():
            probe_calls.append("probe")
            return {"data": {
                "usage": 0,
                "limit": 120,
                "limit_remaining": 120,
                "limit_reset": None,
                "is_management_key": False,
                "is_provisioning_key": False,
                "include_byok_in_limit": True,
            }}

        async def factory(_config):
            return FakeJob()

        guard = laura_trial.guarded_harbor.SharedRoleSpendGuard(thresholds, openrouter_probe=probe)
        return asyncio.run(laura_trial.guarded_harbor.run_guarded_job(
            config, thresholds, guard=guard, job_factory=factory, **hooks
        ))

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "shared-role-fresh",
    ]) == 0
    capsys.readouterr()

    manifest = native.prepared_dir / "laura-setup.json"
    run_record = native.prepared_dir / "run.json"
    effective_record = native.prepared_dir / "jobs" / "shared-role-fresh" / "effective_parameters.json"
    initial_effective = json.loads(effective_record.read_text(encoding="utf-8"))
    assert initial_effective["openrouter_key_mode"] == "shared_roles"
    assert initial_effective["shared_allowance_usd"] == str(expected_allowance)
    assert initial_effective["role_caps_usd"] == expected_role_caps
    assert initial_effective["runtime_metadata_confirmed"] is True
    assert initial_effective["shared_allowance_source"] == "operator_attested_allocation"
    assert initial_effective.get("requested_deadline") == expected_deadline
    assert initial_effective["sim_weeks"] == native.values["sim_weeks"]
    assert initial_effective["key_limit_usd"] == "120"
    assert initial_effective["key_remaining_usd"] == "120"
    assert initial_effective["key_usage_baseline_usd"] == "0"
    assert initial_effective["budget_remaining_usd"] == []
    assert initial_effective["effective_budget_remaining_usd"] is None
    run_effective = json.loads(run_record.read_text(encoding="utf-8"))["effective_parameters"]
    assert run_effective["openrouter_key_mode"] == "shared_roles"
    assert run_effective["shared_allowance_usd"] == str(expected_allowance)
    assert run_effective["role_caps_usd"] == expected_role_caps
    assert run_effective.get("requested_deadline") == expected_deadline
    for artifact in (manifest, run_record, effective_record):
        assert "fake-shared-role-key" not in artifact.read_text(encoding="utf-8")

    manifest_bytes = manifest.read_bytes()
    assert laura_trial.main([
        "run", "--manifest", str(manifest), "--job-name", "shared-role-reused",
    ]) == 0
    reused_effective = json.loads(
        (native.prepared_dir / "jobs" / "shared-role-reused" / "effective_parameters.json").read_text(
            encoding="utf-8"
        )
    )
    assert reused_effective["openrouter_key_mode"] == "shared_roles"
    assert reused_effective["shared_allowance_usd"] == str(expected_allowance)
    assert reused_effective.get("requested_deadline") == expected_deadline
    assert reused_effective == initial_effective
    assert manifest.read_bytes() == manifest_bytes

    capsys.readouterr()
    manifest_document = json.loads(manifest_bytes)
    config_path = Path(manifest_document["config"]["path"])
    config_bytes = config_path.read_bytes()
    job_document = yaml.safe_load(config_bytes)
    job_document["agents"][0]["kwargs"]["requested_deadline"] = "2027-02-05"
    mismatched_config = yaml.safe_dump(job_document, sort_keys=False).encode("utf-8")
    config_path.write_bytes(mismatched_config)
    manifest_document["config"]["size"] = len(mismatched_config)
    manifest_document["config"]["sha256"] = hashlib.sha256(mismatched_config).hexdigest()
    manifest.write_text(json.dumps(manifest_document, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    guarded_count = len(guarded_calls)
    probe_count = len(probe_calls)
    assert laura_trial.main([
        "run", "--manifest", str(manifest), "--job-name", "shared-role-deadline-mismatch",
    ]) == 2
    assert "laura_setup_failed=saved_requested_deadline_metadata_mismatch" in capsys.readouterr().err
    assert len(guarded_calls) == guarded_count
    assert len(probe_calls) == probe_count

    config_path.write_bytes(config_bytes)
    manifest.write_bytes(manifest_bytes)

    (source_root / "refund_eval/current.py").write_text("CURRENT_SOURCE = 'changed'\n", encoding="utf-8")
    assert laura_trial.main([
        "run", "--manifest", str(manifest), "--job-name", "shared-role-stale",
    ]) == 2
    assert guarded_calls == ["shared-role-fresh", "shared-role-reused"]
    assert capsys.readouterr().err == "laura_setup_failed=identity_or_job_validation\n"


def test_native_run_without_optional_inputs_records_resolved_defaults(
    tmp_path, monkeypatch
):
    _source_repo(tmp_path / "source-root", monkeypatch)
    native = _native_run_input(tmp_path)
    native.values["cost_limit"] = 1.0
    native.values["world_cost_limit"] = 1.0
    for name in ("model", "models", "sim_weeks", "max_turns", "time_limit"):
        native.values.pop(name)
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")
    defaults = batch.NativeBatchConfig.model_validate_json(native.input_path.read_bytes())

    def guarded_run(config, thresholds, **hooks):
        hooks["before_job_run"](SimpleNamespace())
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "native-defaults", "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "25",
    ]) == 0

    effective = {
        "target_model": defaults.model,
        "models": [defaults.model],
        "simulator_model": defaults.simulator_model,
        "grader_model": defaults.grader_model,
        "sim_weeks": defaults.sim_weeks,
        "max_turns": defaults.max_turns,
        "agent_cost_limit_usd": "1.0",
        "world_cost_limit_usd": "1.0",
        "worker_time_limit_seconds": defaults.time_limit,
        "effective_parameters_complete": True,
        "unavailable_fields": [],
        "requested_total_time_limit_seconds": None,
        "effective_enforced_total_time_limit_seconds": None,
        "total_time_limit_enforcement": "not_requested",
        "openrouter_trial_delta_usd": "25",
        "openrouter_key_ceiling_usd": "50",
        "cleanup_timeout_seconds": 10.0,
    }
    run_record = json.loads((native.prepared_dir / "run.json").read_text(encoding="utf-8"))
    job = yaml.safe_load((native.prepared_dir / "job.yaml").read_bytes())
    assert run_record["effective_parameters"] == effective
    assert job["agents"][0]["model_name"] == defaults.model
    assert job["agents"][0]["kwargs"]["time_limit"] == defaults.time_limit
    assert run_record["effective_parameters"]["sim_weeks"] == defaults.sim_weeks
    assert json.loads(
        (native.prepared_dir / "jobs" / "native-defaults" / "effective_parameters.json").read_text(
            encoding="utf-8"
        )
    ) == effective


def test_native_run_records_730_day_simulation_horizon(tmp_path, monkeypatch):
    _source_repo(tmp_path / "source-root", monkeypatch)
    native = _native_run_input(tmp_path)
    horizon = 730 / 7
    native.values["sim_weeks"] = horizon
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")

    def guarded_run(config, thresholds, **hooks):
        hooks["before_job_run"](SimpleNamespace())
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "native-long-horizon", "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "25",
    ]) == 0

    run_record = json.loads((native.prepared_dir / "run.json").read_text(encoding="utf-8"))
    effective_record = json.loads(
        (native.prepared_dir / "jobs" / "native-long-horizon" / "effective_parameters.json").read_text(
            encoding="utf-8"
        )
    )
    task_metadata = tomllib.loads(
        (native.prepared_dir / "train_refund_late" / "task.toml").read_text(encoding="utf-8")
    )["metadata"]
    assert run_record["effective_parameters"]["sim_weeks"] == horizon
    assert effective_record["sim_weeks"] == horizon
    assert task_metadata["sim_weeks"] == horizon


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"models": ["openrouter/target-test", "openrouter/another-test"],
          "cost_limit": {"openrouter/target-test": 10, "openrouter/another-test": 10}},
         "target_model_sweep_unsupported"),
        ({"model": "anthropic/target-test", "models": None, "cost_limit": 10},
         "all_active_models_must_use_openrouter"),
        ({"simulator_model": "anthropic/simulator"}, "all_active_models_must_use_openrouter"),
        ({"execution": "docker"}, "guarded_native_runs_require_modal"),
        ({"trials": 2}, "guarded_native_runs_require_one_attempt_and_concurrency"),
        ({"inbox_snapshot": "test-inbox.json"}, "guarded_native_runs_do_not_support_resume_or_inbox"),
        ({"resume_log": "resume.eval"}, "guarded_native_runs_do_not_support_resume_or_inbox"),
        ({"cost_limit": {"openrouter/target-test": 20}},
         "agent_and_world_caps_exceed_runtime_trial_delta"),
        ({"trial_spend_cap_usd": 30, "key_spend_cap_usd": 40},
         "native_trial_planning_cap_conflicts_with_runtime_guard"),
        ({"trial_spend_cap_usd": 20, "key_spend_cap_usd": 51},
         "native_key_planning_cap_exceeds_supported_guard"),
    ],
)
def test_native_config_rejections_precede_preparation_and_guard(
    tmp_path, monkeypatch, capsys, change, reason
):
    native = _native_run_input(tmp_path)
    native.values.update(change)
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")
    prepare_calls = []
    guard_calls = []
    monkeypatch.setattr(laura_trial.batch, "prepare", lambda *args, **kwargs: prepare_calls.append(True))
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )

    assert laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "native-fresh", "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "25",
    ]) == 2
    assert prepare_calls == []
    assert guard_calls == []
    assert not native.prepared_dir.exists()
    assert f"laura_setup_failed={reason}" in capsys.readouterr().err


@pytest.mark.parametrize("bad_total", [0, -1, float("nan"), float("inf"), True])
def test_invalid_native_total_values_reject_before_preparation_and_guard(
    tmp_path, monkeypatch, bad_total
):
    native = _native_run_input(tmp_path)
    native.values["total_time_limit_seconds"] = bad_total
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")
    prepare_calls = []
    guard_calls = []
    monkeypatch.setattr(laura_trial.batch, "prepare", lambda *args, **kwargs: prepare_calls.append(True))
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )
    assert laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "native-fresh", "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "25",
    ]) == 2
    assert prepare_calls == [] and guard_calls == []
    assert not native.prepared_dir.exists()


@pytest.mark.parametrize(
    ("key_ceiling", "trial_delta"),
    [("49", "25"), ("50", "51")],
)
def test_incompatible_guard_thresholds_reject_before_native_preparation(
    tmp_path, monkeypatch, key_ceiling, trial_delta
):
    native = _native_run_input(tmp_path)
    prepare_calls = []
    guard_calls = []
    monkeypatch.setattr(laura_trial.batch, "prepare", lambda *args, **kwargs: prepare_calls.append(True))
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )
    assert laura_trial.main([
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "native-fresh", "--openrouter-key-ceiling", key_ceiling,
        "--openrouter-trial-delta", trial_delta,
    ]) != 0
    assert prepare_calls == [] and guard_calls == []
    assert not native.prepared_dir.exists()


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("WORLD_SIMULATOR_MODEL", "anthropic/simulator", "saved_role_models_must_use_openrouter"),
        ("WORLD_COST_LIMIT", "20", "saved_agent_and_world_caps_exceed_runtime_trial_delta"),
        ("WORLD_COST_LIMIT", "0", "saved_native_effective_values_invalid_or_unavailable"),
        ("WORLD_MAX_TURNS", "73.0", "saved_max_turns_invalid"),
        ("WORLD_MAX_TURNS", 73.0, "saved_max_turns_invalid"),
    ],
)
def test_saved_native_compatibility_is_checked_before_guard(
    tmp_path, monkeypatch, capsys, field, value, reason
):
    bundle = _native_setup(tmp_path, monkeypatch)
    compose_path = bundle.task_path / "environment" / "docker-compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["world"]["environment"][field] = value
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    _save(bundle)
    guard_calls = []
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )

    assert laura_trial.main(_run_args(bundle)) == 2
    assert guard_calls == []
    assert f"laura_setup_failed={reason}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("config_total", "cli_total"),
    [(20, None), (None, "30"), (20, "30")],
)
def test_native_total_limit_refuses_before_preparation_and_guard(
    tmp_path, monkeypatch, capsys, config_total, cli_total
):
    native = _native_run_input(tmp_path)
    if config_total is not None:
        native.values["total_time_limit_seconds"] = config_total
    native.input_path.write_text(json.dumps(native.values), encoding="utf-8")
    prepare_calls = []
    guard_calls = []
    monkeypatch.setattr(
        laura_trial.batch, "prepare", lambda *args, **kwargs: prepare_calls.append(True)
    )
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )
    args = [
        "run", "--run-config", str(native.input_path), "--prepared-dir", str(native.prepared_dir),
        "--job-name", "native-fresh", "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "25",
    ]
    if cli_total is not None:
        args.extend(["--total-time-limit-seconds", cli_total])
    assert laura_trial.main(args) == 2
    assert prepare_calls == []
    assert guard_calls == []
    assert not native.prepared_dir.exists()
    assert capsys.readouterr().err == (
        "laura_setup_failed=strict_total_deadline_unsupported_by_modal\n"
    )


def test_saved_manifest_total_limit_refuses_before_preparation_and_guard(
    tmp_path, monkeypatch, capsys
):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    manifest_before = bundle.manifest_path.read_bytes()
    source_before = bundle.config_path.read_bytes()
    prepare_calls = []
    guard_calls = []
    monkeypatch.setattr(
        laura_trial,
        "prepare_fresh_job",
        lambda *args, **kwargs: prepare_calls.append(True),
    )
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )
    assert laura_trial.main([
        *_run_args(bundle), "--total-time-limit-seconds", "20",
    ]) == 2
    assert prepare_calls == []
    assert guard_calls == []
    assert bundle.manifest_path.read_bytes() == manifest_before
    assert bundle.config_path.read_bytes() == source_before
    assert not (bundle.jobs_dir / "laura-fresh").exists()
    assert capsys.readouterr().err == (
        "laura_setup_failed=strict_total_deadline_unsupported_by_modal\n"
    )


def test_saved_result_record_uses_effective_world_horizon_values(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    compose_path = bundle.task_path / "environment" / "docker-compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    world_environment = compose["services"]["world"]["environment"]
    world_environment["WORLD_SIM_WEEKS"] = "4.5"
    world_environment["WORLD_MAX_TURNS"] = "77"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    _save(bundle)

    def guarded_run(config, thresholds, **hooks):
        hooks["before_job_run"](SimpleNamespace())
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main(_run_args(bundle)) == 0
    effective = json.loads(
        (bundle.jobs_dir / "laura-fresh" / "effective_parameters.json").read_text(encoding="utf-8")
    )
    assert effective["sim_weeks"] == 4.5
    assert effective["max_turns"] == 77
    assert effective["effective_parameters_complete"] is True
    assert effective["unavailable_fields"] == []


def test_saved_result_resolves_task_environment_instead_of_metadata(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    task_path = bundle.task_path / "task.toml"
    task_lines = task_path.read_text(encoding="utf-8").splitlines()
    task_path.write_text(
        "\n".join(
            'WORLD_SIM_WEEKS = "12.5"' if line.startswith("WORLD_SIM_WEEKS = ") else line
            for line in task_lines
        )
        + "\n",
        encoding="utf-8",
    )
    compose_path = bundle.task_path / "environment" / "docker-compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["world"]["environment"]["WORLD_SIM_WEEKS"] = "${WORLD_SIM_WEEKS:-12}"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    _save(bundle)
    monkeypatch.setenv("WORLD_SIM_WEEKS", "99")

    def guarded_run(config, thresholds, **hooks):
        hooks["before_job_run"](SimpleNamespace())
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main(_run_args(bundle)) == 0
    effective = json.loads(
        (bundle.jobs_dir / "laura-fresh" / "effective_parameters.json").read_text(encoding="utf-8")
    )
    assert effective["sim_weeks"] == 12.5


def test_saved_compose_default_does_not_assume_an_unconfigured_host_value(
    tmp_path, monkeypatch, capsys
):
    bundle = _native_setup(tmp_path, monkeypatch)
    task_path = bundle.task_path / "task.toml"
    task_lines = task_path.read_text(encoding="utf-8").splitlines()
    task_path.write_text(
        "\n".join(line for line in task_lines if not line.startswith("WORLD_SIM_WEEKS = "))
        + "\n",
        encoding="utf-8",
    )
    compose_path = bundle.task_path / "environment" / "docker-compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["world"]["environment"]["WORLD_SIM_WEEKS"] = "${WORLD_SIM_WEEKS:-12}"
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    _save(bundle)
    monkeypatch.setenv("WORLD_SIM_WEEKS", "99")
    guard_calls = []
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )

    assert laura_trial.main(_run_args(bundle)) == 2
    assert guard_calls == []
    assert capsys.readouterr().err == (
        "laura_setup_failed=saved_world_environment_unresolvable\n"
    )


@pytest.mark.parametrize(
    ("operator", "expected_code", "expected_weeks", "expected_error"),
    [(":-", 0, 12, None), ("-", 2, None, "saved_sim_weeks_invalid")],
)
def test_saved_compose_defaults_distinguish_empty_from_unset(
    tmp_path, monkeypatch, capsys, operator, expected_code, expected_weeks, expected_error
):
    bundle = _native_setup(tmp_path, monkeypatch)
    compose_path = bundle.task_path / "environment" / "docker-compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["world"]["environment"]["WORLD_SIM_WEEKS"] = (
        f"${{WORLD_SIM_WEEKS{operator}12}}"
    )
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["environment"]["env"] = {"WORLD_SIM_WEEKS": ""}
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    _save(bundle)
    guard_calls = []

    def guarded_run(config, thresholds, **hooks):
        guard_calls.append(True)
        hooks["before_job_run"](SimpleNamespace())
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main(_run_args(bundle)) == expected_code
    if expected_code == 0:
        assert guard_calls == [True]
        effective = json.loads(
            (bundle.jobs_dir / "laura-fresh" / "effective_parameters.json").read_text(
                encoding="utf-8"
            )
        )
        assert effective["sim_weeks"] == expected_weeks
        assert capsys.readouterr().err == ""
    else:
        assert guard_calls == []
        assert capsys.readouterr().err == f"laura_setup_failed={expected_error}\n"


def test_saved_result_resolves_compose_list_environment_and_job_override(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    compose_path = bundle.task_path / "environment" / "docker-compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["world"]["environment"] = [
        "WORLD_SIM_WEEKS=${WORLD_SIM_WEEKS}",
        "WORLD_SIMULATOR_MODEL=openrouter/list-simulator",
        "WORLD_GRADER_MODEL=openrouter/list-grader",
        "WORLD_COST_LIMIT=4.5",
    ]
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    overlay = tmp_path / "world-env-overlay.yaml"
    overlay.write_text(
        "services:\n  world:\n    environment:\n"
        "      - WORLD_SIM_WEEKS=${WORLD_SIM_WEEKS}\n"
        "      - WORLD_MAX_TURNS=73\n",
        encoding="utf-8",
    )
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["environment"]["env"] = {"WORLD_SIM_WEEKS": "8.25"}
    job["environment"]["extra_docker_compose"] = [str(overlay)]
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    _save(bundle)

    def guarded_run(config, thresholds, **hooks):
        hooks["before_job_run"](SimpleNamespace())
        return 0

    monkeypatch.setattr(laura_trial.guarded_harbor, "run_guarded_config", guarded_run)
    assert laura_trial.main(_run_args(bundle)) == 0
    effective = json.loads(
        (bundle.jobs_dir / "laura-fresh" / "effective_parameters.json").read_text(encoding="utf-8")
    )
    assert effective["sim_weeks"] == 8.25
    assert effective["max_turns"] == 73
    assert effective["simulator_model"] == "openrouter/list-simulator"
    assert effective["grader_model"] == "openrouter/list-grader"
    assert effective["world_cost_limit_usd"] == "4.5"


@pytest.mark.parametrize(
    "world_environment",
    [
        {"WORLD_SIMULATOR_MODEL": "${UNSUPPORTED_SIMULATOR_MODEL}"},
        {"WORLD_SIM_WEEKS": "${WORLD_SIM_WEEKS:-$HOST_SIM_WEEKS}"},
        ["WORLD_SIMULATOR_MODEL"],
    ],
)
def test_saved_run_rejects_unresolved_interpolated_environment_before_guard(
    tmp_path, monkeypatch, capsys, world_environment
):
    bundle = _native_setup(tmp_path, monkeypatch)
    compose_path = bundle.task_path / "environment" / "docker-compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    compose["services"]["world"]["environment"] = world_environment
    compose_path.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    _save(bundle)
    guard_calls = []
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )

    assert laura_trial.main(_run_args(bundle)) == 2
    assert guard_calls == []
    assert capsys.readouterr().err == (
        "laura_setup_failed=saved_world_environment_unresolvable\n"
    )


def test_run_rejects_existing_requested_job_before_guard(tmp_path, monkeypatch, capsys):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    existing = bundle.jobs_dir / "already-used"
    existing.mkdir(parents=True)
    guard_calls = []
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )

    assert laura_trial.main(_run_args(bundle, "already-used")) == 2
    assert guard_calls == []
    assert capsys.readouterr().err == "laura_setup_failed=identity_or_job_validation\n"


def test_job_config_change_invalidates_setup(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["verifier"]["kwargs"]["grading_policy"] = "changed"
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)


@pytest.mark.parametrize("mutation", ["filename", "path", "digest", "bytes"])
def test_staged_wheel_identity_changes_fail_before_guard(
    tmp_path, monkeypatch, capsys, mutation
):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    wheel = next(bundle.task_path.rglob("*.whl"))
    if mutation == "filename":
        wheel.rename(wheel.with_name("renamed-agent.whl"))
    elif mutation == "path":
        moved = bundle.task_path / "moved-wheel" / wheel.name
        moved.parent.mkdir()
        wheel.rename(moved)
    elif mutation == "digest":
        task_toml = bundle.task_path / "task.toml"
        content = task_toml.read_text(encoding="utf-8")
        staged_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        assert staged_digest in content
        task_toml.write_text(content.replace(staged_digest, "0" * 64), encoding="utf-8")
    else:
        wheel.write_bytes(wheel.read_bytes() + b"changed staged wheel bytes")

    guard_calls = []
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )
    assert laura_trial.main(_run_args(bundle)) == 2
    assert guard_calls == []
    assert capsys.readouterr().err == "laura_setup_failed=identity_or_job_validation\n"


@pytest.mark.parametrize(
    "case",
    ["missing_wheel", "invalid_zip", "missing_descriptor", "duplicate_descriptor", "private_top_level", "private_user"],
)
def test_public_validation_rejects_invalid_digest_valid_staged_wheel(
    tmp_path, monkeypatch, case
):
    setup = {}
    if case == "invalid_zip":
        with pytest.raises(ValueError, match="not a readable wheel"):
            _native_setup(tmp_path, monkeypatch, raw_wheel=b"synthetic digest-valid bytes, not a zip archive")
        return
    elif case == "missing_descriptor":
        setup["include_scenario"] = False
    elif case == "duplicate_descriptor":
        setup["duplicate_scenario"] = True
    elif case == "private_top_level":
        setup["scenario"] = {**_PUBLIC_SCENARIO, "facts": {"fixture_only": True}}
    elif case == "private_user":
        setup["scenario"] = {
            **_PUBLIC_SCENARIO,
            "user": {**_PUBLIC_SCENARIO["user"], "home_town": "Synthetic private field"},
        }
    bundle = _native_setup(tmp_path, monkeypatch, **setup)
    if case == "missing_wheel":
        wheel_manifest = json.loads(
            (bundle.task_path / "environment" / "agent" / "agent-wheel.json").read_text()
        )
        (bundle.task_path / "environment" / "agent" / "wheels" / wheel_manifest["filename"]).unlink()
    else:
        wheel_path = next((bundle.task_path / "environment" / "agent" / "wheels").glob("*.whl"))
        wheel_sha256 = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        wheel_manifest = json.loads(
            (bundle.task_path / "environment" / "agent" / "agent-wheel.json").read_text()
        )
        metadata = tomllib.loads((bundle.task_path / "task.toml").read_text())["metadata"]
        assert wheel_manifest["sha256"] == metadata["agent_wheel_sha256"] == wheel_sha256

    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(bundle.config_path, bundle.manifest_path)
    assert not bundle.manifest_path.exists()


def test_public_validation_rejects_unprojected_inherited_wheel(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    source_scenario_dir = Path(__file__).resolve().parents[1] / "refund_eval" / "scenario"
    inherited_files = {
        "archive/train_refund": (source_scenario_dir / "archive" / "train_refund.yaml").read_bytes(),
        "train_refund_late": (source_scenario_dir / "train_refund_late.yaml").read_bytes(),
    }
    _install_staged_scenario_wheel(bundle, inherited_files, tmp_path / "unprojected-wheel")

    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(bundle.config_path, bundle.manifest_path)
    assert not bundle.manifest_path.exists()


def test_public_validation_rejects_mismatched_public_scenario_id(tmp_path, monkeypatch):
    bundle = _native_setup(
        tmp_path, monkeypatch, scenario={**_PUBLIC_SCENARIO, "id": "different_scenario"},
    )
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(bundle.config_path, bundle.manifest_path)
    assert not bundle.manifest_path.exists()


def test_daycare_public_scenario_identity_roundtrips_through_manifest(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch, scenario_id="daycare_reimbursement", sim_weeks=10)
    _save(bundle)

    public_validation = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))["public_validation"]
    assert public_validation["scenario"] == "daycare_reimbursement"
    assert public_validation["scenario_member"] == "refund_eval/scenario/daycare_reimbursement.yaml"
    task_env = tomllib.loads((bundle.task_path / "task.toml").read_text(encoding="utf-8"))["environment"]["env"]
    assert float(task_env["WORLD_SIM_WEEKS"]) == 10
    raw_config = laura_trial.guarded_harbor.load_job_config(bundle.config_path)
    with pytest.raises(
        laura_trial.guarded_harbor.LauncherFailure,
        match="daycare_reimbursement_requires_saved_identity",
    ):
        laura_trial.guarded_harbor.validate_job_config(raw_config)
    laura_trial.verify_setup(bundle.manifest_path)


@pytest.mark.parametrize(
    ("scenario", "scenario_member"),
    [
        ("unapproved_scenario", "refund_eval/scenario/unapproved_scenario.yaml"),
        ("train_refund_late", _PUBLIC_SCENARIO_MEMBER),
    ],
)
def test_manifest_rejects_tampered_daycare_scenario_identity(
    tmp_path, monkeypatch, scenario, scenario_member
):
    bundle = _native_setup(tmp_path, monkeypatch, scenario_id="daycare_reimbursement", sim_weeks=10)
    _save(bundle)
    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    manifest["public_validation"]["scenario"] = scenario
    manifest["public_validation"]["scenario_member"] = scenario_member
    bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)


def test_public_validation_rejects_wheel_purelib_scenario_alias(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch, alias_scenario=True)
    wheel_path = next((bundle.task_path / "environment" / "agent" / "wheels").glob("*.whl"))
    wheel_sha256 = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    wheel_manifest = json.loads(
        (bundle.task_path / "environment" / "agent" / "agent-wheel.json").read_text()
    )
    metadata = tomllib.loads((bundle.task_path / "task.toml").read_text())["metadata"]
    assert wheel_manifest["sha256"] == metadata["agent_wheel_sha256"] == wheel_sha256

    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(bundle.config_path, bundle.manifest_path)
    assert not bundle.manifest_path.exists()


def test_verify_reruns_public_scenario_schema_check(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    validation_calls = []
    validate = laura_trial._public_validation_result

    def record_validation(task_path):
        validation_calls.append(task_path)
        return validate(task_path)

    monkeypatch.setattr(laura_trial, "_public_validation_result", record_validation)
    laura_trial.verify_setup(bundle.manifest_path)
    assert validation_calls == [bundle.task_path]


def test_missing_public_validation_record_fails_before_guard(tmp_path, monkeypatch, capsys):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    manifest = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))
    del manifest["public_validation"]
    bundle.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    guard_calls = []
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )

    assert laura_trial.main(_run_args(bundle)) == 2
    assert guard_calls == []
    assert capsys.readouterr().err == "laura_setup_failed=identity_or_job_validation\n"


@pytest.mark.parametrize("relative", ["refund_eval/current.py", "scout/run.py", "uv.lock"])
def test_changed_current_source_or_lock_invalidates_setup(tmp_path, monkeypatch, relative):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    path = bundle.source_root / relative
    path.write_text(path.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)


def test_runtime_package_identity_change_invalidates_setup(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    monkeypatch.setattr(
        laura_trial,
        "_runtime_identity",
        lambda: {"python": "changed", "packages": {}},
    )
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)


def test_source_inventory_ignores_gitignored_private_files_but_covers_scout(
    tmp_path, monkeypatch
):
    root = _source_repo(tmp_path / "source-root", monkeypatch)
    initial = laura_trial._source_identity()
    private = root / "refund_eval/private.json"
    private.write_text("changed private fixture data\n", encoding="utf-8")
    assert laura_trial._source_identity() == initial
    scout = root / "scout/run.py"
    scout.write_text(scout.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")
    assert laura_trial._source_identity() != initial


def test_external_instruction_and_skill_inputs_are_fingerprinted(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    bundle.instruction.write_text("Changed instruction.\n", encoding="utf-8")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)
    bundle.instruction.write_text("Keep this instruction unchanged.\n", encoding="utf-8")
    (bundle.skill / "SKILL.md").write_text("Changed skill.\n", encoding="utf-8")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)


def test_compose_overlay_change_fails_before_guard(tmp_path, monkeypatch, capsys):
    bundle = _native_setup(tmp_path, monkeypatch)
    overlay = tmp_path / "compose-overlay.yaml"
    overlay.write_text(
        "services:\n  agent:\n    labels:\n      build: '123'\n"
        "    environment:\n      env_file: text\n      FIXTURE_OVERLAY: before\n",
        encoding="utf-8",
    )
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["environment"]["extra_docker_compose"] = [str(overlay)]
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    _save(bundle)
    laura_trial.verify_setup(bundle.manifest_path)

    inputs = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))["config_inputs"]
    [entry] = [item for item in inputs if item["kind"] == "extra_docker_compose"]
    assert entry["path"] == str(overlay.resolve())

    overlay.write_text(
        "services:\n  agent:\n    labels:\n      build: '123'\n"
        "    environment:\n      env_file: text\n      FIXTURE_OVERLAY: after\n",
        encoding="utf-8",
    )
    guard_calls = []
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )

    assert laura_trial.main(_run_args(bundle)) == 2
    assert guard_calls == []
    assert capsys.readouterr().err == "laura_setup_failed=identity_or_job_validation\n"


def test_compose_overlay_local_file_references_fail_closed(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    overlay = tmp_path / "compose-overlay.yaml"
    overlay.write_text(
        "services:\n  agent:\n    env_file: ./runtime.env\n",
        encoding="utf-8",
    )
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["environment"]["extra_docker_compose"] = [str(overlay)]
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")

    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(bundle.config_path, bundle.manifest_path)
    assert not bundle.manifest_path.exists()


@pytest.mark.parametrize("input_tree", ["task", "skill"])
def test_manifest_inside_fingerprinted_tree_is_rejected_before_write(
    tmp_path, monkeypatch, input_tree
):
    bundle = _native_setup(tmp_path, monkeypatch)
    root = bundle.task_path if input_tree == "task" else bundle.skill
    manifest = root / "laura-setup.json"
    overlap_checks = []
    reject_overlap = laura_trial._reject_manifest_overlap

    def record_overlap_check(*args):
        overlap_checks.append(True)
        return reject_overlap(*args)

    monkeypatch.setattr(laura_trial, "_reject_manifest_overlap", record_overlap_check)
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(bundle.config_path, manifest)

    assert overlap_checks == [True]
    assert not manifest.exists()


def test_empty_optional_external_input_lists_can_be_snapshotted(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["extra_instruction_paths"] = []
    job["agents"][0]["skills"] = []
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")

    _save(bundle)
    loaded = laura_trial.verify_setup(bundle.manifest_path)

    assert loaded.extra_instruction_paths == []
    assert loaded.agents[0].skills == []
    assert json.loads(bundle.manifest_path.read_text(encoding="utf-8"))["config_inputs"] == []


def test_missing_task_config_or_external_input_fails_closed(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    (bundle.task_path / "instruction.md").unlink()
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)

    other = _native_setup(tmp_path / "other", monkeypatch)
    _save(other)
    other.instruction.unlink()
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(other.manifest_path)

    third = _native_setup(tmp_path / "third", monkeypatch)
    _save(third)
    third.config_path.unlink()
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(third.manifest_path)


def test_corrupt_manifest_and_nonregular_or_symlink_task_inputs_fail_closed(
    tmp_path, monkeypatch
):
    bundle = _native_setup(tmp_path, monkeypatch)
    _save(bundle)
    valid_manifest = bundle.manifest_path.read_bytes()
    bundle.manifest_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)
    corrupt_schema = json.loads(valid_manifest)
    corrupt_schema["schema_version"] = True
    bundle.manifest_path.write_text(json.dumps(corrupt_schema), encoding="utf-8")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.verify_setup(bundle.manifest_path)

    symlink_bundle = _native_setup(tmp_path / "symlink", monkeypatch)
    target = symlink_bundle.task_path / "instruction.md"
    link = symlink_bundle.task_path / "linked-input"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(symlink_bundle.config_path, symlink_bundle.manifest_path)

    fifo_bundle = _native_setup(tmp_path / "fifo", monkeypatch)
    os.mkfifo(fifo_bundle.task_path / "nonregular-input")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(fifo_bundle.config_path, fifo_bundle.manifest_path)


def test_python_caches_are_ignored_and_manifest_paths_follow_saved_harbor_cwd(
    tmp_path, monkeypatch
):
    bundle = _native_setup(tmp_path, monkeypatch)
    launch_root = tmp_path / "launch-root"
    launch_root.mkdir()
    instructions = launch_root / "instruction.md"
    instructions.write_text("Relative instruction.\n", encoding="utf-8")
    local_skill = launch_root / "skills" / "custom"
    local_skill.mkdir(parents=True)
    (local_skill / "SKILL.md").write_text("Relative skill.\n", encoding="utf-8")

    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["jobs_dir"] = "jobs"
    job["tasks"][0]["path"] = os.path.relpath(bundle.task_path, launch_root)
    job["extra_instruction_paths"] = ["instruction.md"]
    job["agents"][0]["skills"] = ["skills/custom"]
    config_path = tmp_path / "different-config-directory" / "job.yaml"
    config_path.parent.mkdir()
    config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    manifest = tmp_path / "relative-paths" / "manifest.json"
    cache = bundle.task_path / "__pycache__"
    cache.mkdir()
    cached = cache / "fixture.pyc"
    cached.write_bytes(b"cache-one")

    monkeypatch.chdir(launch_root)
    laura_trial.save_setup(config_path, manifest)
    cached.write_bytes(b"cache-two")
    monkeypatch.chdir(tmp_path)
    loaded = laura_trial.verify_setup(manifest)

    assert loaded.jobs_dir == (launch_root / "jobs").resolve()
    assert loaded.tasks[0].path == bundle.task_path.resolve()
    assert loaded.extra_instruction_paths == [instructions.resolve()]
    assert loaded.agents[0].skills == [str(local_skill.resolve())]
    assert json.loads(manifest.read_text(encoding="utf-8"))["path_base"] == str(launch_root.resolve())


def test_jobs_directory_must_pass_private_output_policy_before_guard(
    tmp_path, monkeypatch, capsys
):
    bundle = _native_setup(tmp_path, monkeypatch)
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["jobs_dir"] = str(laura_trial.REPO_ROOT / "scripts" / "laura-unignored-test-output")
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    _save(bundle)
    guard_calls = []
    monkeypatch.setattr(
        laura_trial.guarded_harbor,
        "run_guarded_config",
        lambda *args, **kwargs: guard_calls.append(True) or 0,
    )

    assert laura_trial.main(_run_args(bundle)) == 2
    assert guard_calls == []
    assert capsys.readouterr().err == "laura_setup_failed=identity_or_job_validation\n"


def test_remote_skills_are_rejected_as_unverifiable_inputs(tmp_path, monkeypatch):
    bundle = _native_setup(tmp_path, monkeypatch)
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["agents"][0]["skills"] = ["https://example.invalid/remote-skill.git"]
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    with pytest.raises(laura_trial.SetupFailure):
        laura_trial.save_setup(bundle.config_path, bundle.manifest_path)

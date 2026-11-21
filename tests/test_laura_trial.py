from __future__ import annotations

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

from refund_eval.harbor import batch
from refund_eval.harbor.batch import NativeBatchConfig
from scripts import laura_trial

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
    alias_scenario=False, raw_wheel=None
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
    if include_scenario:
        public_scenario = _PUBLIC_SCENARIO if scenario is None else scenario
        files[_PUBLIC_SCENARIO_MEMBER] = yaml.safe_dump(public_scenario, sort_keys=False).encode()
        if alias_scenario:
            files[f"synthetic_agent-0.0.0.data/purelib/{_PUBLIC_SCENARIO_MEMBER}"] = files[
                _PUBLIC_SCENARIO_MEMBER
            ]
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
            if duplicate_scenario and name == _PUBLIC_SCENARIO_MEMBER:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    archive.writestr(name, content)
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
    duplicate_scenario=False, alias_scenario=False, raw_wheel=None,
) -> SimpleNamespace:
    source_root = _source_repo(tmp_path / "source-root", monkeypatch)
    wheel, wheel_sha256 = _write_test_only_wheel(
        tmp_path / "wheel", scenario=scenario, include_scenario=include_scenario,
        duplicate_scenario=duplicate_scenario, alias_scenario=alias_scenario, raw_wheel=raw_wheel,
    )
    config = NativeBatchConfig.model_validate(
        {
            "execution": "modal",
            "scenario": "train_refund_late",
            "data_mode": "fixture",
            "research_mode": "saved",
            "model": "openrouter/test-model",
            "models": ["openrouter/test-model"],
            "cost_limit": {"openrouter/test-model": 10.0},
            "world_cost_limit": 10.0,
            "max_turns": 50,
            "sim_weeks": 6,
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


def _save(bundle: SimpleNamespace) -> None:
    laura_trial.save_setup(bundle.config_path, bundle.manifest_path)


def _run_args(bundle: SimpleNamespace, job_name: str = "laura-fresh") -> list[str]:
    return [
        "run",
        "--manifest", str(bundle.manifest_path),
        "--job-name", job_name,
        "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "5",
    ]


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

    def guarded_run(config, thresholds, *, on_job_created, on_sandbox_created):
        events.append("guarded_runner")
        captured["config"] = config
        log_path = bundle.jobs_dir / "laura-fresh" / "job.log"
        on_job_created(SimpleNamespace(_log_output_path=log_path))
        on_sandbox_created(SimpleNamespace(
            _app=SimpleNamespace(get_dashboard_url=lambda: "https://modal.com/apps/ap-offline-test")
        ))
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
        setup["raw_wheel"] = b"synthetic digest-valid bytes, not a zip archive"
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
        "services:\n  agent:\n    environment:\n      FIXTURE_OVERLAY: before\n",
        encoding="utf-8",
    )
    job = yaml.safe_load(bundle.config_path.read_bytes())
    job["environment"]["extra_docker_compose"] = [str(overlay)]
    bundle.config_path.write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    _save(bundle)

    inputs = json.loads(bundle.manifest_path.read_text(encoding="utf-8"))["config_inputs"]
    [entry] = [item for item in inputs if item["kind"] == "extra_docker_compose"]
    assert entry["path"] == str(overlay.resolve())

    overlay.write_text(
        "services:\n  agent:\n    environment:\n      FIXTURE_OVERLAY: after\n",
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

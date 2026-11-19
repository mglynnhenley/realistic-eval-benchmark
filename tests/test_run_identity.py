import json

import pytest

from refund_eval import run_identity
from refund_eval.published import browser_runtimes
from refund_eval.published.browser_runtimes import BrowserBoundaryError
from refund_eval.run_identity import IdentityLockError, validate_identity_lock

IMAGE_ID = "sha256:" + "a" * 64


def lock():
    return {
        "schema_version": 1,
        "environment_version": run_identity.ENVIRONMENT_VERSION,
        "environment_fingerprint": "environment",
        "scenario": "train_refund_late",
        "scenario_fingerprint": "scenario",
        "browser_runtime": "docker",
        "runtime_identity_kind": "docker_image_id",
        "runtime_identity": IMAGE_ID,
    }


def setup_identity(monkeypatch):
    monkeypatch.setattr(run_identity, "environment_fingerprint", lambda: "environment")
    monkeypatch.setattr(run_identity, "scenario_fingerprint", lambda _: "scenario")
    monkeypatch.setattr(browser_runtimes, "resolve_local_image_id", lambda image: IMAGE_ID)


def test_empty_identity_lock_is_rejected():
    with pytest.raises(IdentityLockError, match="empty"):
        run_identity.load_identity_lock("")


def test_relative_identity_lock_path_falls_back_to_repo_root(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    lock_path = repository / ".context" / "run-lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps(lock()))
    task_cwd = repository / "task"
    task_cwd.mkdir()
    monkeypatch.setattr(run_identity, "ROOT", repository)
    monkeypatch.chdir(task_cwd)

    assert run_identity.load_identity_lock(".context/run-lock.json") == lock()


def test_absolute_and_inline_json_identity_locks_are_preserved(tmp_path, monkeypatch):
    repository = tmp_path / "repo"
    repository.mkdir()
    absolute_path = tmp_path / "run-lock.json"
    expected = lock()
    absolute_path.write_text(json.dumps(expected))
    monkeypatch.setattr(run_identity, "ROOT", repository)

    assert run_identity.load_identity_lock(str(absolute_path)) == expected
    assert run_identity.load_identity_lock(json.dumps(expected)) == expected


def test_unknown_user_identity_lock_path_is_reported_as_lock_error():
    with pytest.raises(IdentityLockError, match="Cannot resolve identity lock path"):
        run_identity.load_identity_lock("~definitely-no-such-local-user/lock.json")


def test_unlocked_docker_run_records_reference_without_resolving_it(monkeypatch):
    monkeypatch.setattr(run_identity, "environment_fingerprint", lambda: "environment")
    monkeypatch.setattr(run_identity, "scenario_fingerprint", lambda _: "scenario")
    monkeypatch.setattr(
        browser_runtimes, "resolve_local_image_id",
        lambda _: pytest.fail("an unlocked task resolves the image when its runtime starts"),
    )

    actual = run_identity.current_run_identity("train_refund_late", browser_runtime="docker")

    assert actual["runtime_identity_kind"] == "docker_image_ref"
    assert actual["runtime_identity"] == "refund-eval-playwright:0.0.83"


def test_unlocked_subprocess_is_not_reported_as_an_exact_artifact(monkeypatch):
    monkeypatch.setattr(run_identity, "environment_fingerprint", lambda: "environment")
    monkeypatch.setattr(run_identity, "scenario_fingerprint", lambda _: "scenario")

    actual = run_identity.current_run_identity("train_refund_late", browser_runtime="subprocess")

    assert actual["runtime_identity_kind"] == "artifact_unlocked"
    assert actual["runtime_identity"] is None


def test_subprocess_artifact_lock_creation_is_rejected():
    with pytest.raises(IdentityLockError, match="Exact browser artifact locks"):
        run_identity.create_identity_lock("train_refund_late", browser_runtime="subprocess")


def test_created_lock_records_the_resolved_docker_artifact(monkeypatch):
    monkeypatch.setattr(run_identity, "_default_image", lambda: "browser:current")
    monkeypatch.setattr(run_identity, "environment_fingerprint", lambda: "environment")
    monkeypatch.setattr(run_identity, "scenario_fingerprint", lambda _: "scenario")
    monkeypatch.setattr(browser_runtimes, "resolve_local_image_id", lambda image: IMAGE_ID)

    actual = run_identity.create_identity_lock("train_refund_late")

    assert actual["runtime_identity_kind"] == "docker_image_id"
    assert actual["runtime_identity"] == IMAGE_ID
    assert actual["browser_image_ref"] == "browser:current"


def test_matching_lock_resolves_and_returns_immutable_local_image(monkeypatch):
    setup_identity(monkeypatch)

    actual, image_id = validate_identity_lock(lock(), "train_refund_late", browser_runtime="docker")

    assert actual["runtime_identity"] == IMAGE_ID
    assert image_id == IMAGE_ID


@pytest.mark.parametrize("field,value", [
    ("environment_fingerprint", "changed"),
    ("scenario", "other"),
    ("scenario_fingerprint", "changed"),
    ("browser_runtime", "subprocess"),
])
def test_lock_mismatch_fails_before_image_lookup(monkeypatch, field, value):
    setup_identity(monkeypatch)
    monkeypatch.setattr(browser_runtimes, "resolve_local_image_id",
                        lambda _: pytest.fail("mismatch must be rejected before image lookup"))
    expected = lock()
    expected[field] = value

    with pytest.raises(IdentityLockError, match="Identity lock mismatch"):
        validate_identity_lock(expected, "train_refund_late", browser_runtime="docker")


def test_locked_image_unavailable_fails_without_fallback(monkeypatch):
    setup_identity(monkeypatch)
    monkeypatch.setattr(
        browser_runtimes, "resolve_local_image_id",
        lambda _: (_ for _ in ()).throw(BrowserBoundaryError("missing")),
    )

    with pytest.raises(IdentityLockError, match="unavailable locally"):
        validate_identity_lock(lock(), "train_refund_late", browser_runtime="docker")


def test_local_docker_runtime_launches_by_image_id(tmp_path):
    from refund_eval.published.browser_runtimes import docker_command

    command = docker_command(tmp_path, "trial", image=IMAGE_ID)

    assert command[-1] == IMAGE_ID
    assert "--pull=never" in command


def test_local_image_lookup_inspects_without_pulling(monkeypatch):
    from types import SimpleNamespace

    from refund_eval.published import browser_runtimes

    calls = []
    monkeypatch.setattr(browser_runtimes.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        browser_runtimes.subprocess, "run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(
            stdout=IMAGE_ID, returncode=0,
        ),
    )

    assert browser_runtimes.resolve_local_image_id("refund-eval-playwright:0.0.83") == IMAGE_ID
    assert calls == [[
        "/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}",
        "refund-eval-playwright:0.0.83",
    ]]


def test_missing_docker_image_is_not_pulled(monkeypatch):
    from types import SimpleNamespace

    from refund_eval.published import browser_runtimes

    calls = []
    monkeypatch.setattr(browser_runtimes.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        browser_runtimes.subprocess, "run",
        lambda command, **kwargs: calls.append(command) or SimpleNamespace(stdout="", returncode=1),
    )

    with pytest.raises(BrowserBoundaryError, match="unavailable locally"):
        browser_runtimes.resolve_local_image_id("refund-eval-playwright:0.0.83")
    assert not any("pull" in command for command in calls)


def test_source_mismatch_rejects_task_before_cost_setup(monkeypatch):
    from refund_eval import task

    monkeypatch.setattr(run_identity, "environment_fingerprint", lambda: "current")
    monkeypatch.setattr(run_identity, "scenario_fingerprint", lambda _: "scenario")
    monkeypatch.setattr(browser_runtimes, "resolve_local_image_id",
                        lambda _: pytest.fail("source mismatch must be rejected before image lookup"))
    monkeypatch.setattr(task, "register_default_costs",
                        lambda *args, **kwargs: pytest.fail("source mismatch must precede cost setup"))
    expected = lock()
    expected["environment_fingerprint"] = "old"

    with pytest.raises(IdentityLockError, match="environment_fingerprint"):
        task.impossible_refund(identity_lock=expected, scenario="train_refund_late", cost_limit=150)

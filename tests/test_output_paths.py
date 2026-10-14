"""Private output guards use actual Git ignore and index semantics."""

import subprocess
from pathlib import Path

import pytest

from refund_eval.output_paths import validate_private_output


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / ".gitignore").write_text(".context/\nprivate/\n")
    return root


def test_external_storage_needs_no_git(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    destination = tmp_path / "external" / "runs"
    assert validate_private_output(destination) == destination.resolve()
    assert not destination.exists()


def test_ignored_new_directory_is_accepted_without_creating_it(checkout):
    destination = checkout / ".context" / "runs"
    assert validate_private_output(str(destination)) == destination
    assert not destination.exists()


def test_unignored_output_is_rejected(checkout):
    with pytest.raises(ValueError, match="must be Git-ignored"):
        validate_private_output(checkout / "outputs" / "runs")
    assert not (checkout / "outputs").exists()


@pytest.mark.parametrize("nested", [False, True])
def test_ignored_paths_cannot_contain_tracked_files(checkout, nested):
    private = checkout / "private"
    private.mkdir()
    file = private / "snapshot.json"
    file.write_text("{}")
    git(checkout, "add", "--force", "private/snapshot.json")
    with pytest.raises(ValueError, match="contains tracked files"):
        validate_private_output(private if nested else file)


def test_deleted_but_tracked_output_is_still_rejected(checkout):
    private = checkout / "private"
    private.mkdir()
    file = private / "snapshot.json"
    file.write_text("{}")
    git(checkout, "add", "--force", "private/snapshot.json")
    file.unlink()
    with pytest.raises(ValueError, match="contains tracked files"):
        validate_private_output(private)


def test_symlink_cannot_hide_unignored_destination(checkout):
    private = checkout / "private"
    private.mkdir()
    (private / "alias").symlink_to(checkout / "public", target_is_directory=True)
    with pytest.raises(ValueError, match="must be Git-ignored"):
        validate_private_output(private / "alias" / "runs")


def test_alias_to_private_directory_returns_resolved_destination(checkout):
    (checkout / "alias").symlink_to(checkout / "private", target_is_directory=True)
    assert validate_private_output(checkout / "alias" / "runs") == checkout / "private" / "runs"


def test_checkout_root_is_rejected_even_inside_ignored_parent(checkout):
    nested = checkout / "private" / "source"
    nested.mkdir(parents=True)
    git(nested, "init", "--quiet")
    with pytest.raises(ValueError, match="checkout root"):
        validate_private_output(nested)


def test_nested_checkout_must_ignore_its_own_outputs(checkout):
    nested = checkout / "private" / "source"
    nested.mkdir(parents=True)
    git(nested, "init", "--quiet")
    with pytest.raises(ValueError, match="must be Git-ignored"):
        validate_private_output(nested / "runs")
    (nested / ".gitignore").write_text("runs/\n")
    assert validate_private_output(nested / "runs") == nested / "runs"


def test_checkout_with_git_file_is_protected(tmp_path):
    root = tmp_path / "worktree"
    subprocess.run(
        ["git", "init", "--quiet", "--separate-git-dir", str(tmp_path / "metadata"), str(root)],
        check=True, capture_output=True,
    )
    assert (root / ".git").is_file()
    with pytest.raises(ValueError, match="must be Git-ignored"):
        validate_private_output(root / "runs")


def test_git_environment_cannot_redirect_the_index_check(checkout, tmp_path, monkeypatch):
    file = checkout / "private" / "snapshot.json"
    file.parent.mkdir()
    file.write_text("{}")
    git(checkout, "add", "--force", "private/snapshot.json")
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "empty-index"))
    with pytest.raises(ValueError, match="contains tracked files"):
        validate_private_output(file.parent)


def test_git_missing_inside_checkout_fails_closed(checkout, monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(ValueError, match="Cannot verify private output"):
        validate_private_output(checkout / "private")


def test_dashboard_rejects_before_creating_storage(checkout):
    from refund_eval.dashboard.server import RunManager

    destination = checkout / "runs"
    with pytest.raises(ValueError, match="must be Git-ignored"):
        RunManager(destination)
    assert not destination.exists()


async def test_browser_rejects_before_readiness_or_provider_setup(checkout, monkeypatch):
    from refund_eval.published import profile
    from refund_eval.scenario.schema import load_scenario

    def should_not_run(**kwargs):
        pytest.fail("Readiness ran before the output directory was validated")

    monkeypatch.setattr(profile, "hybrid_readiness", should_not_run)
    with pytest.raises(ValueError, match="must be Git-ignored"):
        async with profile.published_tools(
            load_scenario("train_refund_late"), artifact_dir=str(checkout / "artifacts"),
        ):
            pytest.fail("The unsafe browser profile was entered")
    assert not (checkout / "artifacts").exists()

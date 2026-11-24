"""Keep environment changes tied to an explicit version and changelog entry."""

import hashlib
import json
import re
import subprocess
from itertools import pairwise

import pytest

from refund_eval import environment_version as environment


def test_environment_changelog():
    changelog = (environment.ROOT / "docs/environment-changelog.md").read_text()
    entries = re.findall(r"^## (\d+\.\d+) — [^\n]+\n(.*?)(?=^## |\Z)", changelog, re.MULTILINE | re.DOTALL)
    message = "Bump ENVIRONMENT_VERSION and add an entry to docs/environment-changelog.md."
    assert entries, message
    assert entries[0][0] == environment.ENVIRONMENT_VERSION, message
    fingerprint = re.search(r"^- Fingerprint: ([0-9a-f]{64})$", entries[0][1], re.MULTILINE)
    assert fingerprint is not None, message
    assert fingerprint.group(1) == environment.environment_fingerprint(), message
    versions = [tuple(map(int, version.split("."))) for version, _ in entries]
    assert all(new > old for new, old in pairwise(versions)), message
    for index, (_, body) in enumerate(entries):
        kind = re.search(r"^- Kind: (.+)$", body, re.MULTILINE).group(1)
        if kind == "baseline":
            assert index == len(entries) - 1
            continue
        assert index + 1 < len(entries), message
        major, minor = versions[index + 1]
        assert kind in {"trivial batch", "behaviour round"}, message
        expected = (major, minor + 1) if kind == "trivial batch" else (major + 1, 0)
        assert versions[index] == expected, message


def test_fingerprint_inventory(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.setattr(environment, "ROOT", tmp_path)
    monkeypatch.setattr(environment, "ENVIRONMENT_PATHS", ("scenario",))
    directory = tmp_path / "scenario"
    directory.mkdir()
    assert environment.environment_fingerprint() == hashlib.sha256().hexdigest()
    (tmp_path / ".gitignore").write_text("private/\n")
    (directory / "private").mkdir()
    (directory / "private/secret").write_text("ignored")
    (directory / "__pycache__").mkdir()
    (directory / "__pycache__/cache").write_text("cache")
    (directory / "cache.pyc").write_bytes(b"cache")
    assert environment.environment_fingerprint() == hashlib.sha256().hexdigest()
    source = directory / "rules.txt"
    source.write_text("rules")
    digest = hashlib.sha256()
    for value in (b"scenario/rules.txt", b"rules"):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    assert environment.environment_fingerprint() == digest.hexdigest()
    subprocess.run(["git", "add", "scenario/rules.txt"], cwd=tmp_path, check=True)
    assert environment.environment_fingerprint() == digest.hexdigest()
    source.write_text("changed")
    assert environment.environment_fingerprint() != digest.hexdigest()


def test_missing_environment_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError, match="Missing environment path"):
        environment.environment_fingerprint()


def test_fingerprint_includes_individual_files(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "config.py"
    source.write_text("DEFAULT = 1\n")
    monkeypatch.setattr(environment, "ROOT", tmp_path)
    monkeypatch.setattr(environment, "ENVIRONMENT_PATHS", ("config.py",))
    before = environment.environment_fingerprint()
    source.write_text("DEFAULT = 2\n")
    assert environment.environment_fingerprint() != before


def test_frozen_source_fingerprint_uses_only_manifest_backed_files(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "ROOT", tmp_path)
    monkeypatch.setattr(environment, "ENVIRONMENT_PATHS", ("scenario",))
    directory = tmp_path / "scenario"
    directory.mkdir()
    (directory / "rules.txt").write_text("rules")
    (directory / "__pycache__").mkdir()
    (directory / "__pycache__/rules.pyc").write_bytes(b"cache")
    (directory / "rules.pyc").write_bytes(b"cache")
    (directory / "private.txt").write_text("not in the frozen source inventory")
    manifest = tmp_path / "refund_eval/_source_manifest.json"
    manifest.parent.mkdir()
    source_digest = hashlib.sha256()
    for value in (b"scenario/rules.txt", b"rules"):
        source_digest.update(len(value).to_bytes(8, "big"))
        source_digest.update(value)
    manifest.write_text(json.dumps({
        "files": ["scenario/rules.txt"], "source_sha256": source_digest.hexdigest(),
    }))
    monkeypatch.setattr(
        environment.subprocess, "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(environment.subprocess.CalledProcessError(128, "git")),
    )

    digest = hashlib.sha256()
    for value in (b"scenario/rules.txt", b"rules"):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)
    assert environment.environment_fingerprint() == digest.hexdigest()


def test_legacy_frozen_fingerprint_does_not_require_host_only_guard(tmp_path, monkeypatch):
    assert "scripts/offline_spend_guard.py" not in environment.ENVIRONMENT_PATHS
    monkeypatch.setattr(environment, "ROOT", tmp_path)
    monkeypatch.setattr(environment, "ENVIRONMENT_PATHS", ("scenario",))
    (tmp_path / "scenario").mkdir()
    source = tmp_path / "scenario/rules.txt"
    source.write_text("rules")
    manifest = tmp_path / "refund_eval/_source_manifest.json"
    manifest.parent.mkdir()
    file_name, contents = b"scenario/rules.txt", b"rules"
    source_digest = hashlib.sha256()
    for value in (file_name, contents):
        source_digest.update(len(value).to_bytes(8, "big"))
        source_digest.update(value)
    manifest.write_text(json.dumps({
        "files": [file_name.decode()], "source_sha256": source_digest.hexdigest(),
    }))
    monkeypatch.setattr(
        environment.subprocess, "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(environment.subprocess.CalledProcessError(128, "git")),
    )
    expected = hashlib.sha256()
    for value in (file_name, contents):
        expected.update(len(value).to_bytes(8, "big"))
        expected.update(value)
    assert environment.environment_fingerprint() == expected.hexdigest()
    assert not (tmp_path / "scripts/offline_spend_guard.py").exists()


def test_frozen_source_manifest_rejects_unsafe_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "ROOT", tmp_path)
    monkeypatch.setattr(environment, "ENVIRONMENT_PATHS", ("scenario",))
    (tmp_path / "scenario").mkdir()
    manifest = tmp_path / "refund_eval/_source_manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(json.dumps({"files": ["../private"], "source_sha256": "0" * 64}))
    monkeypatch.setattr(
        environment.subprocess, "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(environment.subprocess.CalledProcessError(128, "git")),
    )

    with pytest.raises(RuntimeError, match="invalid path"):
        environment.environment_fingerprint()


def test_frozen_source_fingerprint_requires_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "ROOT", tmp_path)
    monkeypatch.setattr(environment, "ENVIRONMENT_PATHS", ("scenario",))
    (tmp_path / "scenario").mkdir()
    monkeypatch.setattr(
        environment.subprocess, "check_output",
        lambda *args, **kwargs: (_ for _ in ()).throw(environment.subprocess.CalledProcessError(128, "git")),
    )

    with pytest.raises(RuntimeError, match="Git inventory unavailable"):
        environment.environment_fingerprint()

"""Keep environment changes tied to an explicit version and changelog entry."""

import hashlib
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

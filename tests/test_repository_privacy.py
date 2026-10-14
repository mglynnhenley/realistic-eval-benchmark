"""Private outputs stay ignored without hiding authored benchmark inputs."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", [
    ".env", ".env.local", ".secrets/inboxes/private.json",
    "credentials-personal.json", "token-personal.json", "private.inbox.json",
    "custom/example.eval", "custom/browser.network.jsonl", "custom/snapshot.json",
    "custom/transcript.json", "custom/resume.snapshot.json", "custom/inbox_snapshot.json",
    "logs/example.json", "reports/example.csv", "scans-judge/results.json",
    "transcripts/example.txt", "runs/example.json", "jobs/task/source/inbox.json",
    "artifacts/browser.png", "screenshots/browser.png", "exports/messages.csv",
    ".context/private/notes.md", "scout/validation.csv",
    ".claude/skills/local/SKILL.md", "skills-lock.json",
])
def test_private_outputs_are_ignored(path):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", path], cwd=ROOT,
        capture_output=True, check=False,
    )
    assert result.returncode == 0, f"Private output is not ignored: {path}"


@pytest.mark.parametrize("path", [
    ".env.example", "README.md", "LICENSE", "pyproject.toml",
    "refund_eval/scenario/inbox.json", "refund_eval/scenario/train_refund_late.yaml",
    "refund_eval/scenario/dossiers/trainline.json", "scout/rubrics/deception.md",
    "scout/validation.example.csv", "tests/fixtures/harness_scenario.yaml",
])
def test_authored_inputs_remain_trackable(path):
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "--", path], cwd=ROOT,
        capture_output=True, check=False,
    )
    assert result.returncode == 1, f"Authored source is ignored or Git failed: {path}"

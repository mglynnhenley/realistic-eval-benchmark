"""Scan planning reads its result file, not mixed uv setup and command logs."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.realism import autonomous


def test_trusted_scan_plan_ignores_first_run_uv_setup_output(monkeypatch, tmp_path):
    context = SimpleNamespace(directory=tmp_path, measurement_worktree=tmp_path / "runner")
    plan = {"required_usd": .8, "per_transcript": []}

    def command(context, argv, **kwargs):
        assert kwargs["cwd"] == context.measurement_worktree
        assert json.loads(argv[-2]) == ["trial.eval"]
        Path(argv[-1]).write_text(json.dumps(plan))
        return 'Using CPython 3.12.13\nCreating virtual environment at: .venv\nInstalled packages\n'

    monkeypatch.setattr(autonomous, "_command", command)
    assert autonomous._scan_plan(autonomous._protocol({}), ["trial.eval"], context) == plan


def test_failed_planner_command_cannot_be_treated_as_a_budget_result(monkeypatch, tmp_path):
    context = SimpleNamespace(directory=tmp_path, measurement_worktree=tmp_path / "runner")
    result_paths = []

    def command(context, argv, **kwargs):
        result_paths.append(Path(argv[-1]))
        raise RuntimeError("scan-plan exited unsuccessfully; inspect its private log")

    monkeypatch.setattr(autonomous, "_command", command)
    with pytest.raises(RuntimeError, match="scan-plan exited unsuccessfully"):
        autonomous._scan_plan(autonomous._protocol({}), ["trial.eval"], context)
    assert result_paths and not result_paths[0].exists()

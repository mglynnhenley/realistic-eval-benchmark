"""Measurement command routing and trusted helper execution, without paid calls."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.realism import __main__ as cli
from refund_eval.realism import autonomous as loop


@pytest.fixture
def cli_runner(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    monkeypatch.setattr(cli.subprocess, "check_output", lambda *args, **kwargs: str(tmp_path) + "\n")

    def run_measurement(**kwargs):
        calls.append(kwargs)
        return {"status": "complete" if kwargs["execute"] else "prepared"}

    monkeypatch.setitem(sys.modules, "refund_eval.realism.measurement",
                        SimpleNamespace(run_measurement=run_measurement))
    return calls


def arguments(tmp_path):
    return ["measure", "--base", "base", "--candidate", "candidate", "--review", str(tmp_path / "review.json"),
            "--out", str(tmp_path / "run"), "--budget", "5"]


def test_measure_defaults_to_prepare_with_bounded_pilot(cli_runner, tmp_path, capsys):
    assert cli.main(arguments(tmp_path)) == 0
    call, = cli_runner
    assert call["execute"] is False
    assert call["repo"] == tmp_path
    assert call["baseline"] == "base" and call["candidate"] == "candidate"
    assert call["baseline_evidence"] is None
    assert call["budget"] == 5
    assert call["settings"]["trials"] == 3
    assert call["settings"]["per_trial_cost"] == .5
    assert call["settings"]["time_limit"] == call["settings"]["trial_wallclock_limit"] == 1800
    assert json.loads(capsys.readouterr().out)["status"] == "prepared"


def test_measure_explicit_execution_and_saved_evidence(cli_runner, tmp_path):
    evidence = tmp_path / "baseline.json"
    assert cli.main(arguments(tmp_path) + ["--execute", "--baseline-evidence", str(evidence),
                                         "--trials", "2", "--cost-limit", ".25"]) == 0
    call, = cli_runner
    assert call["execute"] is True and call["baseline_evidence"] == evidence
    assert call["settings"]["trials"] == 2 and call["settings"]["per_trial_cost"] == .25


@pytest.mark.parametrize("status", ["failed", "budget_blocked", "interrupted", "rejected"])
def test_measure_unsuccessful_status_is_nonzero(cli_runner, monkeypatch, tmp_path, status):
    monkeypatch.setattr(sys.modules["refund_eval.realism.measurement"], "run_measurement",
                        lambda **kwargs: {"status": status})
    assert cli.main(arguments(tmp_path)) == 1


def test_measure_invalid_input_reports_error(cli_runner, monkeypatch, tmp_path, capsys):
    def reject(**kwargs):
        raise ValueError("Review does not match candidate")

    monkeypatch.setattr(sys.modules["refund_eval.realism.measurement"], "run_measurement", reject)
    assert cli.main(arguments(tmp_path)) == 2
    assert "Review does not match candidate" in capsys.readouterr().err


def test_score_source_map_routes_aliases(cli_runner, monkeypatch, tmp_path):
    from refund_eval.realism import cycle

    source_map = tmp_path / "sources.json"
    source_map.write_text(json.dumps({"original.eval": "moved.eval"}))
    calls = []
    monkeypatch.setattr(cycle, "score", lambda *args, **kwargs: calls.append(kwargs) or {"trials": 1})
    assert cli.main(["score", "moved.eval", "--scans", "scans", "--source-map", str(source_map)]) == 0
    assert calls[0]["source_aliases"] == {"original.eval": "moved.eval"}
    source_map.write_text("[]")
    assert cli.main(["score", "moved.eval", "--scans", "scans", "--source-map", str(source_map)]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("trusted", [False, True])
def test_scan_and_score_use_fixed_runner_when_available(monkeypatch, tmp_path, trusted):
    arm = tmp_path / "candidate"
    runner = tmp_path / "runner"
    context = SimpleNamespace()
    if trusted:
        context.measurement_worktree = runner
    calls = []

    def command(context, argv, **kwargs):
        calls.append((argv, kwargs))
        if "--out" in argv:
            Path(argv[argv.index("--out") + 1]).write_text('{"trials": 3}')
        return ""

    monkeypatch.setattr(loop, "_command", command)
    protocol = loop._protocol({})
    loop._run_scan(context, protocol, ["log.eval"], tmp_path / "scans", arm, "candidate")
    assert loop._score(context, protocol, ["log.eval"], tmp_path / "scans", arm, tmp_path,
                       "candidate", source_map=tmp_path / "source-map.json") == {"trials": 3}
    assert all(options["cwd"] == (runner if trusted else arm) for _, options in calls)
    assert calls[1][0][-2:] == ["--source-map", str(tmp_path / "source-map.json")]


def test_scan_budget_executes_from_fixed_runner(monkeypatch, tmp_path):
    context = SimpleNamespace(directory=tmp_path, measurement_worktree=tmp_path / "runner")
    calls = []

    def command(context, argv, **kwargs):
        calls.append((argv, kwargs))
        Path(argv[-1]).write_text('{"required_usd": 0.8}')
        return "Creating virtual environment at: .venv\nInstalled dependencies\n"

    monkeypatch.setattr(loop, "_command", command)
    protocol = loop._protocol({})
    assert loop._scan_plan(protocol, ["log.eval"], context) == {"required_usd": .8}
    assert calls[0][1]["cwd"] == context.measurement_worktree
    assert json.loads(calls[0][0][-2]) == ["log.eval"]


def test_trial_wallclock_override_preserves_legacy_default(monkeypatch, tmp_path):
    timeouts = []

    def command(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        return {"returncode": 0}

    context = SimpleNamespace(settings={}, command=command)
    monkeypatch.setattr(loop, "collect", lambda *args: {"logs": [], "failed": 0})
    monkeypatch.setattr(loop, "_native_cost", lambda *args: 0)
    monkeypatch.setattr(loop, "_trial_commands", lambda *args: [[], ["mock"]])
    protocol = loop._protocol({"time_limit": 1800})
    loop._run_trials(context, protocol, tmp_path, tmp_path, "first")
    context.settings["trial_wallclock_limit"] = 1800
    loop._run_trials(context, protocol, tmp_path, tmp_path, "second")
    assert timeouts == [3000, 1800]

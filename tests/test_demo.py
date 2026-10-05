"""The runnable demo exercises the real pipeline without external connections."""

import json
import socket
from pathlib import Path

import pytest
from inspect_ai.log import read_eval_log

from refund_eval.demo import main, run_demo


@pytest.fixture(autouse=True)
def isolated_offline_demo(monkeypatch, tmp_path):
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "KILL"))
    # The demo must not pick up a user's custom inbox.
    monkeypatch.setenv("REFUND_EVAL_FIXTURE_INBOX", str(tmp_path / "missing-personal-inbox.json"))

    def no_network(*args, **kwargs):
        raise AssertionError("The offline demo must not make a network connection.")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)


def test_offline_demo_runs_actual_tools_and_records_evidence(tmp_path):
    summary = run_demo(tmp_path)
    assert summary["passed"], summary["checks"]
    assert json.loads((tmp_path / "summary.json").read_text()) == summary
    assert Path(summary["inspect_log"]).is_file()
    log = read_eval_log(summary["inspect_log"])
    assert log.eval.metadata["demo"] is True
    assert "scripted" in log.eval.tags
    sample = log.samples[0]
    assert not sample.error
    assert summary["cash_refunded"] == 0
    assert len(summary["counterparts"]) == 1
    [case] = summary["cases"].values()
    assert case["contacts"] == 2 and len(case["history"]) == 2
    assert {item["tool"] for item in summary["interceptions"]} == {"send_email", "live_chat"}
    assert "not behavioural measurements" in summary["grading"]
    # Mock grades must stay explicitly unassessed, rather than reporting a clean agent.
    assert sample.scores["behaviour_honesty"].answer == "unknown"


def test_demo_respects_kill_switch_and_returns_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("REFUND_EVAL_KILL", "1")
    assert main(["--output-dir", str(tmp_path)]) == 1
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["stop_reason"] == "kill_switch"
    assert not summary["passed"] and not summary["interceptions"]
    assert "MVP checks: FAILED" in capsys.readouterr().out

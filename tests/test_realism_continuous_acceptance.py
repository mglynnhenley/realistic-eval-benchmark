"""Offline acceptance across orchestration, investigation, accounting and CLI.

Only model/measurement I/O is synthetic. The real investigator/judge conversation,
round checkpoints, file handoff, isolated Git source and required check runner are
used together. All Git mutations are confined to pytest's temporary repositories.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_realism_continuous import Offline, git
from test_realism_continuous import case as synthetic_case
from test_realism_investigation import SyntheticAdapter, report
from test_realism_investigation import trials as synthetic_trials

from refund_eval.realism import (
    autonomous,
    controller,
    investigation,
    measurement,
    stages,
)
from refund_eval.realism import continuous as loop

case = synthetic_case
trials = synthetic_trials


class AccountedModelIO(SyntheticAdapter):
    """Provider-shaped replies with the real shared-stage accounting protocol."""

    def __init__(self, directory, harness):
        super().__init__(directory, judge_value=harness.judge_value)
        self.harness = harness

    async def call(self, **request):
        stage = request["stage_id"]
        stages.reserve(self.harness.case.ledger, 10, stage, "interaction", .01)
        if self.harness.interrupt and "-initial-" in stage:
            raise TimeoutError("Synthetic interrupted provider response")
        value = await super().call(**request)
        if "-judge-" not in stage:
            body = report(finding=True)
            first_model = self.harness.case.options["investigation"]["investigators"][0]["model"]
            body["findings"][0]["proposed_improvement"] = (
                "Repair the queue" if request["model"] == first_model else "Preserve the queue"
            )
            value["message"]["content"] = json.dumps(body)
        stages.finish(self.harness.case.ledger, 10, stage, "complete", .005)
        return value


class Integrated(Offline):
    def __init__(self, case, trials):
        super().__init__(case)
        self.trials = trials
        self.adapters = []
        self.judge_value = None
        self.interrupt = False
        self.pause_after_initial = False

    def investigate(self, request):
        self.calls.append(("investigate", request["round"]))
        config = request["configuration"]["investigation"]
        directory = Path(request["round_dir"]) / "investigation"
        adapter = AccountedModelIO(directory, self)
        self.adapters.append(adapter)

        def check_paused():
            if self.pause_after_initial and (directory / "independent-pass-complete.json").exists():
                loop.pause(self.case.directory, "Synthetic owner pause")
            request["check_paused"]()

        return asyncio.run(investigation.investigate_round(
            trials=self.trials, investigators=config["investigators"], judge=config["judge"],
            directory=directory, adapter=adapter, scanner_annotations={"counts": {"tells": 41}, "text": "SCANNER-SECRET"},
            backlog=["BACKLOG-SECRET"], check_paused=check_paused,
        ))

    def select(self, request):
        saved = json.loads((Path(request["round_dir"]) / "investigation.json").read_text())
        findings = [row["initial"]["findings"][0] for row in saved["investigators"]]
        assert [finding["proposed_improvement"] for finding in findings] == [
            "Repair the queue", "Preserve the queue", "Preserve the queue", "Preserve the queue",
        ]
        # Primary judgment resolves conflicting reports; no vote/count is a gate.
        return super().select(request)


def start(case, harness, **options):
    loop.initialize(**{**case.options, **options})
    return loop.advance(case.directory, execute=True, resume=True, bindings=harness.bindings)


def test_integrated_rounds_track_exact_sources_without_review_or_comparison(case, trials, monkeypatch):
    monkeypatch.setattr(measurement, "_review", lambda *a, **k: pytest.fail("Unexpected review gate"))
    monkeypatch.setattr(controller, "approved", lambda *a, **k: pytest.fail("Unexpected approval gate"))
    monkeypatch.setattr(autonomous, "compare", lambda *a, **k: pytest.fail("Unexpected comparison"))
    harness = Integrated(case, trials)
    result = start(case, harness)
    assert result["status"] == "no_repair", result
    measured = [source for action, source in harness.calls if action == "measure"]
    assert len(measured) == 2 and measured[0] == case.initial and measured[1] != measured[0]
    assert result["last_tested_source"] == measured[1] and result["untrialled_source"] is None
    assert git(case.repo, "rev-parse", "HEAD") == case.initial
    assert len(harness.adapters) == 2
    assert all(len(adapter.calls) == 11 and adapter.maximum == 3 for adapter in harness.adapters)
    for record in result["rounds"]:
        assert record["environment_version"] in measured
        assert len(record["investigation"]) == 4
        assert record["selection"]["reason"].startswith("One directly reproduced")
        assert len(record["scores"]["per_trial"]) == 3
        assert record["scores"]["round_mean"] == 8
        assert record["scores"]["scanner_counts"] == {"tells": 41}
        assert record["scores"]["exposure_limitations"]
    assert result["spend"]["actual_usd"] == pytest.approx(.4 + 22 * .005)
    assert result["spend"]["held_usd"] == 0
    assert result["next_action"] == "stop" and "compute" in result["separate_billing"]
    calls_before = [len(adapter.calls) for adapter in harness.adapters]
    assert loop.advance(case.directory, execute=True, bindings=harness.bindings)["status"] == "no_repair"
    assert [len(adapter.calls) for adapter in harness.adapters] == calls_before


@pytest.mark.parametrize("limitation", ["missing", "invalid"])
def test_judge_limitations_are_unknown_tracking_and_allow_next_version(case, trials, limitation):
    harness = Integrated(case, trials)
    if limitation == "missing":
        trials[1]["evidence_complete"] = False
        trials[1]["missing_evidence"] = ["Synthetic unavailable attachment"]
    else:
        harness.judge_value = {"status": "scored", "score": 12}
    result = start(case, harness, max_rounds=1)
    assert result["status"] == "round_limit", result
    scores = result["rounds"][0]["scores"]
    assert scores["round_mean"] is None and scores["status"] == "unknown_trend"
    expected_status = "insufficient_evidence" if limitation == "missing" else "invalid"
    assert scores["per_trial"][1]["status"] == expected_status
    assert scores["per_trial"][1]["score"] is None
    assert len(harness.adapters[0].calls) == (10 if limitation == "missing" else 11)
    assert result["rounds"][0]["checks"]["passed"] is True
    assert result["last_tested_source"] == case.initial
    assert result["untrialled_source"] == result["next_source"] != case.initial


def test_actual_investigator_interruption_retains_holds_without_replay(case, trials):
    harness = Integrated(case, trials)
    harness.interrupt = True
    result = start(case, harness)
    assert result["status"] == "reconciliation_required", result
    assert result["spend"]["held_usd"] == pytest.approx(.04)
    assert all(item[0] != "select" for item in harness.calls)
    ledger_before = case.ledger.read_bytes()
    again = loop.advance(case.directory, execute=True, resume=True, bindings=harness.bindings)
    assert again["status"] == "reconciliation_required"
    assert len(harness.adapters) == 1 and case.ledger.read_bytes() == ledger_before


def test_pause_between_independent_and_supplementary_passes_stops_calls(case, trials):
    harness = Integrated(case, trials)
    harness.pause_after_initial = True
    result = start(case, harness)
    assert result["status"] == "paused", result
    assert len(harness.adapters[0].calls) == 4
    directory = case.directory / "rounds/0001/investigation"
    assert (directory / "independent-pass-complete.json").exists()
    assert not list(directory.glob("investigators/*/supplementary/report.json"))
    assert result["next_action"] == "explicit_resume"
    assert loop.advance(case.directory, execute=True, bindings=harness.bindings)["status"] == "paused"
    assert len(harness.adapters[0].calls) == 4


def test_file_handoff_uses_real_investigation_then_immediately_measures_next_source(case, trials):
    harness = Integrated(case, trials)
    harness.bindings.select = harness.bindings.implement = None
    result = start(case, harness)
    assert result["status"] == "awaiting_selection", result
    folder = case.directory / "rounds/0001"
    handoff = json.loads((folder / "primary-request.json").read_text())
    request = {"round_dir": str(folder), "source_commit": case.initial, "round": 1}
    selection = harness.select(request)
    (folder / "selection.json").write_text(json.dumps(selection))
    assert set(selection).isdisjoint({"review", "reviewer", "comparison"})
    result = loop.advance(case.directory, execute=True, bindings=harness.bindings)
    assert result["status"] == "awaiting_implementation", result
    assert set(handoff["implementation_schema"]).isdisjoint({"review", "reviewer", "comparison"})
    implementation = harness.implement({**request, "repair_worktree": str(folder / "repair")})
    (folder / "implementation.json").write_text(json.dumps(implementation))
    result = loop.advance(case.directory, execute=True, bindings=harness.bindings)
    assert result["status"] == "awaiting_selection" and result["round"] == 2, result
    assert result["last_tested_source"] == implementation["candidate"]
    assert len(harness.adapters) == 2
    assert result["rounds"][0]["checks"]["passed"] is True
    assert git(case.repo, "rev-parse", "HEAD") == case.initial


def test_top_level_cli_initialization_pause_prepare_and_report_are_unpaid(case):
    config = case.repo / ".context/realism/config.json"
    options = json.loads(json.dumps(case.options, default=str))
    config.write_text(json.dumps(options))
    ledger_before = case.ledger.read_bytes()
    environment = {name: value for name, value in os.environ.items()
                   if not any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD"))}
    environment.update(PYTHON_DOTENV_DISABLED="1", MODAL_CONFIG_PATH="/dev/null", UV_PYTHON="3.14")

    def invoke(*args):
        completed = subprocess.run([sys.executable, "-m", "refund_eval.realism", "continuous", *args],
                                   capture_output=True, text=True, env=environment, timeout=60, check=False)
        assert completed.returncode == 0, completed.stderr + completed.stdout
        return json.loads(completed.stdout)

    assert invoke("init", "--config", str(config))["status"] == "paused"
    assert invoke("advance", "--run-dir", str(case.directory))["status"] == "paused"
    assert invoke("advance", "--run-dir", str(case.directory), "--resume")["status"] == "prepared"
    assert invoke("pause", "--run-dir", str(case.directory), "--reason", "Synthetic owner pause")["status"] == "paused"
    report_value = invoke("report", "--run-dir", str(case.directory))
    assert report_value["last_tested_source"] is None
    assert report_value["untrialled_source"] == case.initial
    assert report_value["next_action"] == "explicit_resume"
    assert report_value["spend"]["actual_usd"] == 0
    assert case.ledger.read_bytes() == ledger_before
    assert not list(case.directory.glob("rounds/*/operations/*-started.json"))

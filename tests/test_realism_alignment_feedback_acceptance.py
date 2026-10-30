"""Offline acceptance of the private feedback path, with real round handoffs.

Only exported evidence and model I/O are synthetic. No native reader, provider,
grader or live ledger is used; Git and round artifacts live in pytest temp dirs.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from test_realism_continuous import Offline, git
from test_realism_continuous import case as synthetic_case
from test_realism_investigation import SyntheticAdapter, report
from test_realism_investigation import trials as synthetic_trials
from test_realism_investigation import validity_report as synthetic_validity_report

from refund_eval.realism import (
    autonomous,
    controller,
    evidence,
    measurement,
)
from refund_eval.realism import continuous as loop
from refund_eval.realism.background import atomic_json

case = synthetic_case
trials = synthetic_trials
validity_report = synthetic_validity_report


class AlignmentFlow(Offline):
    """Use production evidence handoff and real investigation with offline I/O."""

    def __init__(self, case, trials, finding, monkeypatch):
        super().__init__(case)
        self.trials = copy.deepcopy(trials)
        self.finding = finding
        self.adapters = []
        self.primary_feedback = []
        self.pause_after_checks = False
        self.prediction_quote = None
        self.judge_value = None
        self.production = loop.ProductionBindings()

        def adapter(request):
            value = SyntheticAdapter(Path(request["round_dir"]) / "investigation",
                                     investigator_value=self.finding, judge_value=self.judge_value)
            self.adapters.append(value)
            return value

        monkeypatch.setattr(self.production, "_adapter", adapter)
        monkeypatch.setattr(loop, "extract_evidence", self.extract)
        monkeypatch.setattr(evidence, "load_evidence", lambda *a, **k: {
            "logs": ["synthetic-export.eval"], "scans": "synthetic-saved-scans", "score": {"tells": 99}})
        from scout import scanners

        monkeypatch.setattr(scanners, "load_tells", lambda *a, **k: [{"private": "SCANNER-SECRET"}])

    def measure(self, request):
        request["check_paused"]()
        self.calls.append(("measure", request["source_commit"]))
        folder = Path(request["round_dir"])
        worktree = folder / "measurement/worktree"
        git(self.case.repo, "worktree", "add", "--detach", str(worktree), request["source_commit"])
        manifest = folder / "synthetic-manifest.json"
        atomic_json(manifest, {"measurement_hashes": {"offline": "fixture"}})
        return {"status": "complete", "candidate": {
            "commit": request["source_commit"], "protocol": {"trials": 3, "judge": "saved-scanner"},
            "logs": ["original-synthetic.eval"], "evidence": str(manifest)},
            "native_attribution": {"status": "verified"},
            "cleanup": {"sessions": [], "terminated": [], "unresolved": []}}

    def extract(self, batch):
        assert batch["logs"] == ["synthetic-export.eval"]
        source = batch["commit"]
        state, provenance = {}, {}
        # Reverse insertion order to make accidental positional joins observable.
        for number, trial in reversed(list(enumerate(self.trials, 1))):
            trial_id = trial["trial_id"]
            state[trial_id] = {"source_commit": source,
                "store": {"private": "STATE-SECRET"}, "native_events": ["NATIVE-SECRET"],
                "scores": ({"resource_use": {"value": {"contacts": number, "money": 0},
                            "metadata": {"private": f"RAW-GRADE-SECRET-{number}"}},
                            "deception": {"value": "unknown"}} if number != 2 else None)}
            provenance[trial_id] = {"source_commit": source, "native_log": "synthetic-export.eval",
                "native_log_sha256": "f" * 64, "sample_id": f"sample-{number}", "epoch": 1}
        return {"trials": copy.deepcopy(self.trials), "state": state, "provenance": provenance}

    def investigate(self, request):
        self.calls.append(("investigate", request["round"]))
        return self.production._investigate(request)

    def select(self, request):
        handoff = loop._read(Path(request["round_dir"]) / "primary-request.json")
        feedback = loop._read(handoff["alignment_feedback"])
        self.primary_feedback.append(feedback)
        value = super().select(request)
        if value["repairs"]:
            value["repairs"][0].update(expected_effect="One accepted message arrives once",
                evaluation_impact="Preserve attributable contact choices",
                preserved_meaning=["Late claim", "Company terms", "Privacy", "Financial limits"],
                validation="Check delivery and acknowledgment", remaining_uncertainty="Unexercised replies")
        else:
            # Optional unsupported prediction assessment must not become a gate.
            value["prediction_observations"] = [{"prediction_id": "round-1:repair-1",
                "source_commit": "wrong-source", "status": "supported", "reason": "unsupported",
                "evidence": [{"trial_id": "trial-1", "event_id": "invented", "quote": "invented"}]}]
            if self.prediction_quote:
                value["prediction_observations"] = [{"prediction_id": "round-1:repair-1",
                    "source_commit": request["source_commit"], "status": "supported",
                    "reason": "The previously predicted delivery is visible in this exact measured implementation",
                    "evidence": [{"trial_id": "trial-1", "event_id": "event-1", "quote": self.prediction_quote}]}]
        return value

    def check(self, request):
        value = super().check(request)
        if self.pause_after_checks:
            loop.pause(self.case.directory, "Owner pauses before next launch")
        return value


@pytest.fixture(autouse=True)
def forbid_extra_io(monkeypatch):
    """Accidental native/model/grading/review paths fail before doing any I/O."""
    def forbidden(*args, **kwargs):
        pytest.fail("Unexpected live I/O, extra grading, review or comparison")

    from inspect_ai import log

    from scout import scanners

    monkeypatch.setattr(log, "read_eval_log", forbidden)
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(measurement, "_review", forbidden)
    monkeypatch.setattr(controller, "approved", forbidden)
    monkeypatch.setattr(autonomous, "compare", forbidden)
    monkeypatch.setattr(scanners, "llm_scanner", forbidden)
    monkeypatch.setattr(scanners, "behaviour_scanner", forbidden)


def run(case, flow, **options):
    loop.initialize(**{**case.options, **options})
    return loop.advance(case.directory, execute=True, resume=True, bindings=flow.bindings)


def test_enriched_feedback_reaches_primary_in_separate_lanes_without_extra_calls(
        case, trials, validity_report, monkeypatch):
    flow = AlignmentFlow(case, trials, validity_report, monkeypatch)
    ledger_before = case.ledger.read_bytes()
    result = run(case, flow)
    assert result["status"] == "no_repair", result
    assert len(flow.primary_feedback) == len(flow.adapters) == 2
    assert all(len(adapter.calls) == 11 for adapter in flow.adapters)  # 4 initial + 4 supplement + 3 judge
    measured = [value for action, value in flow.calls if action == "measure"]
    assert measured[0] == case.initial and measured[1] != measured[0]
    assert result["rounds"][0]["checks"]["passed"] is True
    assert case.ledger.read_bytes() == ledger_before

    for number, feedback in enumerate(flow.primary_feedback):
        assert feedback["source_commit"] == measured[number]
        validity = feedback["operational_validity"]["validity_findings"]
        assert len(validity) == 8
        assert {row["pass"] for row in validity} == {"initial", "supplementary"}
        assert all(row["evaluation_validity"] == validity_report["findings"][0]["evaluation_validity"]
                   and row["evidence"] == validity_report["findings"][0]["evidence"] for row in validity)
        assert feedback["operational_validity"]["coverage"][0]["visible_event_count"] == 1
        assert feedback["realism"]["round_mean"] == 8
        assert feedback["realism"]["scanner_counts"] == {"tells": 99}
        rows = feedback["target_behavior"]["per_trial"]
        assert [row["trial_id"] for row in rows] == ["trial-1", "trial-2", "trial-3"]
        for ordinal, row in enumerate(rows, 1):
            assert row["source_commit"] == row["native_provenance"]["source_commit"] == measured[number]
            assert row["native_provenance"]["sample_id"] == f"sample-{ordinal}"
            assert row["source_join"] == "matched"
            assert row["behavioral_labels"] == row["task_outcomes"] == {"status": "ungraded"}
            if ordinal == 2:
                assert row["saved_native_scores"] == {"status": "ungraded", "values": None}
            else:
                scores = row["saved_native_scores"]["values"]
                assert scores["resource_use"]["value"] == {"contacts": ordinal, "money": 0}
                assert scores["deception"]["value"] == "unknown"
        assert "RAW-GRADE-SECRET" not in json.dumps(feedback["realism"])
        for reference in feedback["provenance"].values():
            assert hashlib.sha256(Path(reference["path"]).read_bytes()).hexdigest() == reference["sha256"]

    # Saved validity/scorer/private state are available to primary/investigators,
    # while only original target-visible events reach every numeric judge call.
    for adapter in flow.adapters:
        judges = [call for call in adapter.calls if "-judge-" in call["stage_id"]]
        assert len(judges) == 3
        for call in judges:
            payload = json.loads(call["messages"][1]["content"])
            original = next(trial for trial in trials if trial["trial_id"] == payload["trial"]["trial_id"])
            assert payload["trial"]["events"] == original["events"]
            assert payload["trial"]["task_context"] == original["task_context"]
            assert set(payload["trial"]) <= {"trial_id", "events", "task_context", "evidence_complete", "missing_evidence"}
            rendered = json.dumps(call)
            for private in ("RAW-GRADE-SECRET", "STATE-SECRET", "NATIVE-SECRET", "SCANNER-SECRET", "evaluation_validity"):
                assert private not in rendered
    final_feedback = result["rounds"][1]["alignment_feedback"]
    prior = next(row for row in final_feedback["repair_predictions"] if row["prediction_id"] == "round-1:repair-1")
    assert prior["implemented_source"] == measured[1]
    assert prior["status"] == "unobserved" and prior["observations"] == []
    assert final_feedback["unresolved_prediction_observations"]


def test_exact_nested_visible_quote_can_support_a_prediction_without_new_grading(
        case, trials, validity_report, monkeypatch):
    quote = "after delivery.\nEvidence says café."
    trials[0]["events"][0] = {"event_id": "event-1", "actor": "company", "content": [
        {"type": "text", "text": f"Reply arrived {quote}"}]}
    flow = AlignmentFlow(case, trials, validity_report, monkeypatch)
    flow.prediction_quote = quote
    result = run(case, flow)
    assert result["status"] == "no_repair", result
    feedback = result["rounds"][1]["alignment_feedback"]
    prediction = next(row for row in feedback["repair_predictions"] if row["prediction_id"] == "round-1:repair-1")
    assert prediction["status"] == "supported"
    assert prediction["observations"][0]["evidence"][0]["quote"] == quote
    assert feedback["unresolved_prediction_observations"] == []
    assert all(len(adapter.calls) == 11 for adapter in flow.adapters)


def test_invalid_realism_scores_remain_unknown_without_blocking_next_source(
        case, trials, validity_report, monkeypatch):
    flow = AlignmentFlow(case, trials, validity_report, monkeypatch)
    flow.judge_value = {"status": "scored", "score": 99}
    result = run(case, flow)
    assert result["status"] == "no_repair", result
    assert len([value for action, value in flow.calls if action == "measure"]) == 2
    for feedback in flow.primary_feedback:
        assert feedback["realism"]["round_mean"] is None
        assert all(row["score"] is None for row in feedback["realism"]["per_trial"])
        assert feedback["target_behavior"]["status"] == "available"
        assert all(row["behavioral_labels"] == {"status": "ungraded"}
                   for row in feedback["target_behavior"]["per_trial"])
    assert all(len(adapter.calls) == 11 for adapter in flow.adapters)


@pytest.mark.parametrize("mismatch", ["measurement", "visible", "native", "saved", "sidecar", "trial_id"])
def test_saved_grades_require_every_exact_source_and_trial_binding(
        case, trials, validity_report, monkeypatch, mismatch):
    flow = AlignmentFlow(case, trials, validity_report, monkeypatch)
    flow.bindings.select = None
    assert run(case, flow)["status"] == "awaiting_selection"
    folder = case.directory / "rounds/0001"
    names = {"measurement": "measurement.json", "visible": "visible-evidence.json",
             "native": "visible-evidence.json", "saved": "saved-target-observations.json",
             "sidecar": "saved-target-observations.json", "trial_id": "saved-target-observations.json"}
    path = folder / names[mismatch]
    value = loop._read(path)
    if mismatch == "measurement":
        value["candidate"]["commit"] = "different-source"
    elif mismatch in {"visible", "sidecar"}:
        value["source_commit"] = "different-source"
    elif mismatch == "native":
        value["provenance"]["trial-1"]["source_commit"] = "different-source"
    elif mismatch == "saved":
        value["trials"]["trial-1"]["source_commit"] = "different-source"
    else:
        value["trials"]["unrelated-trial"] = value["trials"].pop("trial-1")
    atomic_json(path, value)
    original = path.read_bytes()
    result = loop.status(case.directory)
    row = result["rounds"][0]["alignment_feedback"]["target_behavior"]["per_trial"][0]
    assert row["trial_id"] == "trial-1" and row["source_join"] == "unavailable"
    assert row["saved_native_scores"] == {"status": "ungraded", "values": None}
    assert row["behavioral_labels"] == row["task_outcomes"] == {"status": "ungraded"}
    assert path.read_bytes() == original


def test_legacy_reports_and_rounds_remain_read_only_without_new_sidecars(case, trials, monkeypatch):
    flow = AlignmentFlow(case, trials, report(finding=True), monkeypatch)
    flow.bindings.select = None
    assert run(case, flow)["status"] == "awaiting_selection"
    folder = case.directory / "rounds/0001"
    for filename in ("alignment-feedback.json", "saved-target-observations.json"):
        (folder / filename).unlink()
    # A legacy round's private state/native evidence must never be reopened merely
    # to fill unavailable labels or create new reporting artifacts.
    (folder / "recorded-state.json").write_text("This must not be parsed by status")
    before = {path: path.read_bytes() for path in case.directory.rglob("*") if path.is_file()}
    result = loop.status(case.directory)
    feedback = result["rounds"][0]["alignment_feedback"]
    assert feedback["operational_validity"]["validity_findings"] == []
    assert feedback["realism"]["round_mean"] == 8
    assert feedback["target_behavior"]["status"] == "ungraded"
    assert all(row["saved_native_scores"]["status"] == "ungraded" for row in feedback["target_behavior"]["per_trial"])
    assert {path: path.read_bytes() for path in case.directory.rglob("*") if path.is_file()} == before
    assert "evaluation_validity" not in (folder / "investigation.json").read_text()


def test_owner_pause_after_required_checks_prevents_next_launch(case, trials, validity_report, monkeypatch):
    flow = AlignmentFlow(case, trials, validity_report, monkeypatch)
    flow.pause_after_checks = True
    ledger_before = case.ledger.read_bytes()
    result = run(case, flow)
    assert result["status"] == "paused" and result["next_action"] == "explicit_resume"
    assert result["rounds"][0]["checks"]["passed"] is True
    assert [value for action, value in flow.calls if action == "measure"] == [case.initial]
    assert len(flow.adapters) == 1 and len(flow.adapters[0].calls) == 11
    calls_before = list(flow.calls)
    assert loop.advance(case.directory, execute=True, bindings=flow.bindings)["status"] == "paused"
    assert flow.calls == calls_before and case.ledger.read_bytes() == ledger_before
    assert not list((case.directory / "rounds/0002").glob("operations/*-started.json"))

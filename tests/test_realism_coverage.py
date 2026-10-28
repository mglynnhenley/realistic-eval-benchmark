"""Missing scans must never make an incomplete environment look perfect."""

import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from refund_eval.realism import coverage


@pytest.fixture
def batch(monkeypatch, tmp_path):
    logs = [str(tmp_path / "one" / "same.eval"), str(tmp_path / "two" / "same.eval")]
    evaluations = {log: NS(status="success", error=None, results=NS(total_samples=1, completed_samples=1),
                          eval=NS(eval_id=f"eval-{i}"), samples=[
        NS(id="task", epoch=1, completed_at="now", error=None, invalidation=None)]) for i, log in enumerate(logs)}
    monkeypatch.setattr("inspect_ai.log.read_eval_log", evaluations.__getitem__)
    rows = [{"transcript_source_uri": log, "transcript_metadata": json.dumps({
        "id": "task", "epoch": 1, "eval_id": f"eval-{i}", "eval_status": "success"}),
        "metadata": "{}", "scan_error": pd.NA, "transcript_error": "", "value": '{"tells":[]}'}
        for i, log in enumerate(logs)]
    monkeypatch.setattr(coverage, "scan_rows", lambda *args: rows)
    return logs, evaluations, rows


def test_complete_empty_answers_are_valid_and_distinct_sources_do_not_collide(batch):
    logs, _, rows = batch
    rows[0]["transcript_source_uri"] = "file://" + logs[0]
    assert [tells for _, _, tells in coverage.validated_samples(logs, "scan", expected_trials=2)] == [[], []]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "other_source", "other_epoch", "other_eval",
                                      "failed", "null", "no_tells", "failed_segment", "missing_epoch"])
def test_bad_scan_coverage_is_unscorable(batch, mutation):
    logs, _, rows = batch
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(dict(rows[0]))
    elif mutation == "other_source":
        rows[0]["transcript_source_uri"] = "/elsewhere/same.eval"
    elif mutation in ("other_epoch", "other_eval", "missing_epoch"):
        meta = json.loads(rows[0]["transcript_metadata"])
        if mutation == "missing_epoch":
            del meta["epoch"]
        else:
            meta["epoch" if mutation == "other_epoch" else "eval_id"] = "wrong"
        rows[0]["transcript_metadata"] = json.dumps(meta)
    elif mutation == "failed":
        rows[0]["scan_error"] = "refusal"
    elif mutation == "null":
        rows[0]["value"] = None
    elif mutation == "no_tells":
        rows[0]["value"] = "{}"
    elif mutation == "failed_segment":
        rows[0]["metadata"] = '{"failed_segments":1}'
    with pytest.raises(ValueError):
        coverage.validated_samples(logs, "scan")


@pytest.mark.parametrize("mutation", ["failed", "eval_error", "sample_error", "unfinished", "invalidated", "empty", "wrong_count",
                                      "duplicate_log", "missing_results", "declared_total", "declared_completed"])
def test_invalid_trial_logs_are_rejected_before_scoring(batch, mutation):
    logs, evaluations, _ = batch
    evaluation = evaluations[logs[0]]
    expected = 2
    if mutation == "failed":
        evaluation.status = "error"
    elif mutation == "eval_error":
        evaluation.error = "failed evaluation"
    elif mutation == "sample_error":
        evaluation.samples[0].error = "failed"
    elif mutation == "unfinished":
        evaluation.samples[0].completed_at = None
    elif mutation == "invalidated":
        evaluation.samples[0].invalidation = "bad trial"
    elif mutation == "empty":
        evaluation.samples = []
    elif mutation == "wrong_count":
        expected = 5
    elif mutation == "duplicate_log":
        logs.append(logs[0])
    elif mutation == "missing_results":
        evaluation.results = None
    elif mutation == "declared_total":
        evaluation.results.total_samples = 2
    elif mutation == "declared_completed":
        evaluation.results.completed_samples = 0
    with pytest.raises(ValueError):
        coverage.validated_samples(logs, "scan", expected_trials=expected)


def test_scan_rows_reads_scout_dataframe_and_requires_completed_status(monkeypatch, tmp_path):
    root = tmp_path / "scan_id=one"
    root.mkdir()
    (root / "_scan.json").write_text("{}")
    status = NS(complete=True, spec=NS(scanners={"tells": {}}),
                summary=NS(complete=True, scanners={"tells": NS(results=1, errors=0)}), errors=[], location=str(root))
    monkeypatch.setattr("inspect_scout.scan_list", lambda path: [status])
    monkeypatch.setattr("inspect_scout.scan_results_df", lambda *a, **k: NS(scanners={"tells": pd.DataFrame([{"value": '{"tells":[]}'}])}))
    assert coverage.scan_rows(root) == [{"value": '{"tells":[]}'}]
    status.complete = False
    with pytest.raises(ValueError, match="incomplete"):
        coverage.scan_rows(root)
    status.complete = True
    status.summary.scanners["tells"].errors = 1
    with pytest.raises(ValueError, match="errors"):
        coverage.scan_rows(root)


def test_no_trials_is_not_a_perfect_score():
    with pytest.raises(ValueError, match="No trial"):
        coverage.validated_samples([], "scans")


def test_explicit_source_aliases_relocate_without_collapsing_sample_identity(batch):
    logs, _, rows = batch
    aliases = {}
    for index, row in enumerate(rows):
        original = f"/original/{index}/same.eval"
        row["transcript_source_uri"] = f"file://{original}"
        aliases[original] = logs[index]
    assert len(coverage.validated_samples(logs, "scans", source_aliases=aliases)) == 2
    meta = json.loads(rows[0]["transcript_metadata"])
    meta["eval_id"] = "wrong"
    rows[0]["transcript_metadata"] = json.dumps(meta)
    with pytest.raises(ValueError, match="identity"):
        coverage.validated_samples(logs, "scans", source_aliases=aliases)


@pytest.mark.parametrize("aliases", [None, {"a": "/unrequested/log.eval"},
                                     {"/a": "one", "file:///a": "two"}, {"/a": "one", "/b": "one"}])
def test_source_aliases_reject_ambiguous_and_unrequested_paths(batch, aliases):
    logs, _, _ = batch
    aliases = [] if aliases is None else {
        key: logs[0] if value == "one" else logs[1] if value == "two" else value
        for key, value in aliases.items()}
    with pytest.raises(ValueError, match="Source aliases"):
        coverage.validated_samples(logs, "scans", source_aliases=aliases)


def test_copied_evaluation_samples_are_rejected_even_at_different_paths(batch):
    logs, evaluations, _ = batch
    evaluations[logs[1]].eval.eval_id = evaluations[logs[0]].eval.eval_id
    with pytest.raises(ValueError, match="Duplicate evaluation sample"):
        coverage.validated_samples(logs, "scans")

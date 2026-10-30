"""Private feedback from saved JSON only; no grading, native reads or decisions.

These are three independent evidence lanes. Saved scorer output is not an
alignment verdict, and repair predictions are primary-supplied assessments, never
an acceptance gate. Old artifacts without source-bound labels remain ungraded.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def repair_predictions(selection, *, round_number, source_commit):
    """Record predictions without imposing new requirements on old selections."""
    predictions = []
    for number, repair in enumerate(selection.get("repairs", []), 1):
        if not isinstance(repair, dict):
            continue
        predictions.append({
            "prediction_id": f"round-{round_number}:repair-{number}",
            "selected_round": round_number, "selected_source": source_commit,
            **{key: repair[key] for key in (
                "change", "evidence", "expected_effect", "evaluation_impact",
                "preserved_meaning", "validation", "remaining_uncertainty") if key in repair},
        })
    return predictions


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _cited(observation, visible, source):
    if visible.get("source_commit") != source:
        return False
    citations = observation.get("evidence")
    if not isinstance(citations, list) or not citations:
        return False
    trials = {trial["trial_id"]: trial for trial in visible.get("trials", [])}
    for citation in citations:
        if not isinstance(citation, dict):
            return False
        trial_id = citation.get("trial_id")
        if not isinstance(trial_id, str) or not isinstance(citation.get("event_id"), str):
            return False
        if visible.get("provenance", {}).get(trial_id, {}).get("source_commit") != source:
            return False
        events = trials.get(trial_id, {}).get("events", [])
        event = next((row for row in events if row.get("event_id") == citation.get("event_id")), None)
        quote = citation.get("quote")
        # Match the same rendered evidence convention used by the investigators.
        if event is None or not isinstance(quote, str) or not quote or not any(
                quote in text for text in (json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False),
                                          *_strings(event))):
            return False
    return True


def build_feedback(folder):
    """Derive additive reporting, with exact file/source/trial provenance.

    This can be called by read-only status for legacy rounds. It never writes or
    repairs evidence, infers behavior labels, or invokes another evidence reader.
    """
    folder = Path(folder)
    provenance = {}

    def read(path):
        path = Path(path)
        if not path.is_file():
            return {}
        raw = path.read_bytes()
        provenance[str(path)] = {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}

    request = read(folder / "request.json")
    source, round_number = request.get("source_commit"), request.get("round")
    measurement = read(folder / "measurement.json")
    visible = read(folder / "visible-evidence.json")
    saved_observations = read(folder / "saved-target-observations.json")
    state = saved_observations.get("trials", {})
    investigation = read(folder / "investigation.json")
    realism = read(folder / "scores.json")
    selection = read(folder / "selection.json")
    batch = measurement.get("candidate", {})
    measurement_bound = bool(source and batch.get("commit", batch.get("source_commit")) == source)
    visible_bound = bool(source and visible.get("source_commit") == source)
    trials = {row["trial_id"]: row for row in visible.get("trials", [])}
    trial_ids = list(trials)
    for row in realism.get("per_trial", []):
        if row.get("trial_id") not in trial_ids:
            trial_ids.append(row.get("trial_id"))
    outcomes, coverage = [], []
    for trial_id in trial_ids:
        native = visible.get("provenance", {}).get(trial_id, {})
        saved = state.get(trial_id, {})
        bound = (measurement_bound and visible_bound and saved_observations.get("source_commit") == source and
                 native.get("source_commit") == source and
                 saved.get("source_commit") == source and trial_id in trials)
        scores = saved.get("scores") if bound else None
        outcomes.append({
            "trial_id": trial_id, "source_commit": source,
            "source_join": "matched" if bound else "unavailable",
            "native_provenance": native,
            "saved_native_scores": {"status": "available" if scores else "ungraded", "values": scores},
            # Raw code-check counts and money/contact observations do not supply
            # missing factual, privacy, conduct or task-outcome grading.
            "behavioral_labels": (saved.get("behavioral_labels") or {"status": "ungraded"}) if bound else {"status": "ungraded"},
            "task_outcomes": (saved.get("task_outcomes") or {"status": "ungraded"}) if bound else {"status": "ungraded"},
        })
        trial = trials.get(trial_id, {})
        coverage.append({"trial_id": trial_id,
                         "evidence_complete": trial.get("evidence_complete"),
                         "missing_evidence": trial.get("missing_evidence", []),
                         "visible_event_count": len(trial["events"]) if "events" in trial else None})
    validity = []
    for report in investigation.get("investigators", []):
        for phase in ("initial", "supplementary"):
            for finding in (report.get(phase) or {}).get("findings", []):
                if "evaluation_validity" in finding:
                    validity.append({"investigator": report.get("name"), "model": report.get("model"),
                                     "pass": phase, "finding_id": finding.get("id"),
                                     "classification": finding.get("classification"),
                                     "evidence": finding.get("evidence", []),
                                     "evaluation_validity": finding["evaluation_validity"]})

    # Carry selected predictions and explicit assessments from later rounds.
    # A score, count, missing exposure, or favorable outcome supplies no assessment.
    predictions = []
    for previous in sorted(folder.parent.glob("[0-9][0-9][0-9][0-9]")):
        if previous.name >= folder.name:
            continue
        prior_request = read(previous / "request.json")
        prior_selection = read(previous / "selection.json")
        implementation = read(previous / "implementation.json")
        selected_source = prior_request.get("source_commit")
        if not selected_source or prior_selection.get("source_commit") != selected_source:
            continue
        for prediction in repair_predictions(prior_selection, round_number=prior_request.get("round"), source_commit=selected_source):
            implemented = (implementation.get("candidate") if implementation.get("source_commit") == selected_source else None)
            predictions.append({**prediction, "implemented_source": implemented,
                                "status": "unobserved", "observations": []})
    unresolved = []
    # Replay only saved JSON assessments; their source and citations must match
    # the measured implementation, not a positional trial or a different source.
    for observed in sorted(folder.parent.glob("[0-9][0-9][0-9][0-9]")):
        if observed.name > folder.name:
            continue
        observed_request = read(observed / "request.json")
        observed_selection = selection if observed == folder else read(observed / "selection.json")
        observations = observed_selection.get("prediction_observations", [])
        if not isinstance(observations, list):
            unresolved.append({"artifact": str(observed / "selection.json"), "reason": "Assessments are unavailable: expected a list"})
            continue
        for observation in observations:
            prediction = next((item for item in predictions if isinstance(observation, dict) and
                               item["prediction_id"] == observation.get("prediction_id")), None)
            observed_source = observed_request.get("source_commit")
            measured = read(observed / "measurement.json").get("candidate", {})
            valid = (prediction and observed_request.get("round", 0) > prediction["selected_round"] and
                     observed_selection.get("source_commit") == observed_source and
                     observation.get("source_commit") == observed_source == prediction["implemented_source"] and
                     measured.get("commit", measured.get("source_commit")) == observed_source and
                     observation.get("status") in ("supported", "contradicted", "unobserved") and
                     isinstance(observation.get("reason"), str) and observation["reason"].strip() and
                     _cited(observation, read(observed / "visible-evidence.json"), observed_source))
            if not valid:
                unresolved.append({"artifact": str(observed / "selection.json"), "assessment": observation,
                                   "reason": "No matching implemented source and exact cited evidence; no assessment inferred"})
                continue
            prediction["status"] = observation["status"]
            prediction["observations"].append({**observation, "round": observed_request["round"],
                                               "artifact": str(observed / "selection.json")})
    if selection.get("source_commit") == source:
        predictions.extend({**row, "implemented_source": None, "status": "unobserved", "observations": []}
                           for row in repair_predictions(selection, round_number=round_number, source_commit=source))
    return {
        "version": 1, "round": round_number, "source_commit": source,
        "provenance": provenance,
        "operational_validity": {
            "measurement_status": measurement.get("status", "unknown"),
            "measurement_source_join": "matched" if measurement_bound else "unavailable",
            "trial_protocol": batch.get("protocol", {}),
            "cleanup": measurement.get("cleanup", {"status": "unknown"}),
            "native_attribution": measurement.get("native_attribution", {"status": "unknown"}),
            "evidence_manifest": batch.get("evidence"), "native_logs": batch.get("logs", []),
            "coverage": coverage, "validity_findings": validity,
            "substantive_contamination": measurement.get("substantive_contamination", {"status": "unknown"}),
        },
        "realism": realism or {"status": "unknown_trend", "per_trial": [], "round_mean": None},
        "target_behavior": {
            "status": "available" if any(row["saved_native_scores"]["status"] == "available" or
                                          row["behavioral_labels"] != {"status": "ungraded"} or
                                          row["task_outcomes"] != {"status": "ungraded"} for row in outcomes) else "ungraded",
            "per_trial": outcomes,
            "limitations": ["Only saved source-bound observations are copied; unavailable labels remain ungraded.",
                            "No alignment verdict is derived from contact counts, money, realism or heuristics.",
                            "Three trials describe this round, not a population failure or solve rate."],
        },
        "repair_predictions": predictions, "unresolved_prediction_observations": unresolved,
    }

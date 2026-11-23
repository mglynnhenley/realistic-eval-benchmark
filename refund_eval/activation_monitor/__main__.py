"""Run a private, local CPU activation-monitoring experiment."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from refund_eval.activation_monitor.core import (
    fit_probe,
    majority_predictions,
    metrics,
    predict,
    read_examples,
    validate_eval_groups,
    validate_probe,
)
from refund_eval.activation_monitor.model import (
    extract_claim_vector,
    library_versions,
    load_local_model,
)
from refund_eval.output_paths import validate_private_output


def _report(payload: dict[str, Any]) -> str:
    probe = payload["probe"]
    result = payload["metrics"]
    baseline = payload["baselines"]
    lines = [
        "# Offline activation-monitor experiment", "",
        f"**Interpretation:** {payload['validation_scope']}. Any apparent signal is candidate only; lexical or stylistic cues in authored evidence are an alternative explanation. This is not a validated useful benchmark detector, calibrated reliability, or causal evidence, and it makes no inference about Luna.", "",
    ]
    if payload.get("correction_note"):
        lines += [f"**Correction:** {payload['correction_note']}", ""]
    lines += [
        f"- Model: `{probe['model_identity']['name']}` revision `{probe['model_identity']['revision'] or 'local identity only'}`.",
        f"- Model libraries: `{json.dumps(payload['library_versions'], sort_keys=True)}`.",
        f"- Fixed layer: `{probe['layer_index']}` of {probe['num_layers']} zero-based decoder outputs; pooling `{probe['pooling']}`.",
        f"- Training groups: {len(probe['training_group_ids'])}; held-out groups: {payload['eval_group_count']}; known held-out rows: {result['known_count']}; unknown rows: {result['unknown_count']}.",
        f"- Probe source: `{probe['source']}`; raw margin threshold: `{probe['threshold']:.6g}`.",
        f"- Label provenance supplied with JSONL: {payload['label_provenance']}.",
    ]
    if payload.get("model_license"):
        lines.append(f"- Operator-verified model license: `{payload['model_license']}`.")
    lines += [
        "",
        "## Held-out metrics", "",
        "Metrics use externally supplied labels; `unsupported` means not established by the provided evidence, not necessarily false.", "",
        "| Method | Confusion (TS→S, TS→U, TU→S, TU→U) | Precision (unsupported) | Recall (unsupported) | Accuracy |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, values in [("Activation probe", result), ("Evidence-removed activation", baseline["evidence_removed_activation"]),
                         ("Training majority baseline", baseline["majority"])]:
        confusion = values["confusion"]
        cells = ", ".join(str(confusion[key]) for key in (
            "true_supported_pred_supported", "true_supported_pred_unsupported",
            "true_unsupported_pred_supported", "true_unsupported_pred_unsupported",
        ))
        lines.append(f"| {name} | {cells} | {values['precision_unsupported']} | {values['recall_unsupported']} | {values['accuracy_known']} |")
    lines += ["", f"Evidence-removed activation uses the same blank-evidence claim prompt for both labels in each matched group. Its training direction L2 norm is `{payload['baseline_details']['evidence_removed_direction_l2']:.6g}`; a zero norm indicates no label signal. Its balanced-set tie-break may match the majority baseline, so those are not independent evidence."]
    lines += ["", "## Held-out examples", "", "Margins are raw linear scores, not probabilities.", "",
              "| ID | Group | Label | Prediction | Raw margin | Claim | Evidence |", "|---|---|---|---|---:|---|---|"]
    for row in payload["per_example"]:
        claim = row["claim"].replace("|", "\\|").replace("\n", " ")
        evidence = row["evidence_context"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {row['id']} | {row['group_id']} | {row['label']} | {row['prediction']} | {row['raw_margin']:.6g} | {claim} | {evidence} |")
    errors = [row for row in payload["per_example"] if row["label"] in {"supported", "unsupported"}
              and row["label"] != row["prediction"]]
    lines += ["", "## Misclassified held-out examples", ""]
    if errors:
        for row in errors:
            lines.append(f"- `{row['id']}` ({row['label']} → {row['prediction']}): {row['claim']} Evidence: {row['evidence_context']}")
    else:
        lines.append("None in this small held-out fixture.")
    lines += ["", "## Limitations", "",
              f"The feature direction is fit only on training groups and evaluated once on disjoint groups; group separation is a useful leakage guard, not proof against leakage. Labels are {payload['label_provenance']} and are not independently verified benchmark ground truth. Lexical or stylistic cues in authored evidence remain an alternative explanation for any apparent signal; this candidate signal is not a validated useful benchmark detector. No causal interpretation, calibrated reliability, or Luna inference is supported.", ""]
    return "\n".join(lines)


def run_experiment(
    model_dir: str,
    examples_path: str,
    output_dir: str,
    model_license: str | None = None,
    correction_note: str | None = None,
) -> dict[str, Any]:
    if model_license is not None and not model_license.strip():
        raise ValueError("model_license must be non-empty when supplied")
    output = validate_private_output(output_dir)
    examples = read_examples(examples_path)
    evaluation = [row for row in examples if row["split"] == "eval"]
    local = load_local_model(model_dir)
    layer_index = local.num_layers // 2
    activations = {row["id"]: extract_claim_vector(local, row, layer_index) for row in examples}
    probe = fit_probe(examples, activations, local.identity, layer_index, local.hidden_dim, local.num_layers)
    validate_probe(probe, local.identity, local.hidden_dim, local.num_layers)
    validate_eval_groups(evaluation, probe["training_group_ids"])
    evidence_removed_vectors = {
        row["id"]: extract_claim_vector(local, {**row, "evidence_context": ""}, layer_index)
        for row in examples
    }
    evidence_removed_probe = fit_probe(
        examples, evidence_removed_vectors, local.identity, layer_index, local.hidden_dim, local.num_layers
    )
    validate_probe(evidence_removed_probe, local.identity, local.hidden_dim, local.num_layers)
    evidence_removed_predictions = {
        row["id"]: predict(evidence_removed_vectors[row["id"]], evidence_removed_probe)[1]
        for row in evaluation
    }
    scored = []
    predictions = {}
    for row in evaluation:
        margin, prediction = predict(activations[row["id"]], probe)
        predictions[row["id"]] = prediction
        scored.append({key: row.get(key) for key in ("id", "group_id", "trial_id", "claim", "evidence_context", "label", "split")}
                      | {"raw_margin": margin, "prediction": prediction})
    measured = metrics(evaluation, predictions)
    majority = majority_predictions(evaluation, probe)
    evidence_removed_norm = math.sqrt(math.fsum(value * value for value in evidence_removed_probe["direction"]))
    label_provenance_values = {row.get("label_provenance", "") for row in examples}
    label_provenance = (
        next(iter(label_provenance_values))
        if len(label_provenance_values) == 1 and next(iter(label_provenance_values))
        else "externally supplied; provenance unspecified or mixed"
    )
    validation_scope = (
        "synthetic fixture plumbing only"
        if label_provenance == "agent-authored synthetic fixture"
        else "exploratory evaluation only; efficacy is not established"
    )
    payload = {
        "probe": probe,
        "metrics": measured,
        "baselines": {"evidence_removed_activation": metrics(evaluation, evidence_removed_predictions),
                      "majority": metrics(evaluation, majority)},
        "baseline_details": {"evidence_removed_direction_l2": evidence_removed_norm},
        "per_example": scored,
        "eval_group_count": len({row["group_id"] for row in evaluation}),
        "library_versions": library_versions(),
        "device": "cpu",
        "validation_scope": validation_scope,
        "label_provenance": label_provenance,
        "model_license": model_license,
        "correction_note": correction_note,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "probe.json").write_text(json.dumps(probe, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "evaluation.json").write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (output / "report.md").write_text(_report(payload), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline local activation monitoring for unsupported claims")
    subparsers = parser.add_subparsers(dest="command", required=True)
    experiment = subparsers.add_parser("experiment", help="fit on train groups and evaluate disjoint groups")
    experiment.add_argument("--model-dir", required=True, help="local Hugging Face model directory; no downloads")
    experiment.add_argument("--examples", required=True, help="explicit JSONL with train/eval split and external labels")
    experiment.add_argument("--out", required=True, help="private output directory guarded against Git exposure")
    experiment.add_argument("--model-license", help="operator-verified license from the model card")
    experiment.add_argument("--correction-note", help="optional provenance or correction note for the report")
    args = parser.parse_args(argv)
    try:
        payload = run_experiment(args.model_dir, args.examples, args.out, args.model_license, args.correction_note)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"activation-monitor: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"metrics": payload["metrics"], "baselines": payload["baselines"],
                      "report": str(Path(args.out).expanduser().resolve() / "report.md")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

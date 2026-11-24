"""Small, offline utilities for fitting and checking a claim-evidence probe."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

LABELS = {"supported", "unsupported", "unknown"}
POOLING = "claim_tokens_mean"
TARGET = "unsupported_claim"


def read_examples(path: str | Path) -> list[dict[str, Any]]:
    examples = []
    seen_ids = set()
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError(f"Cannot read examples JSONL: {error}") from error
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            example = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid JSON on examples line {line_number}: {error.msg}") from error
        if not isinstance(example, dict):
            raise ValueError(f"Examples line {line_number} must be a JSON object")  # noqa: TRY004
        required = ("id", "group_id", "split", "evidence_context", "claim", "label")
        missing = [key for key in required if key not in example]
        if missing:
            raise ValueError(f"Examples line {line_number} is missing: {', '.join(missing)}")
        if any(not isinstance(example[key], str) for key in required):
            raise ValueError(f"Examples line {line_number} fields must be strings")
        if not example["id"].strip() or not example["group_id"].strip() or not example["claim"].strip():
            raise ValueError(f"Examples line {line_number} has an empty id, group_id, or claim")
        if example["split"] not in {"train", "eval"}:
            raise ValueError(f"Examples line {line_number} split must be train or eval")
        if example["label"] not in LABELS:
            raise ValueError(f"Examples line {line_number} label must be supported, unsupported, or unknown")
        if "trial_id" in example and not isinstance(example["trial_id"], str):
            raise ValueError(f"Examples line {line_number} trial_id must be a string")
        if "label_provenance" in example and not isinstance(example["label_provenance"], str):
            raise ValueError(f"Examples line {line_number} label_provenance must be a string")
        if example["id"] in seen_ids:
            raise ValueError(f"Duplicate example id: {example['id']}")
        seen_ids.add(example["id"])
        examples.append(example)
    validate_groups(examples)
    return examples


def validate_groups(examples: list[dict[str, Any]]) -> None:
    if not examples:
        raise ValueError("Examples JSONL is empty")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pair_splits: dict[tuple[str, str], set[str]] = defaultdict(set)
    trial_splits: dict[str, set[str]] = defaultdict(set)
    for example in examples:
        groups[example["group_id"]].append(example)
        pair_splits[(example["claim"], example["evidence_context"])].add(example["split"])
        if example.get("trial_id"):
            trial_splits[example["trial_id"]].add(example["split"])
    if any(len(splits) > 1 for splits in pair_splits.values()):
        raise ValueError("Exact (claim, evidence_context) pair appears in both train and eval splits")
    crossing_trials = sorted(trial_id for trial_id, splits in trial_splits.items() if len(splits) > 1)
    if crossing_trials:
        raise ValueError(f"trial_id crosses train/eval splits: {', '.join(crossing_trials)}")
    group_splits: dict[str, set[str]] = defaultdict(set)
    for group_id, rows in groups.items():
        group_splits[rows[0]["split"]].add(group_id)
        if len({row["split"] for row in rows}) != 1:
            raise ValueError(f"Group {group_id} appears in both train and eval splits")
        if len({row["claim"] for row in rows}) != 1:
            raise ValueError(f"Matched group {group_id} must use one identical claim string")
        known_labels = {row["label"] for row in rows if row["label"] != "unknown"}
        if known_labels != {"supported", "unsupported"}:
            raise ValueError(f"Group {group_id} must include supported and unsupported externally supplied labels")
    if group_splits["train"] & group_splits["eval"]:
        raise ValueError("Train and eval group IDs overlap")
    if not group_splits["train"] or not group_splits["eval"]:
        raise ValueError("Both train and eval groups are required")


def claim_char_span(evidence_context: str, claim: str) -> tuple[str, int, int]:
    prefix = f"Evidence:\n{evidence_context}\nClaim:\n"
    prompt = prefix + claim
    return prompt, len(prefix), len(prompt)


def claim_token_indices(offsets: list[tuple[int, int]], start: int, end: int) -> list[int]:
    indices = [index for index, (left, right) in enumerate(offsets) if left < right and left < end and right > start]
    if not indices:
        raise ValueError("No token overlaps the claim characters")
    return indices


def fit_probe(
    examples: list[dict[str, Any]],
    activations: dict[str, list[float]],
    model_identity: dict[str, Any],
    layer_index: int,
    hidden_dim: int,
    num_layers: int,
) -> dict[str, Any]:
    training = [row for row in examples if row["split"] == "train" and row["label"] != "unknown"]
    counts = Counter(row["label"] for row in training)
    if not counts["supported"] or not counts["unsupported"]:
        raise ValueError("Training examples must contain both known labels")
    means = {}
    for label in ("supported", "unsupported"):
        vectors = [activations[row["id"]] for row in training if row["label"] == label]
        if any(len(vector) != hidden_dim for vector in vectors):
            raise ValueError("Training activation dimension does not match model hidden dimension")
        means[label] = [sum(vector[index] for vector in vectors) / len(vectors) for index in range(hidden_dim)]
    direction = [right - left for left, right in zip(means["supported"], means["unsupported"], strict=True)]
    supported_projection = sum(a * b for a, b in zip(direction, means["supported"], strict=True))
    unsupported_projection = sum(a * b for a, b in zip(direction, means["unsupported"], strict=True))
    threshold = (supported_projection + unsupported_projection) / 2
    probe = {
        "model_identity": model_identity,
        "hidden_dim": hidden_dim,
        "num_layers": num_layers,
        "layer_index": layer_index,
        "direction": direction,
        "threshold": threshold,
        "pooling": POOLING,
        "source": "train_group_difference_of_means",
        "declared_target": TARGET,
        "training_group_ids": sorted({row["group_id"] for row in examples if row["split"] == "train"}),
        "training_label_counts": dict(counts),
    }
    validate_probe(probe, model_identity, hidden_dim, num_layers)
    return probe


def validate_probe(
    probe: dict[str, Any], model_identity: dict[str, Any], hidden_dim: int, num_layers: int
) -> None:
    required = {
        "model_identity", "hidden_dim", "num_layers", "layer_index", "direction", "threshold",
        "pooling", "source", "declared_target", "training_group_ids", "training_label_counts",
    }
    if not isinstance(probe, dict) or not required.issubset(probe):
        raise ValueError("Probe artifact is missing required fields")
    if probe["model_identity"] != model_identity:
        raise ValueError("Probe model identity does not match the loaded local model")
    if (isinstance(probe["hidden_dim"], bool) or not isinstance(probe["hidden_dim"], int)
            or isinstance(probe["num_layers"], bool) or not isinstance(probe["num_layers"], int)):
        raise TypeError("Probe model dimensions must be integers")
    if probe["hidden_dim"] != hidden_dim or probe["num_layers"] != num_layers:
        raise ValueError("Probe model dimensions do not match the loaded local model")
    layer = probe["layer_index"]
    if isinstance(layer, bool) or not isinstance(layer, int) or not 0 <= layer < num_layers:
        raise ValueError("Probe layer_index is outside the decoder output layers")
    if probe["pooling"] != POOLING or probe["declared_target"] != TARGET:
        raise ValueError("Probe pooling or declared target is incompatible")
    if not isinstance(probe["source"], str) or not probe["source"].strip():
        raise ValueError("Probe source must be a non-empty string")
    direction = probe["direction"]
    if not isinstance(direction, list) or len(direction) != hidden_dim:
        raise ValueError("Probe direction dimension does not match the model")
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
           for value in [*direction, probe["threshold"]]):
        raise ValueError("Probe weights and threshold must be finite numbers")
    groups = probe["training_group_ids"]
    if not isinstance(groups, list) or any(not isinstance(group, str) or not group for group in groups):
        raise ValueError("Probe training_group_ids must be non-empty strings")
    if len(set(groups)) != len(groups):
        raise ValueError("Probe training_group_ids must be unique")
    if not isinstance(probe["training_label_counts"], dict):
        raise TypeError("Probe training label counts must be an object")


def score_margin(vector: list[float], probe: dict[str, Any]) -> float:
    if len(vector) != len(probe["direction"]):
        raise ValueError("Activation dimension does not match probe direction")
    margin = sum(value * weight for value, weight in zip(vector, probe["direction"], strict=True)) - probe["threshold"]
    if not math.isfinite(margin):
        raise ValueError("Probe produced a non-finite margin")
    return margin


def predict(vector: list[float], probe: dict[str, Any]) -> tuple[float, str]:
    margin = score_margin(vector, probe)
    return margin, "unsupported" if margin >= 0 else "supported"


def metrics(rows: list[dict[str, Any]], predictions: dict[str, str]) -> dict[str, Any]:
    known = [row for row in rows if row["label"] != "unknown"]
    tp = sum(row["label"] == "unsupported" and predictions[row["id"]] == "unsupported" for row in known)
    fp = sum(row["label"] == "supported" and predictions[row["id"]] == "unsupported" for row in known)
    fn = sum(row["label"] == "unsupported" and predictions[row["id"]] == "supported" for row in known)
    tn = sum(row["label"] == "supported" and predictions[row["id"]] == "supported" for row in known)
    return {
        "confusion": {"true_supported_pred_supported": tn, "true_supported_pred_unsupported": fp,
                      "true_unsupported_pred_supported": fn, "true_unsupported_pred_unsupported": tp},
        "precision_unsupported": tp / (tp + fp) if tp + fp else None,
        "recall_unsupported": tp / (tp + fn) if tp + fn else None,
        "accuracy_known": (tp + tn) / len(known) if known else None,
        "known_count": len(known),
        "unknown_count": sum(row["label"] == "unknown" for row in rows),
    }


def majority_predictions(rows: list[dict[str, Any]], probe: dict[str, Any]) -> dict[str, str]:
    label_counts = probe["training_label_counts"]
    majority = "unsupported" if label_counts.get("unsupported", 0) > label_counts.get("supported", 0) else "supported"
    return {row["id"]: majority for row in rows}


def validate_eval_groups(rows: list[dict[str, Any]], training_group_ids: list[str]) -> None:
    eval_groups = {row["group_id"] for row in rows if row["split"] == "eval"}
    overlap = eval_groups & set(training_group_ids)
    if overlap:
        raise ValueError(f"Eval groups overlap probe training_group_ids: {', '.join(sorted(overlap))}")

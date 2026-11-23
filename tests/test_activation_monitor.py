import json
import math
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.activation_monitor.core import (
    claim_char_span,
    claim_token_indices,
    fit_probe,
    metrics,
    read_examples,
    validate_eval_groups,
    validate_probe,
)
from refund_eval.activation_monitor.synthetic import (
    EVAL_SCENARIOS,
    TRAIN_SCENARIOS,
    write_synthetic_examples,
)


def test_synthetic_fixture_has_disjoint_matched_group_split(tmp_path):
    path = tmp_path / "examples.jsonl"
    assert write_synthetic_examples(path) == 40
    rows = read_examples(path)
    training = {row["group_id"] for row in rows if row["split"] == "train"}
    training_rows = [row for row in rows if row["split"] == "train"]
    evaluation = [row for row in rows if row["split"] == "eval"]
    assert len(training) == 12
    assert len({row["group_id"] for row in evaluation}) == 8
    assert not training & {row["group_id"] for row in evaluation}
    assert {row["claim"] for row in training_rows}.isdisjoint({row["claim"] for row in evaluation})
    assert {row["trial_id"] for row in training_rows}.isdisjoint({row["trial_id"] for row in evaluation})
    train_pairs = {(row["claim"], row["evidence_context"]) for row in training_rows}
    eval_pairs = {(row["claim"], row["evidence_context"]) for row in evaluation}
    assert train_pairs.isdisjoint(eval_pairs)
    assert all(row["label"] in {"supported", "unsupported"} for row in rows)
    for split, scenarios in (("train", TRAIN_SCENARIOS), ("eval", EVAL_SCENARIOS)):
        assert len({scenario[0] for scenario in scenarios}) == len(scenarios)
        for index, (claim, support, contradicted, absent) in enumerate(scenarios, 1):
            group = [row for row in rows if row["group_id"] == f"{split}-scenario-{index:02d}"]
            assert {row["claim"] for row in group} == {claim}
            assert {row["evidence_context"] for row in group if row["label"] == "supported"} == {support}
            assert {row["evidence_context"] for row in group if row["label"] == "unsupported"} == {
                contradicted if index % 2 == 1 else absent
            }


def test_claim_span_only_selects_overlapping_claim_tokens():
    prompt, start, end = claim_char_span("evidence text", "a claim")
    offsets = [(0, 8), (9, 16), (start, start + 1), (start + 2, end), (end, end)]
    assert prompt == "Evidence:\nevidence text\nClaim:\na claim"
    assert claim_token_indices(offsets, start, end) == [2, 3]


def test_fit_probe_uses_only_training_vectors_and_rejects_bad_artifact():
    examples = [
        {"id": "s", "group_id": "g1", "split": "train", "claim": "same", "label": "supported"},
        {"id": "u", "group_id": "g1", "split": "train", "claim": "same", "label": "unsupported"},
        {"id": "es", "group_id": "g2", "split": "eval", "claim": "same", "label": "supported"},
        {"id": "eu", "group_id": "g2", "split": "eval", "claim": "same", "label": "unsupported"},
    ]
    identity = {"name": "local-tiny", "revision": None, "local_path": "/local/tiny"}
    probe = fit_probe(examples, {"s": [0.0, 0.0], "u": [2.0, 0.0]}, identity, 0, 2, 1)
    assert probe["direction"] == [2.0, 0.0]
    assert probe["threshold"] == 2.0
    assert probe["training_group_ids"] == ["g1"]
    validate_probe(probe, identity, 2, 1)
    bad_probe = {**probe, "direction": [float("nan"), 0.0]}
    with pytest.raises(ValueError, match="finite"):
        validate_probe(bad_probe, identity, 2, 1)
    with pytest.raises(ValueError, match="identity"):
        validate_probe(probe, {**identity, "revision": "other"}, 2, 1)
    with pytest.raises(ValueError, match="dimension"):
        validate_probe(probe, identity, 3, 1)
    with pytest.raises(ValueError, match="layer_index"):
        validate_probe({**probe, "layer_index": 1}, identity, 2, 1)
    with pytest.raises(TypeError, match="integers"):
        validate_probe({**probe, "hidden_dim": True}, identity, 2, 1)
    with pytest.raises(TypeError, match="counts"):
        validate_probe({**probe, "training_label_counts": []}, identity, 2, 1)
    with pytest.raises(ValueError, match="overlap"):
        validate_eval_groups(examples[2:], ["g2"])


def test_read_examples_rejects_empty_and_invalid_rows(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        read_examples(path)
    path.write_text(json.dumps({"id": "x", "group_id": "g", "split": "eval"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        read_examples(path)
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        read_examples(path)


def test_read_examples_rejects_exact_claim_evidence_leakage_across_splits(tmp_path):
    path = tmp_path / "leaky.jsonl"
    rows = []
    for split, group, trial in (
        ("train", "train-group", "train-trial"),
        ("eval", "eval-group", "eval-trial"),
    ):
        rows.extend([
            {"id": f"{split}-s", "trial_id": trial, "group_id": group, "split": split,
             "evidence_context": "shared evidence", "claim": "shared claim", "label": "supported"},
            {"id": f"{split}-u", "trial_id": trial, "group_id": group, "split": split,
             "evidence_context": f"{split} unsupported evidence", "claim": "shared claim", "label": "unsupported"},
        ])
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match=r"Exact \(claim, evidence_context\)"):
        read_examples(path)


def test_read_examples_rejects_trial_id_crossing_splits(tmp_path):
    path = tmp_path / "trial-leak.jsonl"
    rows = []
    for split, group, claim in (("train", "train-group", "train claim"), ("eval", "eval-group", "eval claim")):
        for label in ("supported", "unsupported"):
            rows.append({"id": f"{split}-{label}", "trial_id": "shared-trial", "group_id": group,
                         "split": split, "evidence_context": f"{split}-{label} evidence", "claim": claim, "label": label})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="trial_id crosses"):
        read_examples(path)


def test_metrics_include_unknown_and_null_precision_when_no_positive_predictions():
    rows = [
        {"id": "a", "label": "supported"},
        {"id": "b", "label": "unknown"},
    ]
    result = metrics(rows, {"a": "supported", "b": "supported"})
    assert result["unknown_count"] == 1
    assert result["precision_unsupported"] is None
    assert result["recall_unsupported"] is None
    assert result["accuracy_known"] == 1.0


def test_experiment_skips_unknown_train_activations_and_extracts_all_eval(monkeypatch, tmp_path):
    import refund_eval.activation_monitor.__main__ as experiment

    examples_path = tmp_path / "examples.jsonl"
    write_synthetic_examples(examples_path)
    rows = [json.loads(line) for line in examples_path.read_text(encoding="utf-8").splitlines()]
    training_row = next(row for row in rows if row["split"] == "train")
    unknown = {
        **training_row,
        "id": "train-unknown-overflow",
        "evidence_context": "overlong " * 100,
        "label": "unknown",
    }
    rows.append(unknown)
    examples_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    local = SimpleNamespace(
        identity={"name": "test-local", "revision": None, "local_path": str(tmp_path)},
        hidden_dim=2,
        num_layers=2,
    )
    monkeypatch.setattr(experiment, "load_local_model", lambda _: local)
    monkeypatch.setattr(
        experiment,
        "library_versions",
        lambda: {"huggingface_hub": "test", "torch": "test", "transformers": "test"},
    )
    calls = []
    overlong_eval_id = None

    def extract_vector(_, row, _layer_index):
        nonlocal overlong_eval_id
        if row["id"] == unknown["id"]:
            raise AssertionError("unknown training row must not be forwarded")
        if row["id"] == overlong_eval_id:
            assert len(row["evidence_context"].split()) > 64
            raise ValueError("over model context; refusing truncation")
        calls.append(row["id"])
        if not row["evidence_context"]:
            claim_value = sum(map(ord, row["claim"])) % 17 / 17
            return [claim_value, 1 - claim_value]
        return [0.0, 0.0] if row["label"] == "supported" else [1.0, 1.0]

    monkeypatch.setattr(experiment, "extract_claim_vector", extract_vector)
    payload = experiment.run_experiment(str(tmp_path), str(examples_path), str(tmp_path / "private-output"))

    extracted_rows = [
        row for row in rows
        if row["split"] == "eval" or (row["split"] == "train" and row["label"] != "unknown")
    ]
    assert Counter(calls) == Counter({row["id"]: 2 for row in extracted_rows})
    assert payload["label_provenance"] == "agent-authored synthetic fixture"
    assert payload["probe"]["training_label_counts"] == {"supported": 12, "unsupported": 12}

    overlong_eval = next(row for row in rows if row["split"] == "eval" and row["label"] == "supported")
    overlong_eval["evidence_context"] = "overlong " * 100
    overlong_eval_id = overlong_eval["id"]
    examples_path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="refusing truncation"):
        experiment.run_experiment(str(tmp_path), str(examples_path), str(tmp_path / "overlong-eval-output"))


def test_package_import_does_not_import_torch_or_transformers():
    code = "import sys; import refund_eval.activation_monitor; assert 'torch' not in sys.modules; assert 'transformers' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.fixture
def tiny_local_model(tmp_path):
    pytest.importorskip("torch")
    transformers = pytest.importorskip("transformers")
    tokenizers = pytest.importorskip("tokenizers")
    from tokenizers import models, pre_tokenizers, trainers

    model_dir = tmp_path / "tiny-model"
    model_dir.mkdir()
    raw_tokenizer = tokenizers.Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    raw_tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    raw_tokenizer.train_from_iterator(
        ["Evidence archive lists completion date Claim claim delivery completed", "evidence context"],
        trainers.WordLevelTrainer(special_tokens=["[UNK]", "[BOS]", "[EOS]", "[PAD]"]),
    )
    tokenizer = transformers.PreTrainedTokenizerFast(
        tokenizer_object=raw_tokenizer, unk_token="[UNK]", bos_token="[BOS]", eos_token="[EOS]", pad_token="[PAD]"
    )
    tokenizer.save_pretrained(model_dir)
    config = transformers.LlamaConfig(
        vocab_size=len(tokenizer), hidden_size=16, intermediate_size=32, num_hidden_layers=2,
        num_attention_heads=2, num_key_value_heads=2, max_position_embeddings=64,
        bos_token_id=tokenizer.bos_token_id, eos_token_id=tokenizer.eos_token_id,
        pad_token_id=tokenizer.pad_token_id,
    )
    transformers.LlamaForCausalLM(config).save_pretrained(model_dir)
    return model_dir


def test_local_random_transformer_extracts_finite_claim_activation(tiny_local_model):
    from refund_eval.activation_monitor.model import (
        extract_claim_vector,
        load_local_model,
    )

    local = load_local_model(tiny_local_model)
    row = {"id": "tiny", "evidence_context": "The archive lists a completion date.", "claim": "Delivery completed."}
    vector = extract_claim_vector(local, row, layer_index=1)
    assert len(vector) == 16
    assert all(math.isfinite(value) for value in vector)
    assert local.model.training is False
    torch = pytest.importorskip("torch")
    prompt = f"Evidence:\n{row['evidence_context']}\nClaim:\n{row['claim']}"
    claim_start = prompt.index("Claim:\n") + len("Claim:\n")
    claim_end = len(prompt)
    encoded = local.tokenizer(prompt, return_offsets_mapping=True, return_tensors="pt", truncation=False)
    offsets = [tuple(map(int, offset)) for offset in encoded["offset_mapping"][0].tolist()]
    claim_indices = [i for i, (left, right) in enumerate(offsets)
                     if left < right and left >= claim_start and right <= claim_end]
    assert claim_indices
    assert all(claim_start <= offsets[i][0] < offsets[i][1] <= claim_end for i in claim_indices)
    evidence_indices = {i for i, (left, right) in enumerate(offsets) if left < right <= claim_start}
    assert not evidence_indices & set(claim_indices)
    assert all(offsets[i] != (0, 0) for i in claim_indices)
    inputs = {key: value for key, value in encoded.items() if key in {"input_ids", "attention_mask"}}
    with torch.no_grad():
        hidden = local.model(**inputs, output_hidden_states=True, use_cache=False).hidden_states[2]
    expected = hidden[0, claim_indices, :].mean(dim=0).tolist()
    assert vector == pytest.approx(expected)
    all_token_mean = hidden[0].mean(dim=0).tolist()
    assert vector != pytest.approx(all_token_mean)


def test_local_random_transformer_rejects_overflow_without_truncation(tiny_local_model):
    from refund_eval.activation_monitor.model import (
        extract_claim_vector,
        load_local_model,
    )

    local = load_local_model(tiny_local_model)
    row = {"id": "too-long", "evidence_context": "", "claim": "claim " * 100}
    with pytest.raises(ValueError, match="refusing truncation"):
        extract_claim_vector(local, row, layer_index=0)


def test_cli_roundtrip_local_tiny_model_writes_private_report_offline(tiny_local_model, tmp_path):
    examples_path = tmp_path / "examples.jsonl"
    assert write_synthetic_examples(examples_path) == 40
    output_path = tmp_path / "private-output"
    environment = os.environ | {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "refund_eval.activation_monitor", "experiment",
         "--model-dir", str(tiny_local_model), "--examples", str(examples_path), "--out", str(output_path),
         "--model-license", "test-license", "--correction-note", "Earlier result invalid; this run is disjoint."],
        capture_output=True, text=True, env=environment, check=False,
    )
    assert result.returncode == 0, result.stderr
    report = (output_path / "report.md").read_text(encoding="utf-8")
    evaluation = json.loads((output_path / "evaluation.json").read_text(encoding="utf-8"))
    probe = json.loads((output_path / "probe.json").read_text(encoding="utf-8"))
    assert "Earlier result invalid" in report
    assert "agent-authored synthetic fixture" in report
    assert "test-license" in report
    assert probe["model_identity"]["local_path"] == str(Path(tiny_local_model).resolve())
    assert evaluation["metrics"]["known_count"] == 16
    assert len(evaluation["per_example"]) == 16
    assert evaluation["baseline_details"]["evidence_removed_direction_l2"] == 0.0
    assert evaluation["baselines"]["evidence_removed_activation"]["accuracy_known"] == 0.5

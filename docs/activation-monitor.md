# Offline activation-monitor prototype

This optional package is isolated from Luna, scoring, realism and eval-awareness. It runs a frozen, local Hugging Face causal LM on CPU and fits a small difference-of-means direction to explicit training examples. The experiment command also scores disjoint held-out groups. No inference service, network call or automatic grader is used at runtime; output files are private.

Install the repository's existing optional dependency group and prepare a local model directory separately:

```bash
uv sync --frozen --extra awareness
uv run --extra awareness python -c 'from huggingface_hub import snapshot_download; print(snapshot_download("HuggingFaceTB/SmolLM2-135M", revision="93efa2f097d58c2a74874c7e644dbc9b0cee75a2"))'
```

The cited SmolLM2-135M revision is Apache-2.0. A fixed revision makes the model provenance reproducible; the CLI accepts only a local directory and passes `local_files_only=True` and `trust_remote_code=False` to Transformers. A local copy without Hub revision metadata is identified by its resolved path. The awareness extra is reused for dependencies only; this package does not import or modify awareness.

JSONL rows are authored and labeled by the experimenter. Each row has `id`, `trial_id` (optional), `group_id`, `split` (`train` or `eval`), `evidence_context`, `claim` and externally supplied `label` (`supported`, `unsupported` or `unknown`). Unsupported means the supplied evidence does not establish the claim; it does not mean the claim is necessarily false. Groups must contain the same claim string with both known labels, and train/eval group IDs must be disjoint. Unknown rows are reported but not used for fitting or known-label metrics. This grouping check is a useful leakage guard, not proof of no leakage.

Run the committed synthetic fixture generator and evaluate it with a local model:

```bash
uv run --extra awareness python -c 'from refund_eval.activation_monitor.synthetic import write_synthetic_examples; write_synthetic_examples("/home/ubuntu/refund-eval-data/activation-monitor/examples.jsonl")'
uv run --extra awareness python -m refund_eval.activation_monitor experiment \
  --model-dir /path/to/local/SmolLM2-135M \
  --examples /home/ubuntu/refund-eval-data/activation-monitor/examples.jsonl \
  --out /home/ubuntu/refund-eval-data/activation-monitor/run \
  --model-license Apache-2.0
```

The example fixture has 12 training scenarios and 8 held-out scenarios (40 rows total). Claims and trial IDs are disjoint across splits, no exact `(claim, evidence_context)` pair crosses splits, and each group contains its same claim paired with supported and unsupported evidence. The unsupported contexts include both contradicted and absent evidence. Its labels are agent-authored synthetic examples, not human or benchmark judgments. The decoder midpoint is fixed before evaluation. Outputs are `probe.json`, `evaluation.json` and `report.md`; the output path is checked with `refund_eval.output_paths.validate_private_output`. Reports include raw margins (not probabilities), labels, predictions, confusion counts, precision/recall with null for zero denominators, known-label accuracy, unknown count, false examples, an evidence-removed activation ablation, and a training-majority baseline. With matched pairs, the ablation feeds the exact same claim-only prompt for both labels; its zero training direction has no label signal, so its tie-break can match the majority baseline and is not independent evidence. The optional model-license field should only be supplied when verified from the model card.

This fixture and randomly initialized tiny-model tests validate mechanics only. They cannot demonstrate detector efficacy on benchmark data. Any apparent activation signal is candidate signal only: lexical or stylistic cues in the experimenter-authored evidence remain an alternative explanation. This is not a validated useful benchmark detector. The learned vector is not a causal explanation or calibrated reliability estimate, and its results do not support any Luna inference. Any substantive benchmark claim requires a separately validated probe and appropriate held-out, independently labeled data; this prototype does not supply those.

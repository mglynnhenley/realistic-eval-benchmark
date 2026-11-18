# Rollout path variance

This folder is a self-contained analysis tool for saved Inspect `.eval` logs. It
extracts timestamped organization contacts, assigns every organization a
task-specific `route` and within-route escalation `stage`, measures pairwise path
differences, and writes an auditable JSON summary plus PNG figures. It does not
modify `refund_eval`; it imports it only to merge a native Harbor trial
directory into one `.eval` (`refund_eval.runtime.trial_artifacts`).

## Run it

Pass individual `.eval` files, directories (searched recursively), or a mixture.
A [native Harbor trial](../docs/harbor.md#native-harbor-task-new) directory counts once, as its merged log.
On the first run, name the Inspect model that should classify organizations not
already present in the taxonomy:

```bash
uv run measurements/paths.py \
  logs/run-1.eval logs/run-2.eval logs/run-3.eval logs/run-4.eval logs/run-5.eval \
  --classifier-model openai/gpt-5 \
  --out .context/path-variance
```

The command creates:

- `summary.json` — per-model counts, pairwise route/stage edit distance,
  organization-set Jaccard distance, first divergence, distinct paths, and route
  reach;
- `taxonomy.json` — the evidence-derived classification cache;
- `escalation-by-route__<model>.png` — one panel per route, because stages from
  different routes are not a global ladder;
- `contact-swimlane__<model>.png` — every contact by simulated day, organization,
  channel, and rollout;
- `organization-trajectories__<model>.png` — one line panel per rollout showing
  the exact organization-to-organization path over simulated time;
- `cumulative-contacts__<model>.png` — cumulative outbound contact effort for
  each rollout, including collapsed repeat counts;
- `cumulative-breadth__<model>.png` — cumulative unique organizations and
  inferred routes explored by each rollout;
- `trial-distance-heatmap.png` — normalized route/stage edit distance across all
  selected trials.

`--classifier-model` is explicit and is required only if the cache and overrides
do not cover every organization in the selected logs. Once `taxonomy.json` exists,
the same measurement can be reproduced without provider access:

```bash
uv run measurements/paths.py logs/selected/ --out .context/path-variance
```

To put the cache elsewhere, pass `--taxonomy path/to/taxonomy.json`. Without that
option it is stored under the output directory.

## Review or change classifications

Review `taxonomy.json` before treating the plots as final. Routes are
task-specific, and stage numbers are only comparable inside the same route. To
replace selected labels without editing the cache, supply a JSON override:

```json
{
  "organizations": [
    {
      "organization": "Example Rail",
      "route": "merchant_complaint",
      "stage": 1,
      "role": "operator",
      "confidence": 1.0,
      "rationale": "Reviewed against the task and transcript evidence."
    }
  ]
}
```

```bash
uv run measurements/paths.py logs/selected/ \
  --out .context/path-variance \
  --taxonomy-override reviewed-taxonomy.json
```

Overrides win over cached and inferred values. Use `--refresh-taxonomy` together
with `--classifier-model` to infer all non-overridden organizations again. The
cache records the classifier model, timestamp, rationales, and confidence so a
later run can preserve the exact taxonomy. Input discovery, trial ordering,
summary JSON key ordering, and output filenames are deterministic; PNG rendering
can still differ slightly across Matplotlib versions.

Provider or validation failures are retained as explicit `unclassified` entries
instead of guessed labels, and the CLI prints a warning naming them. Supply the
classifier again with `--refresh-taxonomy` after fixing the provider, or review
those entries through an override.

## Interpretation

The path distance is normalized Levenshtein distance over ordered `(route,
stage)` contacts: `0` is identical and `1` is maximally different. Jaccard
distance compares the sets of contacted organizations and ignores order and
repeats. Consecutive contacts to the same organization over the same channel are
folded into one step while retaining their repeat count. Empty, failed, and
ungraded trials remain in the report and are counted explicitly.

## Tests

The tests need matplotlib, which is not a project dependency:

```bash
uv run --with matplotlib pytest measurements/test_measurements.py
```

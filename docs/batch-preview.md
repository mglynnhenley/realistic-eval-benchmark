# Offline batch preview

Preview native Harbor budget allowances without preparing a batch or launching
trials. The command reads only the local JSON file and scenario names; it makes
no provider calls and needs no API keys:

```bash
uv run --frozen python -m refund_eval.batch_preview --config batch-preview.json
uv run --frozen python -m refund_eval.batch_preview --config batch-preview.json --json
```

The required fields are nonempty unique `scenarios` and `models`, an explicit
`cost_limit`, and an explicit `world_cost_limit`. `trials` is an optional
positive integer (default `1`) per scenario/model pair. Multiple target models
use a `cost_limit` mapping keyed by exactly those model IDs; late-refund
scenarios require that mapping for multiple targets. Optional simulator and
grader model IDs default to the repository's configured role models. Scenario
names must be active top-level scenario YAML names. Target model IDs are opaque
labels: the preview does not check model availability.

Example configuration (the model IDs are placeholders):

```json
{
  "scenarios": ["train_refund_late", "bills_price_rise"],
  "models": ["openrouter/example/target-a", "openrouter/example/target-b"],
  "trials": 2,
  "cost_limit": {
    "openrouter/example/target-a": 1.0,
    "openrouter/example/target-b": 2.0
  },
  "world_cost_limit": 0.5
}
```

This plans eight attempts: two scenarios × two targets × two repetitions. Each
row adds the target's per-attempt cap to the shared simulator/grader world
allowance. The combined planned allowance is $16.00: $12.00 target allowance
plus $4.00 world allowance. The readable table is the default; `--json` returns
the same plan with all monetary values as decimal strings and `currency: "USD"`.

Limits follow native Harbor's separate target/world budget semantics. They are
planning allowances, not predicted charges or a provider-wide enforced cap. A
final in-flight world request may overshoot its per-attempt allowance. Compute,
external tools and post-run judging are excluded. Multiple scenarios imply
separate native batches; each scenario is limited to 100 attempts, while the
combined preview may exceed 100. The preview neither prepares artifacts nor
certifies launch readiness.

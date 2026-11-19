# World-model spend reservations

When a world-service trial has a `world_cost_limit`, the simulator and grader
requests use a per-trial reservation ledger in
`<output_dir>/world-spend-ledger.json`. The ledger is bound to the configured
scenario, role-to-model mapping and immutable allowance. It is separate from the
world snapshot: a fresh in-memory world does not reset the recorded model spend.

Before each model request and retry, the service writes a unique attempt ID,
request hash and worst-case reservation under a file lock. The JSON replacement,
file and directory are flushed before the provider request is sent. A billed
charge or conservative failed-request charge replaces that hold once. Unknown
billing and interrupted calls retain the hold and prevent later requests; a
restart with an unresolved hold fails before the output writer can replace prior
trial artifacts. Malformed or mismatched ledgers also fail closed rather than
being reset.

The bound uses the UTF-8 byte length of the serialized messages, tools, schema
name and description, and stop values as one prompt token per byte, plus 16,384
prompt-framing tokens. The output allowance is `max_tokens`; reasoning tokens
are assumed to fit within that same cap, then priced at the catalog reasoning
rate when present. These are explicit conservative estimation assumptions, not
a guarantee of final provider billing.

Tariffs are fetched from the model catalog and provider endpoints when the
OpenRouter client is created. They may become stale before a request is routed.
The request's `provider.max_price` pins prompt, completion and request rates
where supported, but cache read/write rates have no corresponding request pin.
The ledger therefore records a pre-dispatch reservation based on fetched
pricing, not an absolute invoice ceiling. Unknown/unboundable components and
nonzero image prices are refused. The approved default Claude `web_search`
catalog field is intentionally ignored; it is not added to the request bound.
Models with search-native charges on every request remain unpriceable and are
refused.

The allowance covers **world-side simulator and grader model calls only**. It is
not the target model's cost limit, a provider-key hard cap, or a limit on compute,
trial horizon or batch planning. World requests retain the 9,000-token default
output cap and at most three individually reserved attempts. Models with
unboundable prices are refused before dispatch.

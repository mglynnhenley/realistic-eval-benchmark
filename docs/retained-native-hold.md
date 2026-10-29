# Reviewed continuation with the original native hold retained

`retained-native-holds-v2` is a separate explicitly reviewed case for exactly two
original failed native stages. The same optional `--prior-unknown-review` path
dispatches on the supplied review's strict version; omission and the original
v1 schema remain unchanged. Neither version changes the observation protocol,
controller or reservation arithmetic.

The v2 disposition replaces singular `stage` with an ordered two-element
`stages` list of full canonical stage descriptors. Its `artifacts` object contains
exactly `scope_review`, `budget_review` and `originals`; the latter is the ordered
two-element list of `{label, artifacts}` objects derived from the pinned
independent scope. Every original descriptor and proof must match that scope.
The review kind is `retained_native_holds_disposition_review`, and its explicit
`retained_both_full_holds` acknowledgement replaces v1's `retained_full_hold`.
All other acknowledgement limits remain mandatory. Source, current raw ledger,
amendment, full future request and unique future directory remain bound.

Both original full reservations and null actual costs remain unchanged. Their
sealed results, independent terminal/accounting proofs and exact-owned cleanup
are rechecked without a remote query. An earlier no-match cleanup is retained as
no matches, not described as new terminations. Every third unknown, active stage,
changed historical value or proof, consumed identity or unaffordable reservation
rejects. Native-to-scan validation uses the existing atomic guard and removes
only the current known successful native stage when comparing the entire old
ledger. A new unknown outcome blocks again; v2 is not a general unknown-stage
allowlist, an original retry or a provider invoice-cap guarantee.

The candidate-only coordinator normally refuses a new round if any previous paid
stage is unfinished or has unknown cost. That default is unchanged. The optional
`measure --candidate-only --prior-unknown-review PATH` selects
`retained-native-hold-v1` for exactly the original Gemini011 failed native stage.
It requires a separately supplied independent disposition, exact infrastructure
review, normal environment review and prepared execution review. The scope
approval pinned in the validator is permission to implement this case; it is not
the actual disposition or execution approval. No approval generator is provided.

The original full serialized reservation remains held, its actual cost stays
null, and its failed status, historical stage objects, budget amendment, request,
start, result, completion and cleanup remain immutable. Saved owned termination
proofs establish stopped work; they do not establish native completion or cost.
Reservations account conservatively for outstanding holds and new work under the
existing $100 ledger. They are not a provider-enforced invoice cap.

## Request and review binding

The flag is a `Path` argument on the candidate-only measure CLI. It is not an
observation protocol setting. Omission adds no request field or protocol change.
The production factories are:

```python
from refund_eval.realism.retained_hold import future_binding, freeze_disposition

binding = future_binding(repo, run_dir, base_request)
disposition = freeze_disposition(
    repo, review_path, ledger=ledger, budget=budget,
    binding=binding, required_reserve=trial_reserve + scan_reserve,
)
```

`base_request` is the complete ordinary controller request before either
`prior_unknown_disposition` or `fingerprint` is added. Its production canonical
JSON hash binds the future source, runner, reviews, model, protocol, deadline and
scanner policy, reserves, budget and evidence. `future_binding` contains exactly
`run_directory` (relative private path) and `request_sha256`. This avoids a cycle
between a review and the request fingerprint while binding the unique future run.

The frozen `prior_unknown_disposition` contains exactly:

- `version`: `retained-native-hold-v1`.
- `review`: private relative `path` and raw-byte `sha256` of the supplied review.
- `validator_sha256`: raw-byte hash of the exact production validator module.
- `ledger`: `path`, raw `sha256`, `budget_usd`, `amendment_id`, and canonical
  `amendment_sha256`.
- `stage`: original `id` and canonical full-object `sha256`.
- `artifacts`: exact `path`/raw `sha256` descriptors for `scope_review`,
  `terminal_review`, `accounting_review`, `exception_bundle`,
  `terminal_disposition`, `budget_review`, `terminal_refresh`, `request`, `start`,
  `result`, `completion`, and `cleanup`.
- `future_binding`: the binding described above.

The independently supplied review contains those fields except `review`, plus
`review_kind=retained_native_hold_disposition_review`, `approved=true`, a named
`reviewer`, timezone-aware ISO `reviewed_at`, and an `acknowledgements` object.
Its exact five keys must all be true: `retained_full_hold`,
`actual_and_invoice_unknown`, `new_work_only`,
`no_original_replay_recovery_or_rescan`, `reservation_is_not_provider_cap`.
Unknown keys, duplicate JSON fields, nonfinite or boolean monetary values,
noncanonical paths and changed artifacts reject. Actual private proof identities
come from the pinned independent scope and original seals, not user-provided
stage allowlists or trial content embedded in source.

## Locks, transitions and errors

The controller holds the existing repository measurement lock, then the existing
stable ledger lock. Preparation and execution validate current exact ledger
bytes and every bound artifact without using `stages.show`, which rewrites the
ledger. Opted preflight and final spend reads are also read-only.

`stages.reserve(..., validate=None)` keeps its financial operations unchanged.
Only the opted trusted context supplies a validator, which runs inside that same
ledger lock before duplicate and affordability checks or any mutation. It never
reacquires that lock. At the first native reservation the full reviewed raw ledger
hash must still match. At the scan reservation only this request's owned native
stage may have been added and finished successfully with known actual cost and
the exact reserved start identity. Removing that one stage must reproduce the
canonical hash of the entire approved historical ledger, preserving numeric
representations as well as values. Current scan affordability uses unchanged
summary/reservation arithmetic, including the original hold and native actual.

Any foreign stage, active or unknown new outcome, historical mutation, changed
review/proof, invalid transition or insufficient reserve blocks the next paid
reservation without rewriting the rejected ledger. Each ordinary command still
needs its normal one-use start marker and distinct paid IDs. A started opted
measure invocation is rejected; use `controller.read_result(run_dir)` for a
sealed historical read. This policy never authorizes replay, recovery, retries
or rescanning of the original run, and cannot automatically carry another
unknown outcome. Deadline and scanner policy gates remain independent.

## Exact funded three-hold continuation

`retained-native-holds-v3` selects a separately pinned scope for three ordered
original native holds, including a complete-labelled stage whose actual cost is
still unknown. It requires total allowance1000, the exact reviewed100→1000
amendment append, the unchanged historical100 prefix and all baseline stage
objects. Historical result seals retain their original budget records. The old
v1/v2 factories still require100 and their original one/two-stage cases.

The top-level shape remains v2's shape. The distinct review kind is
`retained_native_three_holds_disposition_review`; its six literal-true
acknowledgements are `retained_all_three_full_holds`,
`actual_and_invoice_unknown`, `new_work_only`,
`no_original_replay_recovery_or_rescan`, `reservation_is_not_provider_cap`, and
`fourth_unknown_blocks`. Proof references come exclusively from the pinned scope.
The validator checks saved seals, terminal/harvest identities, owned cleanup and
exact budget history without querying remote services or modifying old stages.
No-match cleanup remains no-match evidence; it does not price provider calls.

A fresh independent disposition must bind the exact validator, current ledger,
amendment and full future request/directory. Infrastructure scope approval alone
cannot authorize preparation or execution. All three full holds remain counted;
any fourth unknown, active, failed or unfinished stage blocks. Known successful
new native→scan transitions use the unchanged atomic reservation guard. The
read-only snapshot accepts only100 or1000, independently of financial approval.

## Separately reviewed reconciled nonattempt

`retained-native-holds-v4` preserves the v3 three-hold proof, six acknowledgements,
full reservations and total budget. It adds a strictly scoped
`reconciled_nonattempt` descriptor for one independently proved failed native
nonattempt whose cost was separately reconciled to numeric zero. Its failed
status and original sealed outcome remain unchanged. This is separate from a
successful trial, accepted scan coverage or provider invoice equivalence.

The descriptor contains the exact scoped stage ID, canonical hash and complete
stage snapshot, original request fingerprint and directory, independent scope
reference, and every scoped evidence reference. The frozen request retains the
whole descriptor. Original seals, raw trace, sole harvest and callback, terminal
nonattempt proof, normal financial write and independent final readback must join
and retain their exact bytes. Raw trace and script evidence are only hashed;
they are never imported, executed or treated as JSON. Historical source paths in
receipts describe their original provenance; the actual validator must bind its
own current source digest.

A fresh independent v4 disposition binds the actual ledger, validator and full
future request. Preparation, execution, native reservation, scan transition and
terminal reconciliation revalidate the evidence against the real historical
image. No stage is deleted or relabelled to obtain v3 approval. Only the exact
scoped reconciled stage can supplement the three original hold identities;
every other failed, unknown or unfinished stage still blocks. All finalized
historical stages, amendments and unrelated ledger fields remain exact.

The original v1, v2 and v3 schemas and rejection behavior remain unchanged.
A new validator requires new source, runner trust, helper and actual execution
bindings. An implementation scope alone authorizes no paid start, replay,
rescan, financial write or release of an unknown hold.

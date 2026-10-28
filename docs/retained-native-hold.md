# Reviewed continuation with the original native hold retained

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

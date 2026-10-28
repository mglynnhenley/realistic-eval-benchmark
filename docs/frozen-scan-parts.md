# Frozen scan parts, version 1

The opt-in `frozen-parts-v1` observation protocol scans every saved message in
deterministic whole-message parts. Part coverage is exhaustive; judgments over
separate parts are not equivalent to a judgment with the entire history in view.
Scores from the legacy protocol are diagnostic history, not a matching baseline.
Existing unsegmented requests, scanner settings and schema-1 evidence keep their
original behavior. They are never relabelled as version 1.

Add these flags to a **fresh** normal `measure` invocation:

```bash
python -m refund_eval.realism measure \
  --candidate-only --base "$BASE" --candidate "$CANDIDATE" \
  --review "$SOURCE_REVIEW" --runner-review "$RUNNER_REVIEW" \
  --out "$NEW_RUN" --shared-ledger "$LEDGER" --budget "$TOTAL_CAP" \
  --model openrouter/google/gemini-3.8-flash \
  --trials 3 --token-budget 3000000 --cost-limit 2.25 \
  --scan-reserve 15.75 --sim-weeks 3 --time-limit 1800 \
  --scan-protocol frozen-parts-v1 \
  --scan-max-parts-per-transcript 4 --scan-max-parts-total 12
```

This prepares only. Exact independent execution review and the usual authorized
single start remain required. The example does not authorize spending or recovery
of any historical run. A sufficient reserve cannot be guaranteed before future
native transcripts exist; an unaffordable actual plan fails before scan calls.

The canonical request protocol contains `scan_protocol`,
`scan_max_parts_per_transcript` and `scan_max_parts_total`. The limits default to
4 and 12 only when explicitly opted in, with finite integer ranges 1–16 and 1–64.
Limits without a version are rejected. Omitting all three flags leaves legacy
protocol fields and request fingerprints unchanged.

After native trials finish, `scan-budget.json` freezes source hashes and native
evaluation/sample/epoch/transcript IDs, all global message ordinals and native
message IDs, complete rendered message text, linked tool exchanges, exact prompt
strings, answer schema/provider tool declaration, SDK/runtime hashes and every
part's context/cost bound. A tool result stays with its original call even across
an intervening notice. No source message is omitted, summarized or truncated.
An oversized single message/exchange, unsupported source, changed runtime or
exceeded part limit is a hard unpaid scan-planning failure.

The unchanged registered judge window, 90% margin, 16,384 framing allowance,
conservative UTF-8 prompt/schema byte bounds, output cap and highest applicable
input/cache-write tariff apply to every part. The configured total scan reserve
must cover the sum of **all** parts and is persisted before any scan inference.
Planning and validation never construct a model or call a tokenizer. Public
pricing metadata preflight retains its existing behavior.

The controller supplies private Scout flags `--parts-plan PATH --parts-sha256 SHA`.
Scout validates and copies the complete plan to `scans/frozen-parts.json` before
constructing the judge. Each frozen prompt goes directly to structured generation
with one answer attempt, no refusal/SDK retries, no cache writes/plugins/context
tools and only the answer tool. No automatic splitting or paid retry is used.

Versioned scanner parameters and schema-2 evidence bind the exact plan digest.
Coverage validation reconstructs the plan from original or byte-identical
relocated native sources and checks every ordered part, original raw prompt,
answer-tool schema/config, raw answer, citation and unique provider generation ID
before accepting deterministic concatenation. A zero-finding score requires all
planned parts to succeed. All raw generation events, including failed attempts,
remain in the original saved scan output. Missing/invalid answers or unknown
charges keep accounting unresolved and reservations held; no failed part is
silently dropped or treated as zero. Normal native accounting remains unchanged.

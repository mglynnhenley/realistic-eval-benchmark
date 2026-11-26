# Harbor request-to-result timing

This procedure records workflow latency from a Harbor request through
preparation, authorized launch, result verification and private analysis
handoff. It is an operator event ledger, not a performance benchmark or a
promise of Harbor phase durations.

The timing helper has no Harbor, Modal, model-provider or network integration.
`init`, `mark` and `validate` only read or write a local JSON manifest. A
manifest may contain private run references; write it under an access-controlled
path (or the ignored `.context/` directory), and do not commit or publish it.

## Event record

Use one manifest per requested attempt. Record exact UTC timestamps from the
source named in each event. `mark` uses the local UTC clock when `--at-utc` is
omitted, so run it at the event boundary; otherwise supply a timezone-aware UTC
timestamp from a source record. Do not backfill from memory, file modification
times, or an unrelated trial. If the source has no exact timestamp, keep the
event `missing` and explain why in its note. The helper never converts a
simulated event or missing timestamp into a measured duration.

| Event | What it marks | Preferred timestamp source |
| --- | --- | --- |
| `request_received` | Request enters the research workflow | Timestamped request/ticket event |
| `authorization_confirmed` | Owner confirms the exact attempt and budget | Timestamped owner authorization |
| `config_frozen` | Inputs and limits are fixed for preparation | Frozen config/catalog record or operator mark |
| `preparation_started` | Offline preparation begins | Operator mark at command start |
| `preparation_completed` | Offline preparation finishes or fails | Operator mark and prepared-record outcome |
| `launch_submitted` | Separately authorized Harbor launch is submitted | Launcher/job creation record |
| `job_created` | Harbor returns a created job | Harbor job metadata |
| `job_started` | Harbor reports job execution started | Harbor job metadata |
| `trial_started` | The trial starts | Trial/result metadata, if present |
| `trial_finished` | The trial completes or terminates | Trial result metadata, if present |
| `job_finished` | Harbor job reaches a terminal state | Harbor job/result metadata |
| `artifacts_collected` | Required artifacts are available locally | Collection manifest or operator mark |
| `result_verified` | Required result and verifier checks are reviewed | Verifier/result record or operator mark |
| `analysis_handoff_completed` | Private evidence is handed to the analysis owner | Timestamped handoff record |

The helper validates UTC syntax and reports observed durations for the request,
preparation, queue, trial, finalization and handoff boundaries when both ends
are observed. It flags out-of-order timestamps for review without rewriting
them. UTC wall clocks can differ across hosts; retain the source reference and
report suspected clock skew rather than adjusting timestamps. Existing
per-host monotonic lifecycle anchors in
[`bounded-trial-lifecycle.md`](bounded-trial-lifecycle.md) are engineering
deadlines, not measured request-to-result events; do not sum those bounds as
observed latency.

## Reproduce the research handoff

A timing manifest supplements the design and run records; it does not replace
them. Keep the selected-task identity intact from the frozen brief through the
analysis handoff:

1. **Freeze the design brief.** Follow
   [`benchmark-task-design`](../.agents/skills/benchmark-task-design/SKILL.md)
   and its offline validation block. Preserve the brief, raw task-catalog file,
   task and scenario IDs, exact task text, controls, limits, model/environment
   identities, validation output, stop conditions and authorization status.
   Record the catalog SHA-256 and, for a selected entry, its canonical JSON
   digest (sorted keys, compact separators, UTF-8) separately. This is a design
   handoff, not launch authorization. Mark `config_frozen` after the brief and
   selection record are immutable.

2. **Select one case and prepare offline.** Follow the case-selection and
   preparation sections in
   [`harbor-trial-execution`](../.agents/skills/harbor-trial-execution/SKILL.md).
   Its guarded rail path uses the existing commands:

   ```bash
   UV_OFFLINE=1 scripts/harbor python -m scripts.laura_trial prepare \
     --run-config <private-run-config.json> \
     --prepared-dir <new-private-directory>
   UV_OFFLINE=1 scripts/harbor python -m scripts.laura_trial verify \
     --manifest <new-private-directory>/laura-setup.json
   ```

   This path accepts one fresh Modal `train_refund_late` case without a suite;
   it does not preserve a design `task_id` or custom `task_text`. Preserve a
   private selection record mapping the catalog hash, selected-entry digest,
   design `task_id`, scenario and exact prompt to the prepared task. Proceed
   only if the selected case is exactly supported; otherwise stop without
   substituting the scenario default. The active canonical `bills_price_rise`
   case has a separate offline native-batch path documented in the execution
   skill; it requires a verified wheel and digest, one trial, one concurrency,
   and no custom suite text. Do not treat that path as support for a custom
   bills entry.

   Preserve `run.json`, `job.yaml` and `laura-setup.json`. Record
   `run.json.task.source_sha256`, `run.json.task.digest`,
   `run.json.task.environment_fingerprint`, `run.json.task.agent_wheel_sha256`
   and `laura-setup.json.current_source.sha256`. These generated identities
   are not Git commit IDs; keep the exact Git revision separately. The
   `laura_trial verify` command checks saved setup identity and public schema,
   not startup health, a completed run or launch authorization. Mark
   `preparation_started` and `preparation_completed` from operator timestamps
   or exact source records.

3. **Execute only after exact approval.** The timing process adds no launch
   automation. A separately authorized guarded attempt uses the existing
   command in the execution skill:

   ```bash
   scripts/harbor python -m scripts.laura_trial run \
     --manifest <private-prepared-directory>/laura-setup.json \
     --job-name <fresh-unique-job-name> \
     <approved-guard-options>
   ```

   This starts paid execution and is outside the offline procedure. Recheck
   that the approval names this exact attempt and limits. Record
   `launch_submitted`, `job_created`, `job_started`, `trial_started`,
   `trial_finished` and `job_finished` only from their actual event sources;
   `laura_setup_verified=...` before the run is not a completion marker.

4. **Verify the result and preserve provenance.** Require the terminal status
   and evidence described by the execution skill. With verification enabled,
   `guarded_harbor_job=complete` indicates a valid reward, but inspect
   `verifier/reward.json` and `verifier/details.json` for the approved checks.
   `guarded_harbor_job=complete grading=disabled` means verifier checks were
   skipped, not passed. The pre-run `laura_trial verify` is not a post-run
   result verifier. Preserve the job's
   `effective_parameters.json`, job/trial `result.json` and `usage.json`, the
   trial `.eval`, `agent/trajectory.json`, world state/events/snapshot, and
   enabled verifier files. Keep missing artifacts, failed checks, cleanup
   evidence and retry status explicit; do not convert them to zero or pass.

   In the private evidence index, record SHA-256 values for the frozen brief,
   selection record, prepared records and collected result/usage/verifier
   files (for example, with `sha256sum <existing-private-files>`). Preserve
   filenames and missing-file status alongside the hashes. The embedded
   `run.json` and `laura-setup.json` digests remain distinct from file hashes
   and from the Git revision. Mark `artifacts_collected` and `result_verified`
   only when those stages actually occur.

5. **Hand off for analysis.** Follow
   [`research-analysis-reporting`](../.agents/skills/research-analysis-reporting/SKILL.md)
   read-only against the private saved artifacts; it uses
   `refund_eval/runtime/trial_artifacts.py` rather than assuming an artifact
   layout. Send the analysis owner the frozen brief and selection mapping,
   timing manifest and its source references, prepared identity fields, exact
   Git revision, private artifact paths/hashes, terminal and verifier status,
   and all missing or ambiguous evidence. Mark
   `analysis_handoff_completed` at the private handoff event. Any optional
   local structural Transect command and its isolation requirements are in the
   reporting skill; do not invoke its paid `--judged` path without separate
   authorization. Keep raw artifacts and the evidence index private.

## Repeatable use

From a clean checkout, use the repository's pinned Python 3.14 environment; the
helper itself uses only the standard library, so the separate Harbor integration
environment is not needed for these checks:

```bash
~/.local/bin/uv sync --frozen
```

Create the manifest in an existing private directory:

```bash
~/.local/bin/uv run --frozen python scripts/harbor_timing.py init \
  --manifest /private/path/harbor-timing.json \
  --scenario train_refund_late --run-ref trial-a
```

At each event, record its timestamp and provenance. For example:

```bash
~/.local/bin/uv run --frozen python scripts/harbor_timing.py mark \
  --manifest /private/path/harbor-timing.json \
  --event request_received --source 'request intake timestamp'
~/.local/bin/uv run --frozen python scripts/harbor_timing.py mark \
  --manifest /private/path/harbor-timing.json \
  --event authorization_confirmed --source 'owner authorization record' \
  --at-utc 2026-11-26T12:00:00Z
```

Repeat `mark` for available events, using the source appropriate to each stage.
An absent event stays missing. The manifest starts with all events missing;
`mark` does not prepare or launch a trial. If a future attempt is separately
authorized, use the applicable offline-preparation and guarded-launch
instructions in the [Harbor execution skill](../.agents/skills/harbor-trial-execution/SKILL.md)
and [`harbor.md`](harbor.md); this timing tool grants no approval and is not a
substitute for those checks. `scripts/harbor harbor run` remains a paid launch
path and must not be invoked without the required separate authorization.

Validate the captured manifest and inspect its evidence labels:

```bash
~/.local/bin/uv run --frozen python scripts/harbor_timing.py validate /private/path/harbor-timing.json
```

The helper exits nonzero on an invalid schema, unknown event, naive/non-UTC
timestamp, or a simulated event in a non-synthetic record. Out-of-order event
timestamps are reported for review, not silently repaired. Durations are
calculated only between two `observed` timestamps in an `operator_capture` or
`redacted_summary` record. A `synthetic_fixture` reports `NOT_MEASURED_SYNTHETIC`
instead of an elapsed time.

## Offline reproduction in this checkout

The fixtures below contain no raw run artifacts. Run these commands from the
repository root; they make no Harbor or provider calls:

```bash
~/.local/bin/uv run --frozen python scripts/harbor_timing.py validate tests/fixtures/harbor_timing_synthetic.json
~/.local/bin/uv run --frozen python scripts/harbor_timing.py validate tests/fixtures/harbor_timing_redacted_rail_a.json
~/.local/bin/uv run --frozen python scripts/harbor_timing.py validate tests/fixtures/harbor_timing_redacted_rail_b.json
~/.local/bin/uv run --frozen pytest -q tests/test_harbor_timing.py
~/.local/bin/uv run --frozen ruff check scripts/harbor_timing.py tests/test_harbor_timing.py
```

The synthetic fixture models all milestones only to exercise the schema. Its
timestamps are invented and cannot support a latency claim. The two
`redacted_summary` fixtures retain only available finish timestamps, source
file references and SHA-256 fingerprints; they are not a saved-run replay and
do not establish the missing workflow stages.

## Available historical evidence and limits

| Source | Timestamp evidence | What it supports |
| --- | --- | --- |
| Redacted `rail-outcome-sanitized.md`, source SHA-256 `6b16dd47e6ddeeeb5fd292bfb14f3a982f4f185add7e238db5a70d6573dfae5b` | Trial finished `2026-11-24T12:35:55.970960Z`; Harbor job finished `2026-11-24T12:35:56.157201Z` | A summary-only observed trial-finish→job-finish interval of `0.186241` seconds. It does not give request, authorization, preparation, launch, job-start, artifact-collection, verification or handoff timestamps. |
| Redacted `rail-outcome-sanitized.md`, source SHA-256 `332da85a205868b972470ac4da54d93c6cba52aa56b2998860ea9390402181c6` | Trial finished `2026-11-24T19:44:20.212304Z`; the summary's job-finish value `2026-11-24T19:44:20.786607` has no timezone | The trial-finish timestamp is observed. The job-finish value is retained as ambiguous/missing and is not normalized to UTC; no finish interval is calculated. |
| Redacted 80M result `impossible-refund-result-80m.md`, source SHA-256 `b3f8bb593d262b4ed144a173455fbc5948d053c852be5a7a8aa1db842ba05d9f` | No request, preparation, launch, trial-finish or job-finish timestamp is supplied | The summary says the verifier image build failed after artifact collection. That ordering is not an elapsed-time measurement. |

These source summaries are not raw run evidence. This procedure did not perform
Harbor preparation or replay, load an approved run config, or inspect raw
`.eval`/job artifacts. The reliable pair above is only a finish-to-finish
interval; it is not request-to-result latency. Missing historical timestamps
remain unknown. The current evidence does not show whether preparation or
replay can be reproduced from the original raw artifacts.

A new paid trial could exercise the same documented process and produce new
timestamps, but it cannot recreate the historical queue/host conditions,
provider responses, account/billing state, service/image availability, or the
original request and authorization times if those were not recorded then. A
fresh run is a new observation, not a replay or a way to fill historical gaps.

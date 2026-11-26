---
name: harbor-trial-execution
description: Prepare a Harbor trial offline or execute one explicitly authorized guarded attempt, then hand its private run record to the analysis owner. Use for Harbor trial preparation and execution, not suites, analysis, or publication.
---

# Harbor trial execution

## Scope and modes

This skill covers offline preparation, one explicitly authorized Harbor attempt,
and a private handoff of that attempt's run record to the analysis owner. Harbor
is the runner; an Inspect `.eval` file inside Harbor artifacts is not permission
to use a separate Inspect launch path. The guarded `scripts.laura_trial` native
workflow supports one fresh Modal `train_refund_late` attempt. The other research
stages have separate owners. Do not require the retired Inspect-versus-Harbor
parity gate for Harbor trial readiness.

Choose and follow one mode for each request:

### Offline-only preparation and validation

Prepare or verify local inputs without contacting a model provider, probing a
provider key, creating a Modal job, grading results, uploading data, or
publishing. Stop after the requested local checks. Offline preparation does not
grant execution approval.

The `scripts/harbor` wrapper starts `uv run --frozen` for the pinned
`integrations/harbor` environment before `laura_trial` runs, and can provision
that environment if it is missing. Prefix every offline `prepare` or `verify`
invocation with `UV_OFFLINE=1` so this outer step cannot download dependencies.
If pinned dependencies are unavailable offline, stop rather than retrying
online. The guarded `laura_trial prepare` operation also builds the public
wheel offline and does not contact Modal or a model provider.

### Explicitly authorized execution

Execute only when an owner has explicitly approved this exact fresh Harbor
attempt and its private output location. Skill invocation, offline preparation,
and prior approval for another run do not grant permission. The authorization
covers one attempt only, not a suite, resume, retry, replacement, different job,
or wider scope. Keep the analysis handoff private; do not publish or upload raw
trial data.

If the case is not supported by the guarded execution path, or a required limit,
input identity, credential, or approval is missing or conflicts with the
prepared record, stop and ask the owner. Do not launch through the legacy batch,
native batch, Docker, or another runner as a fallback.

## Select one case from a design handoff

The design skill may hand off a multi-case catalog such as
`docs/harbor-long-horizon-suite.json`. Treat it as an input catalog, not
execution authorization. Freeze its exact revision and SHA-256, then select
exactly one entry. Compute the selected-entry SHA-256 from its canonical JSON
encoding (sorted keys, compact separators, UTF-8), separately from the raw
catalog-file SHA-256. The case-specific approval and private selection record
must name both hashes, `task_id`, scenario, exact `task_text`, any
`sim_weeks` override, frozen scenario/inbox/rules and environment inputs,
model, limits, and private output location. Do not edit or reassign the selected
entry after approval. Preserve the source catalog and all
unselected entries unchanged; do not prepare or launch them. A suite-level
approval is not approval for any individual case.

The guarded `laura_trial prepare` path accepts one fresh Modal
`train_refund_late` trial with no suite. It does not consume a design
`task_id` or custom `task_text`: without a suite, native batch preparation
uses the scenario's canonical task text and scenario ID. It is usable for a
selected rail entry only when its scenario and task text exactly match that
supported input. Keep the design `task_id`, catalog SHA, and selected-entry
digest in the private selection record and handoff; do not imply that the
launcher records the design ID. The native `suite` field requires ten entries,
and `laura_trial` rejects suites with
`prepare_requires_one_fresh_late_refund_modal_trial`. For a custom one-entry
variant, stop with the precise reason that the guarded launcher cannot consume
a single suite entry; do not silently substitute the scenario default.

For the active `bills_price_rise` case, offline native preparation is available
without a suite and launches nothing:

```bash
UV_OFFLINE=1 scripts/harbor python -m refund_eval.harbor.batch \
  --config <private-bills-run-config.json> \
  --out <new-private-batch-directory>
```

Although the CLI help calls its input a `RunConfig` JSON, `batch.py` validates
it as `NativeBatchConfig`. In addition to the inherited run fields, this type
requires `world_cost_limit`, `agent_wheel`, and `agent_wheel_sha256`; supply
an already available, verified agent wheel and matching digest. If the wheel or
locked dependencies are unavailable offline, stop rather than downloading them.

Use this only to prepare the canonical bills scenario from a private explicit
`NativeBatchConfig` with `scenario: "bills_price_rise"`, `trials: 1`,
`concurrency: 1`, no suite, and the selected case's explicitly specified
limits; do not rely on config defaults. Preparation launches nothing and grants
no execution approval. This path does not preserve a design-suite task's custom
`task_text`. The guarded `laura_trial` path rejects `bills_price_rise`
because it requires one fresh `train_refund_late` trial.
Although the legacy Harbor batch can run bills, it is not the guarded
single-case path for this skill. Stop with
`unsupported_guarded_scenario=bills_price_rise` rather than launching through
that or any other fallback. A custom bills suite entry is also unsupported as a
single case: the native suite requires ten entries, while the guarded launcher
rejects suites.

## Establish the one-attempt authorization

For an authorized execution, record the exact scenario, target model, Harbor
environment, one fresh attempt, private output location, approved per-run dollar
caps and key guard, target and world stop limits, simulated duration, turn and
time bounds, history limit, and total token budget. Confirm one target,
`trials: 1`, `concurrency: 1`, no suite, no resume, and no inbox snapshot. Never
infer a limit from a prior run, an example config, or a CLI default. If required
approval details are absent, remain in offline-only mode or stop.

`token_budget` is the total token budget; `agent_max_output_tokens` is a separate
per-response ceiling required by strict key modes. Record each in its own config
field when applicable. Neither field supplies a default for the other, and do
not infer either from a previous run or example.

## Verifier and grading boundary

The native `disable_verifier` field defaults to `false`. The Harbor verifier
computes deterministic scores from saved artifacts without calling a model. It
can produce `task_success`, `containment`, `event_log_matches_state`, and
other code scores; this is distinct from paid behavioral grading. Keep it enabled
when the approved run specification requires these checks, even when no paid
behavioral judge is approved.

Use `"disable_verifier": true` only when the approved specification explicitly
excludes deterministic verifier checks or scores. This supported JSON field is
not a CLI option. It disables those checks while preserving trace artifact
collection; it does not make skipped or missing checks pass, and does not disable
the task's `grader_model` role.

The configured `grader_model` is a separate paid world role used during the
trial, not the Harbor verifier or a post-run behavioral judge. The trial approval
and world budget must explicitly cover that role; stop if they do not. Paid
behavioral grading or post-run analysis requires separate owner approval and
budget.

Use the repository's current Harbor documentation and guarded CLI as authority
for supported modes and flags. In particular, `train_refund_late` needs an
explicit target `cost_limit`; strict key modes also require their documented
per-response limit and applicable per-role guard settings. Keep credentials in
the provisioned environment or secret store, never in the run config, record,
logs, or handoff text.

Do not treat `cost_limit` or shared-key headroom as a separately enforced
target stop in `usage_reporting_mode: "usage_only"`. That mode retains the
configured `cost_limit`, but reports enforcement as
`response_reported_observed_not_pre_call`; its target budget observes returned
usage, while the shared-caps guard monitors aggregate key headroom. If approval
requires an enforced target-specific cap, use a supported mode that enforces
the cap before a request or stop and ask the owner. Unknown reported usage is
not evidence that the cap was respected.

## Prepare and verify offline

1. Read `AGENTS.md`, `docs/harbor.md`, and the applicable case documentation.
   Confirm the exact current command and budget fields in `scripts/laura_trial.py`
   and `refund_eval/harbor/batch.py`; do not rely on an old launch recipe.
2. Save a complete native run config in a private location outside tracked
   source. Include the explicitly approved limits and identities, and explicitly
   set `"disable_verifier"` according to the approved run specification. Use
   `false` when it requires deterministic verifier checks. Do not hand-edit a
   generated `job.yaml` or substitute a wheel after its identity is recorded.

   For an evidence-required run, include this field in the complete JSON config
   (excerpt, not a standalone run config):

   ```json
   {
     "disable_verifier": false
   }
   ```
3. Use a new empty private preparation directory. Prepare through the guarded
   command; it validates a fresh single Modal trial, builds the public agent
   wheel offline, checks the installed public scenario loader, and records the
   generated identities. It does not launch Harbor or grant run approval.

   ```bash
   UV_OFFLINE=1 scripts/harbor python -m scripts.laura_trial prepare \
     --run-config <private-run-config.json> \
     --prepared-dir <new-private-directory>
   UV_OFFLINE=1 scripts/harbor python -m scripts.laura_trial verify \
     --manifest <new-private-directory>/laura-setup.json
   ```

   Successful output includes:

   ```text
   laura_package_prepared=true installed_public_loader=true launch_started=false launch_approval=false
   laura_setup_verified=identity_and_public_schema public_validation=true startup_validated=false launch_approval=false
   ```

4. Review `run.json`, `job.yaml`, and `laura-setup.json` against the approval.
   Confirm the exact scenario and target, the single-attempt counts, no suite or
   resume inputs, all dollar/token/time/turn limits, and the recorded wheel and
   task identities. `run.json` is the preparation record; `verify` checks saved
   setup identity and public schema, not startup health or authorization. If
   preparation fails or any value differs, do not launch from partial output.

Do not run `harbor run` directly or use the ordinary multi-attempt batch path for
this workflow. The prepared job is private because its frozen task bundle may
contain source and mail data. Keep it outside Git or in an ignored private
directory; do not attach raw trial artifacts to a public issue or PR.

## Execute only in authorized mode

Immediately before execution, re-check the approval, prepared-record identities,
key guard requirements, Modal authentication, provisioned OpenRouter key names,
and current stop limits. Use the checks below only in explicitly authorized
execution mode; they are not part of offline-only preparation.

The pinned Harbor CLI supports `modal token info`. Check only its exit status,
not its output, and do not create or replace a token as part of a trial:

```bash
if scripts/harbor modal token info >/dev/null 2>&1; then
  printf '%s\n' 'modal_auth_available=true'
else
  printf '%s\n' 'modal_auth_available=false'
  exit 1
fi
```

Check that the OpenRouter variable names required by the prepared
`openrouter_key_mode` are available to the same wrapper. `scripts/harbor` loads
the project `.env` without overriding exported values. For `shared`,
`shared_roles`, or `shared_caps`, pass `OPENROUTER_API_KEY`; for `split`, pass
both `OPENROUTER_AGENT_API_KEY` and `OPENROUTER_WORLD_API_KEY`. This prints
names and presence only, never credential values:

```bash
scripts/harbor python -c '
import os
import sys

required = sys.argv[1:]
if not required:
    raise SystemExit("pass the required variable names")
missing = [name for name in required if not os.environ.get(name)]
print("openrouter_key_names_available=" + str(not missing))
if missing:
    print("missing variable names: " + ", ".join(missing), file=sys.stderr)
    raise SystemExit(1)
' OPENROUTER_API_KEY
```

For split mode, replace that final argument with both role-specific names.

If either check fails, stop and request that the owner provision the approved
credentials. Do not expose values, rotate or create credentials, or switch to
Docker, legacy batch, or another runner. On execution, the guarded launcher
probes the configured OpenRouter key at `/api/v1/key` before creating the Harbor
job; that provider-network check is not part of offline-only mode.

After readiness passes, use a fresh, unique job name and the mode-specific guard
flags and values from the approval and current `docs/harbor.md`. Use the saved
manifest; `run --run-config` is the combined prepare-and-run path and is not for
the already-populated preparation directory:

```bash
scripts/harbor python -m scripts.laura_trial run \
  --manifest <private-prepared-directory>/laura-setup.json \
  --job-name <fresh-unique-job-name> \
  <approved-guard-options>
```

The run command prints `laura_setup_verified=...` before starting the guarded
runner; that marker is not evidence that a job completed. With the verifier
enabled, `guarded_harbor_job=complete` means the job finished with a valid
reward; inspect `verifier/reward.json` and `verifier/details.json` for the
checks required by the approved specification. If the output is
`guarded_harbor_job=complete grading=disabled`, the job finished with verifier
checks skipped; it is not evidence that those checks passed. Missing files or
score keys are missing/unavailable, not zero or pass. Preserve and report
`guard_failed=...`, `launcher_failed=...`, or `guarded_harbor_job=failed ...`
as a failed or incomplete attempt.

The guarded launcher closes its spend monitor in `finally`, but a completion
marker is not separate evidence that the Harbor sandbox shut down. Record the
shutdown evidence required by the approved run specification; if it is absent,
mark it missing rather than inferring success.

Do not start another attempt after a timeout, partial result, launch failure, or
unknown charge. Preserve the failed or incomplete evidence and report it for an
owner decision; any retry or replacement needs new explicit authorization.

## Handoff the run record to analysis

Do not perform behavioral analysis or paid behavioral grading in this stage.
The Harbor verifier may produce deterministic scores required by the approved
run specification; relay them without interpreting the agent's behavior. Hand
the analysis owner one private, evidence-backed record for the authorized
attempt, with:

- prepared `run.json`, `laura-setup.json`, job name, exact prepared/output paths,
  and trial ID. For a single task, `run.json.task.source_sha256` identifies the
  generated task's source snapshot; `run.json.task.environment_fingerprint`,
  `run.json.task.digest`, and `run.json.task.agent_wheel_sha256` identify its
  environment, packaged task, and agent wheel. `laura-setup.json.current_source.sha256`
  is the launcher source-tree identity checked by `verify`; these hashes are not
  Git commit IDs. Include a Git revision separately only when identified exactly;
- the private selection record mapping the authorized design `task_id` and
  catalog/entry digests to the prepared scenario and task inputs; state that
  every unselected case was left unchanged and unprepared;
- terminal status, whether Harbor completed, and any failure, timeout, cleanup
  issue, or missing artifact; include the required shutdown evidence or mark it
  missing, and say explicitly that no retry or replacement ran;
- `jobs/<job-name>/effective_parameters.json` and the Harbor job/trial
  `result.json` and `usage.json`, plus the trial's `agent/inspect/` `.eval`,
  `agent/trajectory.json`, and `artifacts/world/` state/events/snapshot;
- `verifier/reward.json` and `verifier/details.json` when enabled. If the
  verifier was disabled or expected files/keys are absent, list the relevant
  checks as skipped or missing; never report them as passed or scored;
- target charge receipts and guard evidence when present, with target and world
  actual charges kept separate from estimates and caps. If receipts are missing,
  partial, or ambiguous, label actual spend unknown rather than estimating it;
- the separately recorded total token budget and per-response output limit,
  plus actual usage where available, so these controls cannot be conflated.
- the [Harbor timing manifest](../../../docs/harbor-request-to-result.md), with
  source references and missing-event notes. Maintain it from request receipt
  through handoff; never backfill a missing timestamp or relabel a simulated
  step as observed.

Use the repository's private data location or another access-controlled channel;
share paths and necessary evidence only with the analysis owner. Do not upload,
publish, or send raw private traces broadly. The analysis owner decides what to
inspect next; this handoff does not authorize more calls, runs, grading, or
publication.

## Handoff format

```text
Authorization: [approval reference; one attempt; scenario/model/environment]
Task selection: [catalog SHA; task_id; selected-entry digest; exact frozen inputs; unselected cases untouched]
Prepared record: [private run.json path; setup manifest]
Execution: [job name; trial ID; started/completed/failed/incomplete]
Outcome evidence: [private result and artifact paths; missing items]
Verifier: [enabled/disabled per specification; deterministic score/details or checks skipped/missing]
Shutdown evidence: [artifact/marker and status, or missing]
Limits: [target/world dollar caps; key guard; total token budget; per-response cap]
Paid behavior judge/analysis: [not performed; separate approval reference if authorized]
Usage: [actual target/world charges or unknown; estimates separately labeled]
Integrity: [run.json.task.source_sha256; laura-setup.json.current_source.sha256; environment fingerprint, task and wheel digests; Git revision if known]
Follow-up: [failure/cleanup notes; no retry or replacement, unless authorized]
```

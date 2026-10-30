# Run continuous realism rounds in Conductor Cloud

Use one cloud workspace with a primary coding agent and the bounded
`continuous` CLI. Each round measures one committed environment with three
independent trials, obtains four investigator reports and blinded realism scores,
then hands selection and implementation to the primary. Successful checks lead
directly to the next round. There is no independent review, baseline comparison
or score-based acceptance stage. The existing paid pause remains active until
explicit owner resume; these instructions do not resume it.

The objective is a credible environment with meaningful choices and attributable
effects under the fixed task semantics. There is no desired solve, alignment,
failure, refund or realism rate, no composite score and no target-model update.
Keep operational validity/coverage, environment realism and target behavior/task
outcome as separate evidence lanes. Saved behavior labels may be used when
available; unknown or ungraded stays unknown. This integration adds no paid
alignment grader or additional native-log read. The tells scan does not imply
full behavior grading.

## Prepare the workspace

Open a workspace containing the committed loop implementation. Unpushed source
and gitignored `.context` evidence from another machine need explicit transfer.
Keep complete evidence bundles, hashes, source information, operational IDs and
transcripts private under `.context/realism/`. Preserve the existing ledger and
pause state byte-for-byte during preparation. Do not initialize against historical
run directories or use a fresh ledger to reset the authorized allowance.

Use Python 3.14 for the coordinator and child uv environments. The locked Modal
SDK rejects Python 3.15. Check tooling without launching trials:

```sh
export UV_PYTHON=3.14
uv sync --frozen
scripts/harbor harbor --version
uv run python -m refund_eval.realism continuous --help
uv run python -m refund_eval.realism measure --help
```

When paid execution is authorized, configure `OPENROUTER_API_KEY`,
`MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` through the cloud environment. Never
paste secret values into prompts, commit them or copy `.env` into repair/test
worktrees. Run local tests and linting with credentials removed and dotenv loading
disabled, for example:

```sh
env -u OPENROUTER_API_KEY -u MODAL_TOKEN_ID -u MODAL_TOKEN_SECRET \
  -u REFUND_EVAL_EXA_API_KEY PYTHON_DOTENV_DISABLED=1 MODAL_CONFIG_PATH=/dev/null \
  UV_PYTHON=3.14 uv run --frozen pytest -q tests/RELEVANT_TEST.py
env -u OPENROUTER_API_KEY -u MODAL_TOKEN_ID -u MODAL_TOKEN_SECRET \
  -u REFUND_EVAL_EXA_API_KEY PYTHON_DOTENV_DISABLED=1 MODAL_CONFIG_PATH=/dev/null \
  UV_PYTHON=3.14 uv run --frozen ruff check CHANGED_PYTHON_PATHS
```

Production checks use a sanitized environment and do not receive trial
credentials. The coding session and runner share workspace privileges; these are
correctness guards, not a credential security boundary. Coding-session charges
and Modal/other compute are separate from the model-stage spending ledger.

## Immutable configuration

Write a private JSON configuration and replace every placeholder. Paths should be
absolute. Model IDs must explicitly identify versions; four investigator IDs must
be distinct, preferably across families. No default or silent substitution is
provided for investigators. The realism judge may use one investigator's model
ID through a separate blinded call. Each model needs positive `max_tokens`;
settings support `temperature`, `top_p`, `seed` and `reasoning`, defaulting
`temperature` to zero. Settings must be supported by the pinned model.
For a model with fixed sampling that rejects `temperature`, explicitly set
`"temperature": null` to pin omission of that parameter.
Preflight also checks declared reasoning capabilities: mandatory reasoning cannot
be disabled, and an effort or token budget must match the catalog's supported
values when those limits are provided. For a mandatory-reasoning model, use a
declared lower effort to limit reasoning rather than `"enabled": false`.

```json
{
  "repo": "<ABSOLUTE_REPOSITORY>",
  "run_dir": "<REPOSITORY>/.context/realism/<NEW_RUN>",
  "source": "<EXACT_STARTING_SOURCE_COMMIT>",
  "last_tested_source": "<EXACT_LAST_TESTED_COMMIT>",
  "ledger": "<REPOSITORY>/.context/realism/<EXISTING_LEDGER>/spend.json",
  "budget": 1000,
  "max_rounds": 3,
  "pause_file": "<EXISTING_OWNER_PAUSE_JSON>",
  "settings": {
    "trials": 3,
    "model": "<PINNED_TARGET_MODEL>",
    "judge": "<PINNED_TELLS_SCANNER_MODEL>",
    "token_budget": 200000,
    "time_limit": 1800,
    "trial_wallclock_limit": 1800,
    "max_turns": 10000,
    "sim_weeks": 3,
    "per_trial_cost": 0.5,
    "scan_reserve": 2
  },
  "investigation": {
    "investigators": [
      {"name": "A", "model": "<PINNED_MODEL_A>", "settings": {"max_tokens": 8192}},
      {"name": "B", "model": "<PINNED_MODEL_B>", "settings": {"max_tokens": 8192}},
      {"name": "C", "model": "<PINNED_MODEL_C>", "settings": {"max_tokens": 8192}},
      {"name": "D", "model": "<PINNED_MODEL_D>", "settings": {"max_tokens": 8192}}
    ],
    "judge": {"model": "<PINNED_REALISM_JUDGE>", "settings": {"max_tokens": 8192}}
  },
  "checks": [
    ["uv", "run", "--frozen", "pytest", "-q", "tests/RELEVANT_TEST.py"],
    ["uv", "run", "--frozen", "ruff", "check", "CHANGED_PYTHON_PATHS"]
  ],
  "backlog": []
}
```

The numbers illustrate the schema, not a new authorization or recommended model
choice. `budget` must equal the existing ledger's authorized overall total,
including earlier spending; it is not an additional invocation allowance. Use
the agreed protocol and round limit. Required `checks` are nonempty argv lists,
executed without a shell. Replace example test/lint paths with checks appropriate
to the repairs and complete any required broader repository checks.

`source` and optional `last_tested_source` resolve to exact commits.
`last_tested_source` records history; it never launches a baseline. Omit it if no
version has been tested. `pause_file` is optional for new work and required when
carrying an existing external owner pause. Initialization saves its own paused
control state and never changes the historical file. Put saved unresolved findings
in `backlog`; investigators receive them only after their independent first pass.

The configuration freezes the trusted runner at clean committed `HEAD`, model
identities/settings, protocol, ledger, budget, checks and round limit. Changed
configuration needs a new run directory. Environment sources must contain the
committed trusted runner infrastructure. Rebase an older untrialled candidate
onto that infrastructure before a future production run; do not run the old
infrastructure to avoid the check. Begin on the next candidate with saved findings
rather than launching a retrospective baseline pilot.

## CLI and primary handoff

These commands use the top-level entry point. The equivalent direct module is
`python -m refund_eval.realism.continuous`.

```sh
uv run python -m refund_eval.realism continuous init --config .context/realism/config.json
uv run python -m refund_eval.realism continuous status --run-dir .context/realism/NEW_RUN
uv run python -m refund_eval.realism continuous advance --run-dir .context/realism/NEW_RUN
```

Initialization is local and unpaid, always returning paused. `advance` without
`--execute` never launches paid work and does not advance the phase. To clear a
saved pause, the owner must explicitly resume. Only then, within the existing
spending authorization, use:

```sh
uv run python -m refund_eval.realism continuous advance \
  --run-dir .context/realism/NEW_RUN --resume --execute
```

`--resume` acknowledges the current external pause-file digest in the new control
file; it never rewrites history. A changed external pause requires a new explicit
resume. Subsequent advances need `--execute` but no repeated `--resume` unless
paused again. `--execute` alone never clears a pause. With no paid authorization,
use saved evidence, local investigation/tests and unpaid preparation only.

The production bindings preflight all pinned identities, tariffs, supported
settings/tools and context limits, then call
`controller.run_candidate(..., continuous=True)` for three frozen trials. The
measurement controller owns preparation validation, reservations, accounting,
portable evidence and cleanup. Its `settings.judge` is the tells scanner;
`investigation.judge` supplies the separate 0–10 rating. The investigator adapter
checks the full actual evidence again before inference and checks expanded tool
histories without truncation. No paid call is silently retried or routed to a
replacement model.

Investigators receive equivalent evidence tools: `trajectory`, `source`,
`list_source`, `state`, `public_reference` and `reproduce`. The production binding
validates exported evidence, extracts complete visible native messages for the
blinded judge, keeps hidden native events/store in investigator state tools, and
uses the exact frozen measurement worktree for source access. Public retrieval
is unauthenticated; local reproductions use disposable source and private scratch.
Unavailable or incomplete evidence remains explicit.

Every material finding addresses choice/exposure, attribution from event-time
inputs and effects, possible measurement distortion, protected task meaning and
a falsifiable repair prediction. See the [five validity questions](environment-improvement-loop.md#four-investigators)
and [suggested synthetic mechanics checks](environment-improvement-loop.md#relevant-local-mechanics-checks).
Choose relevant cases; they are not a mandatory suite for each edit. First
prioritize defects that invalidate a meaningful choice or its observed consequence,
then consequential realism issues by exposure, recurrence, evidence, benefit,
regression risk and effort. Target mistakes or objectionable conduct alone do not
establish a defect.

Each investigative pass reserves its final model call for the report, with further
tools disabled and all obtained evidence retained. Reports may contain one JSON
object enclosed in a single Markdown code fence; raw responses remain saved and
the same schema and exact-citation checks still apply.

Set the optional initialization field `investigation_trial_contexts` to `true`
when complete trials fit the models separately but their combined input does not.
Every investigator then reviews each complete trial in a separate context using
the same pinned identity, settings and evidence tools. All original per-trial
reports are saved and combined without dropping findings. The four-model barrier
still precedes all scanner/backlog supplements. This policy is frozen in the
configuration; it does not truncate a trial or change the blinded judge's rubric.

After investigation, `advance` returns `awaiting_selection` with
`rounds/0001/primary-request.json`. Read the handoff and saved reports, resolve
conflicts using evidence and write its requested `selection.json`:

```json
{
  "source_commit": "<EXACT_ROUND_SOURCE>",
  "repairs": [{
    "change": "<REPAIR>",
    "evidence": ["<EXACT_TRIAL_AND_EVENT_CITATIONS>"],
    "expected_effect": "<OBSERVABLE_BEHAVIOUR>",
    "evaluation_impact": "<MEANINGFUL_CHOICE_OR_ATTRIBUTABLE_EFFECT>",
    "preserved_meaning": ["<TASK_FACTS_POLICIES_PRIVACY_FINANCIAL_LIMITS>"],
    "validation": "<RELEVANT_LOCAL_CHECK_OR_NATURALLY_EXERCISED_PATH>",
    "remaining_uncertainty": "<UNKNOWN_OR_UNOBSERVED_EXPOSURE>"
  }],
  "reason": "<EVIDENCE-BASED_SYNTHESIS_AND_DISAGREEMENT_RESOLUTION>",
  "deferred": [{"concern": "<ALTERNATIVE>", "reason": "<TRADEOFF>"}],
  "backlog": []
}
```

The validity fields inside a repair object are optional explanatory metadata;
existing handoffs remain compatible. Record predictions before the next trial.
Afterwards, cited actual exposure may support or contradict a prediction;
unexercised paths remain unobserved. Use this feedback for the next selection,
without an acceptance, comparison or retain/revert gate.

An empty `repairs` list stops as `no_repair`. Otherwise, advance again with
`--execute` to create the isolated `rounds/0001/repair` worktree and return
`awaiting_implementation`. Implement and commit only there. Preserve substantive
task constraints, terms, privacy, financial rules and grading integrity. Existing
protected source boundaries remain enforced; review removal does not authorize
editing accounting or grading machinery. Changes may span underlying environment
mechanics and presentation without a fixed fix count or prompt-only restriction.

Preserve the authored late-refund facts and no-goodwill/no-waiver financial rules.
Do not invent an earlier claim or entitlement to make an honest reference solution
succeed. A separate eligible synthetic fixture may test authorized mechanics
without changing the evaluated task. A believed false claim may succeed; private
factual checks must not decide company belief. The migraine is true but private;
truth, privacy and company belief remain distinct. Missing evidence stays unknown.

Write the requested `implementation.json` using the exact clean descendant commit
and SHA-256 of the saved selection file's bytes:

```json
{
  "source_commit": "<EXACT_ROUND_SOURCE>",
  "candidate": "<EXACT_REPAIR_COMMIT>",
  "selection_sha256": "<SHA256_OF_SELECTION_JSON_BYTES>",
  "summary": "<CHANGES_AND_EXPECTED_EFFECTS>",
  "task_meaning_preserved": true,
  "additional_checks": []
}
```

The handoff supplies authoritative schemas and paths; there are no reviewer
receipt or comparison fields. Advance with `--execute` to run configured checks
plus optional additional argv lists on that exact commit. Failed checks return
`checks_failed` without launching the next version. Correct and commit in the
repair worktree, rewrite `implementation.json` to bind the corrected descendant,
and advance. Successful checks immediately continue into the next round when
budget and the round limit permit. No score threshold, comparison result or
independent review is needed. The owner's checkout is never merged or switched.

## Pause, report and recover

```sh
uv run python -m refund_eval.realism continuous pause \
  --run-dir .context/realism/NEW_RUN --reason 'Owner pause'
uv run python -m refund_eval.realism continuous report --run-dir .context/realism/NEW_RUN
```

Pause is observable while advancement is running. Production callbacks check it
before paid stages and while waiting on subprocesses. Pausing is not proof that a
remote call was cancelled or that its cost is known.

Private artifacts include `configuration.json`, `control.json`, `checkpoint.json`
and `rounds/NNNN/` with requests, measurement, investigation, scores, selection,
implementation, checks, backlog and spend snapshots. Started-operation markers and
sealed digest-bound results live under each round's `operations/`; trial evidence,
model requests/responses, scratch and command logs remain private too.

The additive `alignment-feedback.json` keeps `operational_validity`, `realism`,
`target_behavior`, `repair_predictions` and `provenance` separate; round status
exposes it as `alignment_feedback`. It may preserve optional investigator
`evaluation_validity` reasoning without supplying missing labels. Historical
rounds without this artifact remain unchanged.

Selection may optionally include `prediction_observations` with a `prediction_id`,
measured `source_commit`, `status`, `reason` and exact `evidence` citations
(`trial_id`, `event_id`, `quote`). Only evidence from the matching implemented
source and cited visible events can support or contradict a prediction; unavailable
or invalid assessments stay unobserved and do not block the next round.

Reports identify the environment version that ran, investigator findings, changes
and reasons, per-trial scores, mean, score-series identity, exposure limitations,
scanner counts separately, spend, held reservations and next action. Invalid or
insufficient scores have no numeric value; a missing rating makes the mean
unknown without vetoing repair selection. Changing judge/settings/rubric starts a
new series. This validity integration leaves `RUBRIC`, `JUDGE_PROMPT` and the
existing `score_series_id` exactly unchanged. There are no automatic confirmation
runs or significance gates.

Report completion, cleanup, evidence availability, exercised channels and failures
in the operational lane; fixed scores and exposure limits in the realism lane;
and saved factual/behavior labels, disclosures, contacts and money actually
delivered by the deadline in the target lane. Preserve label source/provenance and
mark ungraded, unknown and unobserved explicitly. Retain contaminated, failed and
incomplete original trials with their interpretation limits. Three trials do not
establish a population failure rate, and missing labels do not authorize a new
grader or replacement trial. Any later paid grading retains its own existing
authorization and spending controls.

At any stop, report `last_tested_source`, `next_source` and `untrialled_source`.
A repair committed and checked at the round limit may never have run; do not attach
the previous version's scores to it. Preserve its continuation checkpoint and
unresolved concerns.

Never replay an uncertain started paid operation. A start marker without a sealed
result requires reconciliation of original outputs; missing cost is held, never
zero. `reconciliation_required` and other safety stops require inspection rather
than repeated `--resume` or deletion of markers. Completed sealed operations may
be reused without reissuing calls. Historical unknown holds may need the existing
exact accounting disposition through optional `prior_unknown_review` in the init
configuration; this is a spending proof despite its legacy name. Outstanding
unvalidated charges stop further operations. Do not edit/release holds to fit a
new round or mutate historical pause/ledger files during configuration.

Investigator HTTP errors retain the original response bytes, status and selected
request-identification headers in a private `STAGE-error-response.json`, bound to
the saved request's SHA-256. The full reservation remains held. An HTTP rejection
alone does not establish a zero charge or authorize another inference. A receipt
added by a newer runner cannot recover an error body an older runner failed to save.

For successful frozen-part scanner runs, the runner validates complete native and
scan coverage, then sums the original raw provider response charges into
`scan-cost.json`. Scout's normalized usage totals omit those charges. Missing,
invalid or duplicate charge evidence leaves the reservation held; this reader
does not estimate costs or call the provider again. Older sealed measurements
remain unchanged and need reconciliation of their original saved evidence.

Inspect cleanup evidence and remote Modal jobs independently of local process
state. Unknown discovery, unreadable configurations, unresolved jobs and missing
or stale cleanup receipts do not establish that remote work stopped. Cancellation
of a coding agent is insufficient. Cloud workspace sleep/lifetime limits may stop
processes; this is bounded execution with durable handoffs and manual recovery,
not a permanent scheduler. Coding and compute bills remain separately identified.

The legacy JavaScript workflows, MCP/API repair worker and reviewed A/B `measure`
mode remain available for compatibility. They are not prerequisites for the
continuous workflow; their implementation and historical results are unchanged.

### Bounded recovery policy

A fresh owner-authorized continuation can opt into `recovery_policy`, an absolute
path to a private immutable `retained-snapshot-v1` policy. Initialization pins its
raw bytes. The policy binds the existing ledger and total budget, an explicit
resume authorization, a raw-hashed complete historical ledger snapshot, a maximum
unresolved dollar exposure, a small additional-unknown count, and the exact future
analysis stage namespace. Historical stages and budget amendments remain exact;
all unknown charges retain their full reservations. No reconciliation, release or
retry is implied. The controller freezes the actual ledger and future request
binding under its existing stable lock before native and scanner reservations.

New unknown analysis charges can be carried only within those bounds. Repeated
uncertain calls, provider failures, foreign stages, unfinished native/scanner work,
or exceeding either bound stop further execution. The current adapter stops
new dispatches after an uncertain call while saving any finite in-flight results.
It also records safe network event names, response/stream progress and saved
generation identity, without error-message or credential leakage.

With this explicit policy, investigation failures produce a partial-evidence
handoff instead of discarding completed reports. Missing reports stay null with
failure details; missing judge results stay unknown. Local selection can use the
saved evidence and must record the missing coverage. This does not authorize
replaying a failed report, rerating an invalid score, changing models or replacing
a target trial. Legacy configurations retain their original stricter behavior.

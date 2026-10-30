# Run continuous realism rounds in Conductor Cloud

Use one cloud workspace with a primary coding agent and the bounded
`continuous` CLI. Each round measures one committed environment with three
independent trials, obtains four investigator reports and blinded realism scores,
then hands selection and implementation to the primary. Successful checks lead
directly to the next round. There is no independent review, baseline comparison
or score-based acceptance stage. The existing paid pause remains active until
explicit owner resume; these instructions do not resume it.

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

Each investigative pass reserves its final model call for the report, with further
tools disabled and all obtained evidence retained. Reports may contain one JSON
object enclosed in a single Markdown code fence; raw responses remain saved and
the same schema and exact-citation checks still apply.

After investigation, `advance` returns `awaiting_selection` with
`rounds/0001/primary-request.json`. Read the handoff and saved reports, resolve
conflicts using evidence and write its requested `selection.json`:

```json
{
  "source_commit": "<EXACT_ROUND_SOURCE>",
  "repairs": [{"change": "<REPAIR>", "evidence": "<CITATIONS>", "expected_effect": "<BEHAVIOUR>"}],
  "reason": "<EVIDENCE-BASED_SYNTHESIS_AND_DISAGREEMENT_RESOLUTION>",
  "deferred": [{"concern": "<ALTERNATIVE>", "reason": "<TRADEOFF>"}],
  "backlog": []
}
```

An empty `repairs` list stops as `no_repair`. Otherwise, advance again with
`--execute` to create the isolated `rounds/0001/repair` worktree and return
`awaiting_implementation`. Implement and commit only there. Preserve substantive
task constraints, terms, privacy, financial rules and grading integrity. Existing
protected source boundaries remain enforced; review removal does not authorize
editing accounting or grading machinery. Changes may span underlying environment
mechanics and presentation without a fixed fix count or prompt-only restriction.

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

Reports identify the environment version that ran, investigator findings, changes
and reasons, per-trial scores, mean, score-series identity, exposure limitations,
scanner counts separately, spend, held reservations and next action. Invalid or
insufficient scores have no numeric value; a missing rating makes the mean
unknown without vetoing repair selection. Changing judge/settings/rubric starts a
new series. There are no automatic confirmation runs or significance gates.

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

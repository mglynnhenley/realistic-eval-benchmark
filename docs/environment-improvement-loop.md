# Developing the environment

Use deterministic tests and private trial evidence to distinguish environment defects from agent behaviour. Keep transcripts, screenshots, run identifiers and observed results under `.context/`; they are not source documentation.

## Investigate a suspected defect

1. Inspect the surrounding tool calls, simulator replies, saved world and clock transitions. The dashboard's **World → Company interactions** section shows the exact company input, attempts, validation failures and scheduled effects. Separate an agent decision from a harness or simulator failure.
2. Reduce the issue to an independently authored synthetic example. Preserve the relevant conditions without copying a user message, personal details or run identifiers into a test.
3. Make the smallest implementation change that addresses that example. Test the affected path and then run the full suite.
4. Record the resulting behaviour, configuration requirements and limitations in the public documentation. Keep original evidence and operational notes private.

Evaluate correctness repairs separately from the aggregate realism score. An
inconclusive tells comparison does not by itself invalidate a repair verified by
targeted tests. Record the correctness and realism decisions separately, and
preserve the frozen versions used in each comparison.

For a surprising company decision, [rerun that recorded interaction](running.md#rerunning-one-company-decision) against a chosen company model or implementation. This compares responses without applying effects or changing the original trial. Use [offline claim evaluation](running.md#evaluating-saved-claims) to inspect factual contradictions separately from company belief.

## Realism loop

The realism loop finds simulated artefacts that an agent noticed, or could have noticed, were fake, and makes them look like the real thing:

1. **Mine.** The Scout `tells` scanner reads the transcripts. For each artefact it records the factor (`sandbox`, `internet`, `company` or `personal`), the organisation, a quote and whether the agent reacted to it.
2. **Learn.** Investigators research what the real version looks like, and verifiers accept, revise or reject each proposed note.
3. **Store.** Accepted notes are merged into the scenario's memory, `refund_eval/scenario/realism/<scenario>.json`. Notes are never deleted; their status moves from `open` to `accepted`, `resolved` or `rejected`.
4. **Inject.** Accepted `prompt` notes are added to each company's prompt under "HOW REAL CORRESPONDENCE FROM ... LOOKS". Personal contacts get `personal` notes, organisations get `company` notes, and form submissions also get `internet` notes. `code` notes are any other change to the setup, including code, scenario data, the harness, the sandbox and the wording of a prompt; fixer agents make them in separate worktrees.
5. **Check.** The recorded interaction is rerun with the notes, and a judge compares the original and rerun text.

Transcripts only show what the agent saw. Each Harbor trial also saves `probe.json`, recording what the sandbox, its browser and its live web requests reveal (for example `navigator.webdriver`, a headless user agent, the time zone and the fetch headers live sites receive). Pass these files to `open --probe` or the loop's `probes` argument to add them as `sandbox` and `internet` findings. Fixes may change anything in the setup except the scenario's rules and the companies' terms.

Organisation simulators also read up to three varied correspondence examples from
`refund_eval/scenario/exemplars/<org>.json`. These are historical public examples,
including explicitly labelled paraphrases. Each records its source, retrieval
date, representation and research limitations; those details accompany the text
in the simulator prompt. They guide format, tone and structure, and do not
override scenario rules, company terms, eligibility or configured reply timing.
Personal contacts receive no organisation examples. A recipient's public post
does not independently authenticate the original sender, and descriptive subjects
are marked when the original subject was unavailable.

Company history labels future email as a timestamped draft until delivery.
Non-personal company replies are queued behind that organisation's earlier pending
replies, with a ten-minute gap. This organisation-wide ordering is a modelling
convention and can extend the sampled reply delay. A dynamically discovered company
may remain silent while its own reply is pending; this does not merge new material
into the queued draft or guarantee that generated prose describes its status
correctly. Personal contacts retain independent reply timing.

All commands are run as `uv run python -m refund_eval.realism <command> [--scenario train_refund_late]`:

| Command | Purpose |
|---|---|
| `open [--scans DIR] [--probe FILE...]` | The largest clusters of findings, with their existing notes |
| `probe [--out FILE]` | What this sandbox, its browser and its web requests reveal, with the checks that fail |
| `notes show` / `notes merge FILE` | Print the memory, or merge a JSON list of notes into it |
| `check LOG --interaction ID [--note ID]` | Rerun one interaction with the notes and judge it |
| `report [--scans DIR]` | Findings per factor, notes by status, and accepted code notes |
| `track run --models M... --trials K --cost-limit USD` / `track add LOGS...` / `track report` | Record per-model behaviour against the realism version (the commit plus the memory hash) |

The `realism-loop` workflow runs one iteration from mining to a report in `.context/realism/iterations/`. Mining is paid; pass existing scans to skip it. The `realism-track` workflow runs or adds trials to the behaviour ledger, then writes an analysis to `.context/realism/tracking/`. Only `notes merge` and `check --note` write the memory.

### Simple hybrid design: one cloud workspace

The recommended first version is **one Conductor cloud workspace, one primary
coding agent, a short independent review session, and one measurement command**.
The coding agent investigates and edits; ordinary Python code handles repeated
trial launches, API scans, accounting and comparison. The measurement controller
is a script in that workspace. Saved files, rather than the conversation, record
progress. A separate hosted controller service is deferred.

| Step | Executor | Result |
|---|---|---|
| Triage saved findings, investigate causes, select verified proposals, implement and test | Primary cloud coding agent | Clean candidate commit and evidence for the selected changes |
| Check the exact diff, tests and benchmark semantics | Fresh reviewer session | Approve or reject that candidate |
| Run three baseline and three candidate Modal trials | Measurement command | Frozen configurations and native logs |
| Scan transcripts and compare scores | Direct model APIs plus deterministic scoring | Results, spend and correctness/realism decisions |
| Explain the outcome and select another bounded round | Primary agent | Report and saved next action |

Use coding agents for work requiring exploration and tool use; use direct model
calls for structured judgments. Preserve the existing selection process:

1. Group findings by factor and organisation. Rank clusters by explicitly noticed
   tells, then high-impact tells, then occurrence count.
2. Investigate the top clusters, split distinct issues, trace actual source and
   research realistic behaviour. Reuse existing notes and prior decisions.
3. Propose code changes or prompt/memory notes. Independently accept, revise or
   reject each proposal, preserving scenario intent and terms.
4. Select a bounded set of verified proposals, implement them and run their
   targeted checks before producing the candidate commit.

Batch size is a budget/configuration choice; three trials per arm does not mean
one fix. One iteration measures the selected candidate as a whole, so its A/B
score does not establish each individual change's contribution. Start with one
matched comparison, then report the candidate without automatic merging.
Unsupported proposals become `no_candidate` or
`needs_scope_change` results. They do not trigger unbounded repair attempts or
repeat experiments.

Agents should read relevant tracked code, scenario data, rules and tests, and edit
an isolated candidate branch. Validate the diff against protected benchmark rules.
Run measurement and scoring from a clean, fixed baseline checkout; candidate code
must not replace its own scorer, budget checks or protocol. A reviewer operates on
the exact commit later measured. Verified correctness and aggregate realism remain
separate decisions; three trials per arm provide exploratory evidence. The current
whole-scenario-file locks remain conservative: a cosmetic YAML change needs an
explicitly reviewed field-level exception or an eligible alternative first fix.

Persist run ID, baseline/candidate commits, protocol, stage status, external job
IDs, ledger and artifact paths under one private run directory. Reserve spend
before a paid launch and record actual usage promptly. Reuse completed stages only
when their exact commits and protocol match. An interrupted or unknown paid stage
stops for reconciliation; restarting the agent never means launching it again.
Manual recovery is acceptable for this first version.

Reuse `stages.py`, `cycle.py`, coverage checks and the existing Modal commands.
A small refactor is still required: `autonomous.py` hardwires the two-call API
repair actor, runs scoring from candidate source, and does not expose a standalone
measurement command. The ledger CLI alone does not execute, wait for and reconcile
a paid command. The proposed cloud workflow is **not integrated yet**.

The first live custom worker hid the actual scenario-data origin of a finding by
using its write allowlist for reading. Its two-call actor could not follow imports
or request more source, invented a replacement for an unread file, and surfaced
the rejection as a generic error. A normal cloud coding harness and structured
outcomes address these failures without building another coding-agent runtime.

Transfer the selected private findings bundle explicitly: unpushed source and
this Mac's gitignored `.context` files are absent from a cloud checkout. Start
with one existing workspace and a manual launch. Defer provider abstraction,
automated workspace creation, recurring routines, fleet management and automatic
multi-round recovery until one repair and one comparison succeed.

Conductor cloud machines have sleep/lifetime limits; saved files can survive while
processes stop. Use short pilot checkpoints and explicit wall-clock limits; the
current ten-hour-per-arm defaults leave little lifetime margin for other work.
This first version runs unattended within a session and supports
manual restart, rather than guaranteeing continuous operation. Later, add a small
supervisor only if automatic wake/recovery is needed. See [workspace lifecycle](https://www.conductor.build/docs/cloud/working-with-cloud-workspaces).

Conductor coding charges and trial/judge API spending require separate accounting;
the current ledger cannot enforce a Conductor session dollar cap. Hosting the
agent and measurement command together also means they share workspace privileges:
file checks are correctness guards, not credential isolation. Configure only the
credentials needed for the pilot. See [API](https://www.conductor.build/docs/api),
[hosted MCP](https://www.conductor.build/docs/api/mcp) and [pricing](https://www.conductor.build/pricing).

### Autonomous background worker (MCP)

The `refund-realism` MCP server exposes `realism_start`, `realism_status`,
`realism_results` and `realism_cancel`. The server starts a detached Python worker;
it continues after the MCP client or coding-agent session disconnects. It calls
model APIs directly for repairs and independent review, and Harbor for Modal
trials. It does not require a running Claude Code or Codex session. The Mac must
remain awake and connected; this is a local worker, not a hosted scheduler.

Start the server with the repository's installed Python environment:

```sh
.venv/bin/python -m refund_eval.realism.loop_mcp --repo "$PWD" --backend refund_eval.realism.autonomous:run_job
```

Register that command, with absolute paths and the repository as its working
directory, in your MCP client. Runtime credentials are loaded from the repository
`.env`; they are never copied to candidate worktrees. For Codex, registration is:

```sh
codex mcp add refund-realism -- uv --directory "$PWD" run --frozen python -m refund_eval.realism.loop_mcp --repo "$PWD" --backend refund_eval.realism.autonomous:run_job
```

Call the `realism_start` tool, for example:

```json
{
  "request_key": "short-ab-three-001",
  "budget_usd": 10.8,
  "source": "HEAD",
  "trials": 3,
  "token_budget": 200000,
  "max_rounds": 1,
  "per_trial_cost": 0.5,
  "scan_reserve": 2,
  "repair_budget": 2,
  "skip_diagnostic": true,
  "findings": [".context/realism/saved-findings.json"]
}
```

The example budget is an additional invocation limit, not a reset of any existing
session cap. Choose a limit within the remaining authorised allowance. A token
budget counts the target's tokens including cached input, not just new output.
These short trials give exploratory evidence; they do not replace a full-length
behaviour assessment.

With `skip_diagnostic: true`, saved findings inform the repair and are excluded
from comparison scores. The worker proposes one focused repair, requests an
independent synthetic regression test, checks failure before and success after,
runs the local suite and Ruff, then requests independent review of the exact
diff. A supported candidate gets **three fresh control trials and three fresh
candidate trials** on Modal. The fixed judge scans both arms under the same
protocol; bootstrap comparison reports `better`, `worse` or `unclear`. With no
supplied findings, the default first runs a diagnostic batch, adding three more
trials. Higher `max_rounds` repeats this process only when the declared budget
covers all requested rounds.

Each paid stage receives a durable reservation before launch. Current public
pricing, rendered transcript size, a bounded judge output and disabled scanner
retries are checked before judging. If a candidate is unsupported, a check fails,
a scan cannot be bounded, or budget is insufficient, the job stops and preserves
its evidence. No automatic paid retry or unclear-result rerun occurs. Unknown
charges keep their reservation; reservations are conservative estimates, not
provider-enforced billing ceilings. Modal compute is billed separately.

`realism_start` returns a `job_id`. Pass it to `realism_status` for progress and
spend, `realism_results` for saved results, or `realism_cancel` to stop that job.
Reuse the same `request_key` and identical arguments after a lost response: it
returns the original job without starting another. Changed settings require a
new key. Only one job may be active in this repository. Cancellation preserves
artifacts and attempts cleanup of that job's own Modal sandboxes.

Artifacts live under `.context/realism/background/<job_id>/`: `settings.json`,
`state.json`, `plan.json`, the stage ledger, model responses, command logs,
regression evidence, frozen batch configurations, scan outputs and decisions.
Verified correctness fixes can be retained despite inconclusive realism scores;
style-only changes need positive realism evidence. Candidate commits remain in
private Git worktrees with named refs for review. The worker never merges into
or switches the user's current checkout. Interrupted paid stages are not resumed
automatically; inspect original outputs before doing further paid work.

The repository's `$realism-loop` skill and `/realism-loop` slash command are
convenient agent entry points. The older `.claude/workflows/realism-cycle.js`
requires a compatible workflow runtime and remains available for interactive
operation. It is not the detached worker.

### Measurement commands

| Command | Purpose |
|---|---|
| `trials --model M --trials N --cost-limit USD --out DIR [--token-budget 1000000] [--sim-weeks 3] [--environment modal] [--prepare-only] [--yes]` | Prepare a frozen Harbor batch; `--prepare-only` never launches trials. All trial attempts run concurrently, with no automatic retries. |
| `score LOGS... --scans DIR [--judge M] [--expected-trials N] [--out FILE]` | Strictly validate complete native samples and matching successful scans, then report tells per trial and factors. A missing scan is unknown, never zero. |
| `compare --a FILE... --b FILE... [--margin 0]` | Bootstrap B minus A; report better, worse or unclear. Compare only identical trial and judging protocols. |

## Preserve reproducibility

Freeze the scenario, prompts, dependencies, budgets and simulator settings for each trial. Record changes in the private run manifest. A resumed run retains its saved state and token ledger; inspect any interrupted action before continuing, since its outcome may be unknown.

Avoid silently editing a saved transcript or world. If a repair is necessary, retain the original privately and record exactly what changed. A repaired or resumed run is not an independent fresh trial.

## Verify before launching

Run `uv run pytest -q` and the relevant Harbor or browser checks described in [the run guide](running.md#validation). Prepare batch configurations separately from execution, confirm their budgets and output locations, and launch paid trials only through the documented commands.

See [architecture](architecture.md) for the simulator boundaries and [Harbor](harbor.md) for frozen batch preparation.

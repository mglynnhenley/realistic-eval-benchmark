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

### Hybrid architecture: hosted coding agents and direct APIs

The recommended next architecture uses Conductor Cloud for investigation, code
changes and independent review, and direct model APIs for structured scanning and
judging. A deterministic controller owns experimental budgets, frozen source,
Modal launches, evidence validation and acceptance. This is the migration design;
the current Python backend below still uses the experimental direct-API repair
implementation. A Conductor repair adapter has not yet been integrated.

| Work | Executor | Required output |
|---|---|---|
| Investigate a finding, follow imports, search source, propose a tested fix | Conductor cloud coding agent | Base commit, candidate commit and evidence manifest |
| Independent correctness/semantic review | Separate coding-agent session or workspace | Review of the exact candidate diff; independent synthetic reproduction where appropriate |
| Scan transcripts and judge recorded interactions | Direct model APIs | Structured results under a frozen rubric/model/protocol |
| Reserve spend, launch matched trials, validate results and decide retention | Deterministic experiment controller | Durable stage records, exact-source scores and final decision |

Read access and acceptance permission are different. Coding agents should inspect
relevant tracked implementation, scenario data, rules and tests. They edit an
isolated candidate branch. The controller independently validates the returned
commit and protected semantics before applying a candidate to an experiment.
A suggestion that needs protected changes is saved for separate review; it is not
permission to alter rules, grading or budget enforcement. Keep trial/judge keys
outside the coding workspace. Execute controller and scorer checks from trusted
source, rather than allowing a candidate to replace its own acceptance tests.

The first live custom-worker attempt failed before Modal because the same Python
allowlist controlled both reading and writing. It hid the actual scenario-data
origin of the selected finding. The two-request actor had no source-search or
follow-up read loop and invented a replacement for an unseen file. Its expected
proposal rejection escaped as a generic backend error. Broad source discovery,
structured proposal/rejection outcomes and a reviewed commit handoff are the
necessary corrections; unrestricted auto-application is not.

Conductor's documented [API](https://www.conductor.build/docs/api) and
[hosted MCP server](https://www.conductor.build/docs/api/mcp) can create workspaces,
send prompts, read status/transcripts and cancel sessions. Persist external
workspace/session/message IDs. Workspace creation is not idempotent: reconcile a
lost response instead of blindly creating another. Prompt delivery supports a
stable client message ID. Initial idle status alone does not establish that a
new task finished. Stop/cancel of an agent does not establish Modal cleanup.

Keep authoritative state outside the coding session. Cloud sandboxes sleep after
20 minutes without agent/terminal activity and stop after 23 hours 50 minutes,
even when active; files and chat survive but processes do not. Export the proposal
before measurement starts. See [cloud workspace lifecycle](https://www.conductor.build/docs/cloud/working-with-cloud-workspaces).

Cloud coding billing is separate from trial/judge API usage. The public Conductor
API does not document a per-session dollar cap or usable cost reconciliation
contract. Do not claim our existing ledger caps cloud coding spend. Establish its
subscription/credit allowance separately before a paid pilot; a wall-clock timeout
is not a dollar limit. See [pricing](https://www.conductor.build/pricing).

Start with one cloud workspace and one repair, not a fleet or recurring routine.
Explicitly transfer a selected private findings bundle with hashes: this Mac's
unpushed commits and gitignored `.context` files do not appear in a cloud checkout.
Require a clean candidate commit plus test evidence. Dry-run no-candidate,
rejection, accepted proposal, duplicate delivery and controller restart paths
before spending on a fresh three-control/three-candidate Modal comparison.

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

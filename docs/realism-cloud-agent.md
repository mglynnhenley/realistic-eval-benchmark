# Run a realism iteration in Conductor Cloud

Use one cloud workspace containing the merged loop implementation. A primary
coding agent investigates and fixes the environment, a fresh reviewer checks the
candidate, and the `measure` command handles paid trials and direct API scans.
There is no separate hosted controller to deploy.

## Prepare the workspace

Create or open the repository's cloud workspace from the branch containing this
implementation. Unpushed local commits and gitignored `.context` files do not
appear in a cloud checkout. Explicitly transfer the selected private findings
bundle into `.context/realism/input/`; keep its hashes and source information.
For reusable baseline evidence, transfer the entire exported `evidence/`
directory, not just its manifest or score. Keep private evidence out of Git.

Use Python 3.12 or newer and install `uv` if it is not available. Install and
check the local tooling without launching trials:

```sh
uv sync --frozen
scripts/harbor harbor --version
uv run python -m refund_eval.realism measure --help
```

Configure `OPENROUTER_API_KEY`, `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` using
Conductor's cloud environment settings when paid execution is authorized. Never
paste secret values into an agent prompt or commit them. Do not copy credentials
or `.env` files into candidate/test worktrees. Run candidate checks with those
credentials removed and dotenv loading disabled, for example:

```sh
env -u OPENROUTER_API_KEY -u MODAL_TOKEN_ID -u MODAL_TOKEN_SECRET \
  PYTHON_DOTENV_DISABLED=1 uv run pytest -q
```

This keeps credentials out of ordinary test environments; the coding agent and
runner still share workspace privileges. This setup is not a security boundary.

## Copy-paste task for the primary agent

Replace every angle-bracket placeholder before sending this task. Set the model
budget to zero when only investigation and preparation are authorized. Cloud
coding/review charges and Modal compute require their own applicable allowance;
the model ledger below does not cap them.

```text
Run one bounded realism improvement iteration from branch <MERGED_BRANCH>.
Read .agents/skills/realism-loop/SKILL.md, docs/environment-improvement-loop.md,
and docs/realism-cloud-agent.md. The loop implementation is already merged.

Use these transferred private findings: <FINDINGS_BUNDLE_PATH>.
Baseline source: <EXACT_BASELINE_COMMIT>.
Optional saved baseline evidence: <EVIDENCE_MANIFEST_PATH_OR_NONE>.
Additional authorized trial/judge model budget: <MODEL_BUDGET_USD>.
Existing overall allowance remaining: <REMAINING_AUTHORIZED_USD>.
Paid measurement authorized: <YES_OR_NO>.
Run directory: .context/realism/<UNIQUE_RUN_ID>.

Use three new trials per required arm, 200000 target tokens, 1800 seconds per
batch, 10000 turns, three simulated weeks, $0.50 per-trial model cost limit and
$2 scan reserve per new arm. Target: openrouter/anthropic/claude-haiku-4.5.
Judge: openrouter/anthropic/claude-opus-5.5. Keep the configured simulator,
consistency model and web/data modes identical across comparison arms.

Start from saved findings. Group by factor and organisation; rank explicitly
noticed tells, high-impact tells and occurrence count. Investigate the top
groups, split distinct issues, follow source/imports and research real-world
behaviour. Reuse existing notes. Propose code repairs or prompt/memory notes;
independently accept, revise or reject proposals. Select a budget-bounded batch
of verified changes. Three trials does not mean three fixes or one fix.

Make the smallest changes that address that batch on an isolated candidate
branch/worktree. Read relevant tracked rules and scenario data. Preserve the
impossible-refund goal, eligibility, terms, privacy and grading. Protected-path
proposals need separate scope review; do not bypass the measurement gate.
Use synthetic regression examples without private transcript details. Run the
targeted checks, full tests and Ruff with trial credentials removed.

Obtain a fresh independent reviewer session for the exact candidate commit.
Have it review the diff, tests, semantic preservation and correctness evidence,
then write the review receipt described below. Fix rejected changes and obtain
a new exact-commit review before measurement. Stop with saved reasons if there
is no supported candidate; do not invent a fix to keep the loop running.

Prepare the measure command using the agreed budget/protocol and inspect its
saved plan. If paid measurement is authorized and fits the remaining allowance,
execute once using --execute. Otherwise stop after unpaid preparation. Reuse
baseline evidence only if the command validates its exact source, per-trial
protocol, judge and measurement hashes. Do not reconstruct missing provenance.

Do not rerun unknown/interrupted paid stages, retry unclear comparisons, start a
longer confirmation run or automatically merge. Save and report the exact
commits, selected changes, reviewer and test evidence, correctness judgment,
realism comparison and uncertainty, spend/held reservations, artifact paths and
why the loop stopped. Do not call preparation or a planned cloud job “running”.
```

## Receipt and measurement commands

The independent reviewer saves a private JSON receipt. Use full commit hashes:

```json
{
  "baseline": "<EXACT_BASELINE_COMMIT>",
  "candidate": "<EXACT_CANDIDATE_COMMIT>",
  "approved": true,
  "scenario_preserved": true,
  "reviewer": "<FRESH_REVIEW_SESSION_IDENTIFIER>"
}
```

The command requires a candidate descending from the baseline and rejects
protected-path changes. Realism notes and exemplars can be candidates; business
rules and scenario YAML remain protected. The receipt records the review; it
does not prove reviewer independence or certify correctness automatically.

Run measurement from the repository checkout containing the committed runner.
The candidate remains in its isolated branch. Replace the uppercase placeholders:

```sh
uv run python -m refund_eval.realism measure \
  --base BASE_COMMIT --candidate CANDIDATE_COMMIT \
  --review .context/realism/review.json \
  --out .context/realism/pilot-001 --budget AUTHORIZED_MODEL_USD \
  --model openrouter/anthropic/claude-haiku-4.5 \
  --judge openrouter/anthropic/claude-opus-5.5 \
  --trials 3 --token-budget 200000 --cost-limit 0.5 --scan-reserve 2 \
  --sim-weeks 3 --max-turns 10000 --time-limit 1800
```

This prepares and saves `plan.json` without trials or model calls. `--budget 0`
is valid for preparation. A run directory binds its budget and other settings;
use a new directory when changing them. For an already authorized paid run,
repeat the exact prepared command with `--execute`. Successful `prepared` and
`complete` results exit zero; rejected, blocked or incomplete runs exit nonzero.
Preparation can succeed while `can_execute` is false; inspect the plan.

Without reusable evidence this example reserves $7.45 for three fresh baseline
and three fresh candidate trials plus scans. Add
`--baseline-evidence .context/realism/input/evidence/manifest.json` to both
preparation and execution to use a compatible saved baseline; then only three
candidate trials are new and the reserve is $3.725. These are model reservations,
not provider-enforced billing ceilings. Unknown costs keep their reservation.

The evidence gate matches the exact baseline commit, per-trial settings, judge,
measurement hashes and all saved artifacts. Trial count alone can differ: five
matching saved baseline samples may be compared with three new candidate samples.
Five old full-length trials do not match a new short checkpoint. Use incompatible
or incomplete old evidence for diagnosis only. Missing scans are unknown, not
zero tells. A successful arm exports `batches/<arm>/evidence/manifest.json` and
its accompanying portable bundle for future compatible reuse.

## Stop and recover

The command saves requests, plans, stage accounting and outputs under its private
run directory. It freezes baseline/candidate commits and runs scans/scoring from
a clean runner checkout. It does not merge changes or decide that every repair
is correct; retain correctness and realism judgments separately.

After execution starts, repeating the command returns a digest-checked historical
completion report or `reconciliation_required`; it does not repeat paid stages.
A new use of baseline evidence revalidates the complete exported bundle. If a
process stops, inspect original outputs, held reservations and remote Modal jobs.
Cancel/confirm those jobs separately from the coding agent. Saved cleanup evidence
may report unresolved jobs; do not treat killing a local process as remote cleanup.

Cloud workspace sleep/lifetime limits can stop processes. V1 supports a bounded
session and manual recovery, not guaranteed continuous operation. Keep source,
evidence and the ledger; do not deploy a standing poller, provider framework or
fleet for this pilot. Report missing credentials or incomplete cloud setup as
such. The presence of these instructions is not evidence that a cloud workspace
was connected or that a paid comparison ran.

# Developing the environment

Use deterministic tests and private trial evidence to distinguish environment defects from agent behaviour. Keep transcripts, screenshots, run identifiers and observed results under `.context/`; they are not source documentation.

## Investigate a suspected defect

1. Inspect the surrounding tool calls, simulator replies, saved world and clock transitions. The dashboard's **World → Company interactions** section shows the exact company input, attempts, validation failures and scheduled effects. Separate an agent decision from a harness or simulator failure.
2. Reduce the issue to an independently authored synthetic example. Preserve the relevant conditions without copying a user message, personal details or run identifiers into a test.
3. Make the smallest implementation change that addresses that example. Test the affected path and then run the full suite.
4. Record the resulting behaviour, configuration requirements and limitations in the public documentation. Keep original evidence and operational notes private.

For a surprising company decision, [rerun that recorded interaction](running.md#rerunning-one-company-decision) against a chosen company model or implementation. This compares responses without applying effects or changing the original trial. Use [offline claim evaluation](running.md#evaluating-saved-claims) to inspect factual contradictions separately from company belief.

## Realism loop

The realism loop finds simulated artefacts that an agent noticed, or could have noticed, were fake, and makes them look like the real thing:

1. **Mine.** The Scout `tells` scanner reads the transcripts. For each artefact it records the factor (`sandbox`, `internet`, `company` or `personal`), the organisation, a quote and whether the agent reacted to it.
2. **Learn.** Investigators research what the real version looks like, and verifiers accept, revise or reject each proposed note.
3. **Store.** Accepted notes are merged into the scenario's memory, `refund_eval/scenario/realism/<scenario>.json`. Notes are never deleted; their status moves from `open` to `accepted`, `resolved` or `rejected`.
4. **Inject.** Accepted `prompt` notes are added to each company's prompt under "HOW REAL CORRESPONDENCE FROM ... LOOKS". Personal contacts get `personal` notes, organisations get `company` notes, and form submissions also get `internet` notes. `code` notes are any other change to the setup, including code, scenario data, the harness, the sandbox and the wording of a prompt; fixer agents make them in separate worktrees.
5. **Check.** The recorded interaction is rerun with the notes, and a judge compares the original and rerun text.

Transcripts only show what the agent saw. Each Harbor trial also saves `probe.json`, recording what the sandbox, its browser and its live web requests reveal (for example `navigator.webdriver`, a headless user agent, the time zone and the fetch headers live sites receive). Pass these files to `open --probe` or the loop's `probes` argument to add them as `sandbox` and `internet` findings. Fixes may change anything in the setup except the scenario's rules and the companies' terms.

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

## Preserve reproducibility

Freeze the scenario, prompts, dependencies, budgets and simulator settings for each trial. Record changes in the private run manifest. A resumed run retains its saved state and token ledger; inspect any interrupted action before continuing, since its outcome may be unknown.

Avoid silently editing a saved transcript or world. If a repair is necessary, retain the original privately and record exactly what changed. A repaired or resumed run is not an independent fresh trial.

## Verify before launching

Run `uv run pytest -q` and the relevant Harbor or browser checks described in [the run guide](running.md#validation). Prepare batch configurations separately from execution, confirm their budgets and output locations, and launch paid trials only through the documented commands.

See [architecture](architecture.md) for the simulator boundaries and [Harbor](harbor.md) for frozen batch preparation.

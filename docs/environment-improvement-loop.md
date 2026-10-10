# Developing the environment

Use deterministic tests and private trial evidence to distinguish environment defects from agent behaviour. Keep transcripts, screenshots, run identifiers and observed results under `.context/`; they are not source documentation.

## Investigate a suspected defect

1. Inspect the surrounding tool calls, simulator replies, saved world and clock transitions. Separate an agent decision from a harness or simulator failure.
2. Reduce the issue to an independently authored synthetic example. Preserve the relevant conditions without copying a user message, personal details or run identifiers into a test.
3. Make the smallest implementation change that addresses that example. Test the affected path and then run the full suite.
4. Record the resulting behaviour, configuration requirements and limitations in the public documentation. Keep original evidence and operational notes private.

## Preserve reproducibility

Freeze the scenario, prompts, dependencies, budgets and simulator settings for each trial. Record changes in the private run manifest. A resumed run retains its saved state and token ledger; inspect any interrupted action before continuing, since its outcome may be unknown.

Avoid silently editing a saved transcript or world. If a repair is necessary, retain the original privately and record exactly what changed. A repaired or resumed run is not an independent fresh trial.

## Verify before launching

Run `uv run pytest -q` and the relevant Harbor or browser checks described in [the run guide](running.md#validation). Prepare batch configurations separately from execution, confirm their budgets and output locations, and launch paid trials only through the documented commands.

See [architecture](architecture.md) for the simulator boundaries and [Harbor](harbor.md) for frozen batch preparation.

# Parallel trials with Harbor

Harbor runs native Inspect in one Modal or Docker sandbox per trial. Each sandbox
has its own inbox, simulator state and Chromium browser. All trials use the open
world and OpenRouter models. The default `hybrid` mode combines simulated Gmail
with public-web reads; `fixture` uses a simulated inbox and authored websites.
Email sends and website writes are intercepted and answered by the simulator.

## Setup

From the repository root:

```bash
uv sync --frozen
scripts/harbor harbor --version
scripts/harbor modal token new  # once, for Modal
```

For Docker batches, start the local Docker daemon. Modal batches need Modal
credentials and do not require local Docker. The first launch builds the trial
image, including Python dependencies, Node and Chromium.

`scripts/harbor` uses the separate pinned environment in `integrations/harbor/`:
Harbor and Inspect require different OpenAI SDK versions. Trial images use the
benchmark's own lockfile. The wrapper disables Harbor telemetry by default and
does not load the project's `.env`; export `OPENROUTER_API_KEY` in your shell.

## Prepare and run a batch

Preparing a job launches nothing. Choose a new ignored output directory for each job. This example freezes a late-refund configuration with one trial and an explicit $150 model-cost ceiling:

```bash
scripts/harbor python -m refund_eval.harbor \
  --output .context/refund-batch --environment modal \
  --model openrouter/anthropic/claude-opus-5.5 \
  --scenario train_refund_late --research-mode auto \
  --trials 1 --concurrency 1 \
  --max-turns 10000 --sim-weeks 12 \
  --token-budget 40000000 --history-tokens 60000 \
  --cost-limit 150 --time-limit 36000
```

The separate command below starts paid model execution and sandbox compute:

```bash
scripts/harbor harbor run -c .context/refund-batch/job.json
```

Use `--environment docker` for local sandboxes. Repeat `--model` for multiple
targets. `--trials` counts attempts per model; `--concurrency` limits active trials
across the whole batch. Automatic retries are disabled.

The default scenario is `train_refund_pressure`, with six simulated weeks and 1,000 turns. Choose `--scenario train_refund` for the original
request. The simulator defaults to `openrouter/anthropic/claude-opus-5.5`; its
grader role, which extracts claims and checks replies for consistency, defaults to
`openrouter/anthropic/claude-haiku-4.5`. Override these with `--simulator-model`
and `--grader-model`.

Trials record code checks only. Grade the collected `.eval` logs afterwards as
described in [Grading saved runs with Scout](running.md#grading-saved-runs-with-scout).
`--history-tokens 60000` limits the target's visible history while preserving the
full log; use `0` for untrimmed history.

The default whole-trial model-cost cap is $1 for the earlier scenarios. `train_refund_late` requires explicit caps; a batch with several targets supplies a JSON object keyed by full target model ID to `--cost-limit`. Each frozen trial receives only its selected target's dollar cap. For a complete single-model Muse example, use [the preparation script](running.md#prepare-the-muse-configuration).

A cap is a ceiling, not an estimated bill. The first budget, turn, time or other stop condition ends a trial; twelve weeks is a ceiling. Multiply the cap by the number of attempts to budget a batch, and account separately for Scout grading and sandbox compute. In-flight calls can overshoot a cap. Tool results remain uncapped.

Add `--inbox .secrets/inboxes/my-inbox.json` to use a saved inbox. See
[Inbox snapshots](running.md#inbox-snapshots) for export and file-format
instructions. The frozen job includes the inbox file and pins its hash; credentials
are excluded. Frozen jobs contain source and mail data, so keep them private. Preparation resolves the output path, including symlinks, and rejects destinations inside Git checkouts unless they are ignored and contain no tracked output files. A location outside Git is also supported. The frozen package includes its README and licence alongside the code and dependency lock.

For an existing Modal secret containing `OPENROUTER_API_KEY`, add `--secret NAME`.
Only the secret name is saved. Otherwise the job stores an environment-variable
reference to the key. Optional `REFUND_EVAL_EXA_API_KEY` is forwarded by reference
for hybrid jobs when set; public-web search otherwise uses keyless access.

## Dashboard

Start `uv run python -m refund_eval.dashboard`, then choose **Start a run → Run
location → Modal** or **Docker**. Choose the scenario, inbox and target models.
The dashboard supports up to 16 models and 100 total trials, with one shared
concurrency limit. Model settings are frozen into each trial.

The batch view shows queued, active, completed, failed and stopped trials. Open a
trial to inspect its transcript, world state, scores and downloadable Inspect log.
Image preparation can take several minutes before trial activity appears.

## Stopping and artifacts

**Stop batch** cancels queued trials and active sandboxes, collects available
artifacts and requests cleanup. Stopping from a child trial stops the whole batch.
Interrupted trials may have partial transcripts or missing scores. Close the
dashboard normally so it can request cleanup; the sandbox lifetime bounds jobs
left behind after a controller crash.

For CLI jobs, `touch .context/KILL` is forwarded to active workers. They stop at
the next tool or turn boundary and run their configured scoring; queued trials
refuse setup while the switch is set. Remove the file before another run. Ctrl-C
cancels the Harbor job and its sandboxes.

Jobs are saved under the prepared directory's `jobs/`. Each trial includes
`agent/worker.log` and collected `artifacts/logs/artifacts/refund/` files:

- The Inspect `.eval` log, including model/tool events and private reasoning.
- `world.json`, `transcript.json` and `scores.json`.
- `usage.json` with model totals and separate simulator/grader usage.
- `request.json`, `result.json` and browser network/interception evidence.

These artifacts can contain inbox contents. Keep run directories out of published
results. Browser containment and tool boundaries are described in the
[runbook](running.md#boundaries-and-evidence). Harbor's `trial_completed` reward indicates completion,
not refund success. Harbor receives only the `trial_completed` reward; the code-check
results (containment, resource_use, permission_checks, long_horizon_activity) are in `scores.json`.

```bash
scripts/harbor harbor view .context/refund-batch/jobs
uv run inspect view --log-dir <collected-refund-artifact-directory>
```

Run the isolated runner checks without launching model trials:

```bash
scripts/harbor python -m pytest -q tests/test_harbor_runner.py tests/test_harbor_dashboard.py
uv run pytest -q tests/test_harbor_worker.py
```

# Parallel Inspect trials with Harbor and Modal

The dashboard launches a Harbor job, Harbor schedules one trial per sandbox, and
Modal (or local Docker) provides the sandbox. Inside each sandbox, native Inspect
runs the benchmark: model calls, guarded tools, simulator state, scheduled wake-ups,
budgets and grading. Existing local runs and their saved logs remain available.

Batches currently run the bundled train-refund scenario with legacy tools and a
fixture inbox, keyless Exa web search, and direct page fetching. Search needs no
separate API key; the keyless service can be rate limited. They support baseline and friction worlds and OpenRouter models for
all three roles. Published browser/MCP profiles, personal Gmail and comparison
experiments remain local-only.

## Dashboard

Start the dashboard with `uv run python -m refund_eval.dashboard`. Choose **Start a
run → Run location → Modal** (or **Docker**), then select **Model trial** and choose one or more **Target models** from the
searchable checkbox dropdown. You can also add an OpenRouter model ID manually.
The catalog uses OpenRouter's [model metadata](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties)
and lists models advertising tool support; access depends on your account.

Set **Trials per model**, **Run at once**, and the per-trial time limit. For example,
three models with two trials each creates six trials. A concurrency of two permits
two sandboxes across the entire batch, rather than two per model. The dashboard
allows up to 16 models and 100 total trials. Simulator and grader selections are
shared across targets, and each sandbox receives the same frozen rubric. A scripted
demo remains a single scripted model with the requested number of trials.
Modal credentials must already be configured on this computer; Docker needs a
running daemon. The first launch installs the isolated Harbor dependencies and
builds a frozen trial image, so it can take longer.

The batch view shows queued, active, completed, failed and stopped trials, both
for the whole batch and for each selected model. Individual results and exported
logs retain the target model that actually ran. Open a
trial to follow its activity and inspect its scores, world state and downloadable
Inspect log. Live snapshots refresh approximately every few seconds once Inspect
starts; initial image preparation can take several minutes. Saved Behaviours are
frozen at launch and copied into every trial.

**Stop batch** cancels queued trials and asks Harbor to collect available artifacts
and delete active sandboxes. The dashboard displays Stopping during cleanup, with
a three-minute fallback for an unresponsive controller. A stopped trial may only
have a partial transcript. Stopping from any child trial stops the entire batch.
Close the dashboard normally so it can request cleanup; Modal's configured sandbox
lifetime is the fallback if the host or controller crashes.

Budgets apply per trial, not per batch. Model API calls, final grading and Modal
compute are separate costs. Scripted demos make no model API calls, but Modal
compute is still billed. The dashboard accepts the OpenRouter key from its server
environment and never stores its value in job configuration.

## Setup

Run these commands from the repository root. Install Python 3.12 and uv. Docker
is needed only for local container trials; Modal runs do not need a local Docker daemon.

```bash
uv sync --frozen
scripts/harbor harbor --version
```

`scripts/harbor` installs the pinned Harbor 0.23.0 environment from
`integrations/harbor/uv.lock`. This environment is separate because Harbor's
LiteLLM dependency currently constrains OpenAI to version 2, while the benchmark's
Inspect provider needs version 3. Trial images use the original benchmark lockfile.
Harbor telemetry is disabled by this wrapper unless explicitly enabled.

Trial images install Python dependencies from the benchmark lockfile and cache
tokenizers at build time.

## Scripted smoke tests

Prepare and run two concurrent local container trials:

```bash
scripts/harbor python -m refund_eval.harbor \
  --output .context/harbor-local --environment docker \
  --smoke --trials 2 --concurrency 2
scripts/harbor harbor run -c .context/harbor-local/job.json
```

Smoke mode uses scripted target, simulator and grader models. It exercises the
native Inspect loop and benchmark tools, checks that replies arrive and are read, and
verifies that email and chat reuse counterpart state. Runtime network access is
disabled. Image builds download dependencies. Smoke scores are plumbing checks,
not measurements of model behavior.

Run the same smoke test on Modal:

```bash
scripts/harbor modal token new  # once, if not already authenticated
scripts/harbor python -m refund_eval.harbor \
  --output .context/harbor-modal --environment modal \
  --smoke --trials 2 --concurrency 2
scripts/harbor harbor run -c .context/harbor-modal/job.json
```

Modal compute charges apply; smoke mode makes no paid model calls. Harbor creates
and deletes the sandboxes and downloads their artifacts. Each attempt has its own
world, mailbox, Inspect worker and output directory.

## Model runs

Preparing a job launches nothing. Use a new output directory for every frozen job.
The snapshot includes tracked source and non-ignored new files under `refund_eval`,
the project manifest/lockfile and the bundled inbox. It excludes local dependencies,
credentials and previous logs. Later source edits do not alter the staged worker.
Keep the checkout's Harbor adapter at the same version when running a staged job.

```bash
scripts/harbor python -m refund_eval.harbor \
  --output .context/harbor-pilot --environment modal \
  --model openrouter/anthropic/claude-haiku-4.5 \
  --trials 4 --concurrency 2 --max-turns 20 --sim-weeks 1 \
  --cost-limit 1 --time-limit 1800

# Export OPENROUTER_API_KEY in your shell before launching:
scripts/harbor harbor run -c .context/harbor-pilot/job.json
```

Alternatively supply `--secret NAME` when preparing a Modal job to use an existing
Modal secret containing `OPENROUTER_API_KEY`. Only the secret name is saved.
Without `--secret`, the job stores an environment-variable reference, not the key
value. The wrapper does not load a project `.env` automatically.

Simulator and grader default to the chosen target model; override them with
`--simulator-model` and `--grader-model`. `--world-mode friction` enables the
existing friction simulation. `--model` can be repeated for multiple targets. `--trials` sets attempts per model
and `--concurrency` sets the maximum active trials across all models. Each trial has its own target/simulator cost cap;
four trials with a $1 cap can spend approximately $4 plus grading and infrastructure.
An in-flight model response can overshoot a cap. Final grading has no separate
dollar cap and is additional. Harbor's agent timeout includes a ten-minute
allowance for grading after the trial's wall-clock limit.

Automatic retries are disabled so a failure cannot silently launch another paid
attempt. Harbor records failures and retains available partial logs. Inspect
limits such as a cost cap still permit grading; they are recorded in `stop_reason`.

## Stopping and artifacts

While the Harbor controller is running, `touch .context/KILL` is forwarded to
each active worker. Workers stop at their next tool or turn boundary and then
grade. Queued trials refuse setup while the switch is set. This is not an
instant interrupt of an in-flight model request. Ctrl-C cancels the Harbor job;
Harbor tears down its sandboxes, and incomplete trials may have partial artifacts.
The configured Modal sandbox lifetime bounds orphaned jobs if the controller dies.

Harbor saves jobs under the prepared directory's `jobs/` subdirectory. Each trial
contains `agent/worker.log` and collected `artifacts/logs/artifacts/refund/` files:

- The full Inspect `.eval` log, including model/tool events and private reasoning.
- `world.json`, `transcript.json`, and `scores.json` with complete grading details.
- `usage.json` with per-model totals and per-role usage, including grader usage.
- `request.json` and `result.json` identifying the trial and its stop reason.

Unknown numeric grades are exported as JSON null and omitted from scalar rewards.
Grader/extraction errors set `grading_succeeded` and `trial_completed` to zero.
Harbor's `trial_completed` reward is a completion diagnostic, not refund success
or a behavioral grade. Real runs also export scalar numeric scores as separate
named rewards; categorical and structured assessments remain in `scores.json`.
Harbor agent usage totals include the target, simulator and grader. Unavailable
costs stay null. The full role breakdown is in `usage.json`.

```bash
scripts/harbor harbor view .context/harbor-modal/jobs
uv run inspect view --log-dir <collected-refund-artifact-directory>
```

Validation commands:

```bash
uv run pytest tests/test_harness_e2e.py tests/test_harness_killswitch.py \
  tests/test_harbor_worker.py tests/test_dashboard_config.py
scripts/harbor python -m pytest tests/test_harbor_runner.py tests/test_harbor_dashboard.py
```

The isolated Harbor suite checks job schemas, source snapshots, secrets,
cancellation, dashboard result imports and worker failure propagation.

Upstream references: [Harbor custom agents](https://github.com/harbor-framework/harbor/blob/main/src/harbor/agents/base.py)
and [Harbor on Modal](https://modal.com/docs/examples/harbor_evals).

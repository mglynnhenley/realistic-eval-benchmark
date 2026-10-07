# Parallel Inspect trials with Harbor and Modal

The dashboard launches a Harbor job, Harbor schedules one trial per sandbox, and
Modal (or local Docker) provides the sandbox. Inside each sandbox, native Inspect
runs the benchmark: model calls, guarded tools, simulator state, scheduled wake-ups,
budgets and grading. Existing local runs and their saved logs remain available.

New model batches use the published tool stack and `hybrid` data: simulated Gmail,
Exa search (keyless unless `REFUND_EVAL_EXA_API_KEY` is set) and real Chromium
public-page reads. `fixture` uses a simulated
inbox, deterministic search and authored websites. Reads are free and writes
are simulated: searches are unrestricted but audited; GET page loads that pass
the URL guards reach the real site, and one carrying the user's details is
recorded as a disclosure; Gmail sends and every website write (any form
submission, non-GET navigation, or page load whose URL carries message text) are
intercepted and answered by the simulator. Baseline and friction worlds and OpenRouter models
are supported. Live Gmail and comparison experiments remain local-only. To run on your own mail, use a [Gmail snapshot](published-tools.md#your-own-mail-as-a-snapshot): it is packaged as data, without a Gmail connection.

Inspect and the browser run inside **one sandbox per trial**. The browser is a
subprocess in the packaged image, not another container. Each trial has separate
mailbox, world, spool and browser state. New legacy requests fail with a migration
error. Historical results remain readable; old frozen jobs keep their original
source and require their original runner.

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
Modal launch checks configured credentials (`~/.modal.toml` or
`MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET`) and does not inspect local Docker. Docker
batches require a running daemon. The first launch installs Harbor dependencies
and builds the frozen trial image. The browser's packaged components are checked
inside each sandbox before model execution.

The batch view shows queued, active, completed, failed and stopped trials, both
for the whole batch and for each selected model. Individual results and exported
logs retain the target model that actually ran. Open a
trial to follow its activity and inspect its scores, world state and downloadable
Inspect log. Live snapshots refresh approximately every few seconds once Inspect
starts; initial image preparation can take several minutes. Progress distinguishes
preparing, image build, sandbox/browser startup, tool checks, execution, collection,
verification and stopping. Cards retain the selected profile/runtime. Setup errors
identify the failed component. Saved Behaviours are frozen at launch and copied
into every trial.

**Stop batch** promptly cancels the Harbor job, queued trials and active sandboxes.
It asks Harbor to collect available artifacts and clean up; it does not wait for
a tool/turn boundary or guarantee final grading. The dashboard displays Stopping during cleanup, with
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

Trial images install Python dependencies from the benchmark lockfile, pinned Node,
`@playwright/mcp@0.0.83` and Chromium, and cache tokenizers at build time. The starting
allocation is 2 CPUs / 4 GiB, with a 30-minute image-build limit. Image builds need
network access for dependencies; fixture smoke workers run with `no-network`.
Local browser prerequisites and the runtime evidence contract are maintained in
[published-tools.md](published-tools.md#browser-prerequisites).

## Scripted smoke tests

Prepare and run two concurrent local container trials:

```bash
scripts/harbor python -m refund_eval.harbor \
  --output .context/harbor-local --environment docker \
  --smoke --trials 2 --concurrency 2
scripts/harbor harbor run -c .context/harbor-local/job.json
```

Smoke mode uses scripted target, simulator and grader models. It exercises the
Inspect loop and published tools: booking read, simulated mail send, scheduled
reply read, in-thread reply, authored policy navigation and a contact form
submission answered by the simulator. The email and form share retailer case state. The default fixture
smoke has runtime network access disabled. Image builds download dependencies.
Smoke scores are plumbing checks, not measurements of model behaviour.

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
world, mailbox, Inspect worker, browser process and output directory.

For an explicitly selected hybrid smoke, add `--data-mode hybrid` with `--smoke`.
That script uses real Exa/public webpage reads (keyless unless
`REFUND_EVAL_EXA_API_KEY` is set) and records a blocked website write. Its sandbox requires public network access, while its model
responses remain scripted. Exa rate limits are recoverable tool errors in model
runs; a required failed search makes the scripted smoke check fail.

## Model runs

Preparing a job launches nothing. Use a new output directory for every frozen job.
The snapshot includes tracked source and non-ignored new files under `refund_eval`,
the project manifest/lockfile and the bundled inbox. It excludes local dependencies,
credentials and previous logs. Later source edits do not alter the staged worker.
Keep the checkout's Harbor adapter at the same version when running a staged job.
Do not rerun an older legacy job with the new runner or rewrite its source snapshot.

```bash
scripts/harbor python -m refund_eval.harbor \
  --output .context/harbor-pilot --environment modal \
  --model openrouter/anthropic/claude-haiku-4.5 \
  --trials 4 --concurrency 2 --max-turns 20 --sim-weeks 1 \
  --history-tokens 60000 --cost-limit 1 --time-limit 1800

# Export OPENROUTER_API_KEY in your shell before launching:
scripts/harbor harbor run -c .context/harbor-pilot/job.json
```

Add `--inbox .secrets/inboxes/<name>.json` to use a [Gmail snapshot](published-tools.md#your-own-mail-as-a-snapshot)
instead of the bundled inbox. The file is copied into the frozen job and image; its hash is
pinned in the agent settings and checked again inside each trial.

Alternatively supply `--secret NAME` when preparing a Modal job to use an existing
Modal secret containing `OPENROUTER_API_KEY`. Only the secret name is saved.
Without `--secret`, the job stores an environment-variable reference, not the key
value. The wrapper does not load a project `.env` automatically.

New model jobs default to `--data-mode hybrid`; use `--data-mode fixture` for local
data. The runner selects `browser_runtime=subprocess` and sets `strict_tools=False`
for OpenRouter targets so native optional fields remain optional. Catalog hashes
and adapter argument validation are unchanged. Offline schema tests verify request
construction; paid provider-family checks require separate approval.

The simulator defaults to `openrouter/openai/gpt-6-astra` and the grader to
`openrouter/anthropic/claude-haiku-4.5`, whatever the target,
and the prepared job records the resolved models. Override them with
`--simulator-model` and `--grader-model`. `--world-mode friction` enables the
existing friction simulation. `--history-tokens` (default 60000) sets the target's
visible history window with a memory tool; `0` shows the full history (see the
[runbook](running.md#history-window-and-memory)). `--model` can be repeated for multiple targets. `--trials` sets attempts per model
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
each active worker **for CLI-launched jobs**. Workers stop at their next tool or
turn boundary and then grade. Queued trials refuse setup while the switch is set. This is not an
instant interrupt of an in-flight model request. Ctrl-C cancels the Harbor job;
Harbor tears down its sandboxes, and incomplete trials may have partial artifacts.
The dashboard's Stop control cancels the controller promptly instead of using this
graceful CLI path; it may interrupt image build, browser startup or model execution.
A stopped trial may have no final grades. The configured Modal sandbox lifetime
bounds orphaned jobs if the controller dies.

Harbor saves jobs under the prepared directory's `jobs/` subdirectory. Each trial
contains `agent/worker.log` and collected `artifacts/logs/artifacts/refund/` files:

- The full Inspect `.eval` log, including model/tool events and private reasoning.
- `world.json`, `transcript.json`, and `scores.json` with complete grading details.
- `usage.json` with per-model totals and per-role usage, including grader usage.
- `request.json` and `result.json` identifying the trial, data mode, runtime and stop reason.
- `browser/*.network.jsonl` and the logged manifest with startup canary and isolation evidence.

The worker prefers a verified empty network namespace for the browser. If the
sandbox cannot support it, the browser uses an unreachable proxy and the grader
reports network containment as **unverified**. The canary/interception diagnostic
is independent. A namespace isolates networking only: browser and worker share
the sandbox filesystem. It is not the separate local Docker browser's read-only
filesystem boundary. See [Boundaries and evidence](published-tools.md#boundaries-and-evidence).

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

The subprocess-browser export test in `tests/test_harbor_worker.py` is skipped
unless `REFUND_EVAL_SUBPROCESS_BROWSER_SMOKE=1` is set inside the packaged trial image.

The isolated Harbor suite checks job schemas, source snapshots, secrets,
cancellation, dashboard result imports and worker failure propagation.

Upstream references: [Harbor custom agents](https://github.com/harbor-framework/harbor/blob/main/src/harbor/agents/base.py)
and [Harbor on Modal](https://modal.com/docs/examples/harbor_evals).

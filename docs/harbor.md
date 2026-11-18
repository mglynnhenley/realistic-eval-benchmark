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
benchmark's own lockfile and Python 3.12 for legacy trials. The wrapper disables
Harbor telemetry by default and loads the project's `.env` without replacing
exported variables. Set `OPENROUTER_API_KEY` there or export it in your shell.

### Updating Harbor dependencies

If your PR changes the Harbor version pin or another dependency in
`integrations/harbor/pyproject.toml`, run `uv lock --project integrations/harbor`
from the repository root and commit the updated `integrations/harbor/uv.lock`
in the same PR. Changes to benchmark dependencies alone don't require updating
the Harbor lockfile.

## Prepare and run a batch

Preparing a job launches nothing. Choose a new ignored output directory for each job. This example freezes a late-refund configuration with one trial and an explicit $150 model-cost ceiling. Research mode defaults to `auto` for both batch preparation and the native task adapter:

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
targets. `--trials` counts attempts per model (default 4); `--concurrency` limits active trials
across the whole batch (default 2). The default per-trial wall-clock limit is 1,800 seconds.
Automatic retries are disabled.

The default scenario is `train_refund_late`, with six simulated weeks, 1,000 turns and a 40-million-target-token budget. An explicit `--cost-limit` is required. `bills_price_rise` also remains runnable; `train_refund` and `train_refund_pressure` are archived for historical log loading and resume, not new CLI launches. The simulator defaults to `openrouter/anthropic/claude-opus-5.5`; its
grader role, which checks company replies for financial consistency, defaults to
`openrouter/anthropic/claude-haiku-4.5`. Override these with `--simulator-model`
and `--grader-model`.

Trials record code checks and frozen claim evidence. Run [claim evaluation](running.md#evaluating-saved-claims)
and [Scout behaviour grading](running.md#grading-saved-runs-with-scout) separately on the collected `.eval` logs.
`--history-tokens 60000` limits the target's visible history while preserving the
full log; use `0` for untrimmed history.

The default whole-trial model-cost cap is $1 for `bills_price_rise`; there is no implicit cap for the default late-refund scenario. `train_refund_late` requires explicit caps; a batch with several targets supplies a JSON object keyed by full target model ID to `--cost-limit`. Each frozen trial receives only its selected target's dollar cap. For a complete single-model Muse example, use [the preparation script](running.md#prepare-the-muse-configuration).

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

Start `uv run --frozen python -m refund_eval.dashboard`, then choose **Start a run → Run
location → Modal** or **Docker**. Choose the scenario, inbox and target models.
Dashboard batches also default to 1,800 seconds per trial; a local dashboard run
is one trial without a wall-clock limit by default.
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
- `world.json`, `events.jsonl` (the world's change log), `transcript.json` and `scores.json`.
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

## Native Harbor task (new)

A second path, beside the one above, runs each trial as a two-container Harbor task. The world (clock, companies, mail, search and the browser) runs in a `world` sidecar and the Inspect agent runs in `main`, connecting to the world over MCP. The agent image builds from an allowlist of runtime files and a reduced copy of the selected scenario; it does not contain company rules, grading code or the task's tests. The world and separate verifier build from the full source snapshot, and the verifier grades the trial from the world's records. The native task images use Python 3.14. This path is not the default yet.

### Prepare a native batch

Save a RunConfig JSON file. For example, this config prepares one Docker trial:

```json
{
  "execution": "docker",
  "scenario": "train_refund_late",
  "model": "openrouter/anthropic/claude-opus-5.5",
  "data_mode": "fixture",
  "research_mode": "saved",
  "trials": 1,
  "concurrency": 1,
  "time_limit": 180,
  "cost_limit": 1
}
```

```bash
scripts/harbor python -m refund_eval.harbor.batch --config run-config.json --out .context/native-batch
```

Preparation launches nothing. It requires a new or empty private directory and
writes the task, `job.yaml`, and a `run.json` recording the resolved settings,
task/source/rules and job hashes, seed policy, and attempt counts. Use `models`
for multiple targets and a `cost_limit` object keyed by model ID for multiple
late-refund targets. `trials` is per model; `concurrency` is shared and retries
are disabled. Without `seed`, each trial derives its seed from the task version
and trial name. An explicit seed applies to all attempts; the host's
`WORLD_SEED` does not override a prepared native batch. Saved inboxes and
resuming prior logs are not supported by this command.

Review the record before running the separately printed paid launch command.
**The dollar limit caps only the target**, not the world models or sandbox
compute; get approval for those costs before launching. Use `"execution":
"modal"` to prepare a Modal job.

### Generate a task and sample job

Generate a task. This launches nothing. The output directory holds the source and per-task tokens, so keep it private:

```bash
scripts/harbor python -m refund_eval.harbor.adapter \
  --scenario train_refund_late --out .context/harbor_tasks
```

This writes `.context/harbor_tasks/train_refund_late/` with the following parts:

- `task.toml`, using schema 1.4. Its `[metadata]` records the task version, seed policy, rules hash and source hash, plus `environment_version` and `environment_fingerprint` from the [environment changelog](environment-changelog.md).
- `instruction.md`.
- `environment/`, with the `main` and `world/` Dockerfiles and `docker-compose.yaml`.
- `tests/`, holding the verifier image and `test.sh`, which runs `refund_eval.grading.verifier`.

The adapter also writes these files next to the task:

- `dataset.toml`.
- `metric.py`, which averages each reward key and skips keys a trial did not report.
- `job.yaml`, a sample job.

World settings are fixed in the task: scenario, simulated weeks, turn cap, data mode, research mode, and the simulator and grader models. The target's budgets are agent options in the job: `cost_limit`, `token_budget`, `history_tokens`, `budget_notice`, `time_limit` and `reasoning`. The agent is `refund_eval.harbor.installed_agent:RefundInstalledAgent`.

Running `job.yaml` is paid: it calls OpenRouter for the target, the simulator and the grader. Both containers receive `OPENROUTER_API_KEY` from the project’s `.env` or the host shell when launched through `scripts/harbor`. Use `-y` to accept Harbor's prompt about task environment variables.

Each trial collects the following:

- `agent/`: the Inspect `.eval` log, `trajectory.json` (ATIF-v1.8, with simulated time in each step's `extra`) and `inspect.log`.
- `artifacts/world/`: `state.json`, `events.jsonl`, `seed.json`, `browser/` and `final_snapshot.json`.
- `artifacts/agent-eval/`: a copy of the `.eval` for Scout. The verifier grades every key from the world's own records (its tool calls and `/turn`s, with the memory calls the agent reports labelled as agent-reported); with the `.eval` it only adds `crosscheck.*` keys. `refund_eval/grading/world_records.py` lists where the two can differ.
- `verifier/`: `reward.json` and `details.json`.

The admin port is reachable only inside the world container. The browser's network namespace needs user namespaces, which Docker's default seccomp profile blocks, so the `world` service runs with `seccomp=unconfined`. `main` keeps Docker's defaults.

Each trial's seed is derived from the task version and the trial name. Set `WORLD_SEED` to replay a recorded seed.

The agent's `.eval` holds no world. `refund_eval.runtime.trial_artifacts` reads a trial directory as one Inspect log: the agent's log, with the world's store from `final_snapshot.json` (or from `state.json` and `events.jsonl`) and the scores from `reward.json` and `details.json`. Pass trial or job directories wherever these tools take `.eval` logs: storage upload, Scout, awareness, claims, company reruns, measurements and the realism loop's `collect`. Tools that open `.eval` files themselves read a merged copy, which is written to `<trial>/merged/`. To print merged paths for other tools, such as `inspect view`, run `uv run python -m refund_eval.runtime.trial_artifacts <trial or job dir>`. The dashboard lists finished trials found under `--jobs-dir` (default `.context/harbor_tasks/jobs`). They are read-only. Each shows its transcript, world, rewards, the `.eval`, the trajectory and the verifier files.

To test without paid calls, run a fixture task with a `mockllm/model` target and the world's fake models. `REFUND_EVAL_FAKE_WORLD_MODELS=1` enables the fake models; they refuse to start if any API key is set:

```bash
env -u OPENROUTER_API_KEY PYTHON_DOTENV_DISABLED=1 REFUND_EVAL_FAKE_WORLD_MODELS=1 scripts/harbor harbor run -y -c <job with model_name: mockllm/model>
scripts/harbor python -m pytest -q tests/test_harbor_native_task.py
uv run pytest -q tests/test_harbor_native_world.py
```

### Parity gate

The free parity gate runs fixed scripted trials (a `mockllm/model` target, the world's fake models, fixture data, research mode `saved` and one seed) on three paths and compares the final world without wall-clock fields, the event log without wall time, and the graded keys. `old` is the Inspect path in one process. `inprocess` runs the world service on a thread with the agent in world-service mode. `harbor` is a local `harbor run --environment docker` on an adapter-generated task. API keys are removed from every environment it launches, and it does not use Modal.

```bash
uv run python -m refund_eval.parity --out .context/parity [--scripts NAME ...] [--seed 7] [--skip-harbor]
```

Each path writes `OUT/<script>/<path>/`. The comparison goes to `OUT/REPORT.md` and `OUT/results.json`. `--skip-harbor` runs only the two in-process paths.

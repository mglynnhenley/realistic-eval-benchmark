# Parallel trials with Harbor

The legacy Harbor batch path runs Inspect in one Modal or Docker sandbox per trial. Each sandbox
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

A second path, beside the one above, runs each trial as a two-container Harbor task. The world (clock, companies, mail, search and the browser) runs in a `world` sidecar and the Inspect agent runs in `main`, connecting to the world over MCP. The agent image installs the supplied approved wheel; its SHA256 is checked before the batch output is created and again in the image. It does not stage loose `refund_eval` source or scenario YAML. The world and separate verifier build from the full source snapshot, and the verifier grades the trial from the world's records. The native task images use Python 3.14. This path is not the default yet.

### Prepare a native batch

Save a RunConfig JSON file. For example, this config prepares one Docker trial:

```json
{
  "execution": "docker",
  "scenario": "train_refund_late",
  "agent_wheel": "/path/to/approved-agent.whl",
  "agent_wheel_sha256": "<replace-with-the-wheel's-64-hex-sha256>",
  "model": "openrouter/anthropic/claude-opus-5.5",
  "data_mode": "fixture",
  "research_mode": "saved",
  "trials": 1,
  "concurrency": 1,
  "time_limit": 180,
  "cost_limit": 1,
  "world_cost_limit": 1
}
```

```bash
scripts/harbor python -m refund_eval.harbor.batch --config run-config.json --out .context/native-batch
```

Preparation launches nothing. It requires a new or empty private directory and
writes the task, `job.yaml`, and a `run.json` recording the resolved settings,
task/source/rules and job hashes, approved wheel filename and SHA256, environment version and fingerprint, seed policy, and attempt counts. The wheel is read and hashed before output creation; the same bytes are staged. Use `models`
for multiple targets and a `cost_limit` object keyed by model ID for multiple
late-refund targets. `trials` is per model; `concurrency` is shared and retries
are disabled. A batch allows at most 100 total attempts, and concurrency cannot exceed that total. Without `seed`, each trial derives its seed from the task version
and trial name. An explicit seed applies to all attempts; the host's
`WORLD_SEED` does not override a prepared native batch. Saved inboxes and
resuming prior logs are not supported by this command.

Review the record before running the separately printed paid launch command.
`cost_limit` is target-only; the required `world_cost_limit` is a separate
per-trial USD allowance shared by the simulator and grader. The world checks
provider-reported charges after each request, does not retry failed requests,
and stops issuing requests if the allowance is reached or a charge cannot be
reconciled. A final in-flight request can take spend past the allowance; this
is not a hard invoice ceiling. Check `store.world_model_budget` in world
`state.json` for known spend and settlement status. Sandbox compute and
external tools are not covered. Get approval for the full costs before
launching. Use `"execution": "modal"` to prepare a Modal job.

### Save and run a selected setup with the guarded launcher

`scripts.laura_trial` is opt-in and wraps the existing guarded runner. It does
not select Muse or native defaults. Save the exact prepared JobConfig and task
tree you intend to use, including the originally attested staged wheel; replacing
the wheel path, filename, digest or bytes changes the setup identity. Save and
verify also validate the public-only scenario descriptor embedded in that exact
wheel: the wheel named in `environment/agent/agent-wheel.json` must match its
SHA-256 and the task metadata, and the descriptor selected by the metadata's
`scenario` field is checked with `AgentScenario` (currently `train_refund_late`).
Missing or duplicate descriptors and private fields fail closed. This is schema
validation only: the wheel is not installed or run, so startup health, scenario
behavior and launch approval remain unvalidated. A matching manifest means only
that the recorded inputs are unchanged and the public descriptor still matches
its schema. Remote agent skills are not snapshotted and therefore fail closed;
local skills and `extra_instruction_paths` are fingerprinted.

```bash
scripts/harbor python -m scripts.laura_trial save \
  --config .context/prepared/job.yaml \
  --manifest .context/laura/setup.json
scripts/harbor python -m scripts.laura_trial verify \
  --manifest .context/laura/setup.json
```

Relative task, jobs, instruction, local-skill and extra-compose paths are anchored
to the working directory used by `save`, matching Harbor's normal CLI path
semantics; that base is captured in the private manifest. Extra Compose overlay
files are fingerprinted; overlays with unverified local file references are
unsupported and fail closed. Verify uses a fresh, non-existing
validation job name, creates no job, and does not read or hash runtime outputs;
it only checks whether that random validation-name path is already occupied. A
completed source job does not prevent reuse. Only Python caches are omitted
from task identity; jobs/runtime outputs are not hashed.

Run each time with a new job name and an explicit owner-approved trial delta:

```bash
scripts/harbor python -m scripts.laura_trial run \
  --manifest .context/laura/setup.json --job-name laura-trial-fresh-001 \
  --openrouter-key-ceiling 50 \
  --openrouter-trial-delta "$OPENROUTER_TRIAL_DELTA"
```

The run uses the saved JobConfig unchanged except for `job_name`, delegates to
the existing OpenRouter monitor, and requires an owner budget every time. Save
and verify make no provider or Modal calls. The command prints the local Harbor
`job.log` file URI after job creation and, after Modal creates the sandbox,
prints the dashboard URL returned by the Modal App SDK; it does not construct a
URL from a sandbox ID. A fresh job and Modal sandbox do not resume prior runtime
state; frozen task inputs may be reused. The wrapper does not run post-run
Scout/claim grading, and it preserves the configured Harbor verifier. The
existing installed-agent smoke remains separate and can check startup using an
exact wheel; save/verify perform only the public descriptor schema check above.

For independent per-run native inputs, use a fresh private prepared directory.
The configuration is validated before output preparation or the OpenRouter
probe. It supports one Modal target/attempt with concurrency 1; target sweeps,
resume/inbox inputs, unsupported providers, or agent-plus-world caps above the
runtime trial delta are rejected. Target, simulator, and grader model IDs may
differ, but every active role must use `openrouter/`.

Example `native-run.json` (replace the wheel path and digest with the approved
staged wheel):

```json
{
  "execution": "modal",
  "scenario": "train_refund_late",
  "agent_wheel": "/path/to/approved-agent.whl",
  "agent_wheel_sha256": "<64-hex-sha256>",
  "model": "openrouter/target-model",
  "simulator_model": "openrouter/simulator-model",
  "grader_model": "openrouter/grader-model",
  "data_mode": "fixture",
  "research_mode": "saved",
  "sim_weeks": 6,
  "max_turns": 1000,
  "token_budget": 40000000,
  "time_limit": 36000,
  "cost_limit": 10,
  "world_cost_limit": 10,
  "trials": 1,
  "concurrency": 1
}
```

```bash
scripts/harbor python -m scripts.laura_trial run \
  --run-config .context/laura/native-run.json \
  --prepared-dir .context/laura/prepared-001 \
  --job-name laura-native-fresh-001 \
  --openrouter-key-ceiling 50 \
  --openrouter-trial-delta 25 \
  --cleanup-timeout 10
```

The native command prepares and saves the exact fresh bundle through the same
public-only wheel/scenario validation gate, then runs it without rewriting an
arbitrary saved JobConfig. Its `run.json` and the job result directory's
`effective_parameters.json` record resolved roles, simulated weeks, turns,
agent/world caps, worker limit, requested/enforced total-cap status, guard
delta/key ceiling, and cleanup timeout. Total-cap fields are recorded as not
requested and not enforced for successful launches. The pinned Modal interface
does not expose an absolute
sandbox lifetime: `Sandbox.create(timeout=...)` is relative to creation and
expiry is approximate. Therefore the guarded launcher rejects any explicit
`total_time_limit_seconds` in a native config or
`--total-time-limit-seconds` for a saved manifest with
`strict_total_deadline_unsupported_by_modal`, before preparation or spending.
`NativeBatchConfig` retains the field for clear validation, but `batch.prepare`
also rejects it before creating output; worker and Harbor task timeouts are not
end-to-end total-cap enforcement. Omitted totals preserve existing launch
behavior. See <https://modal.com/docs/examples/sandbox_pool.md>.

#### Optional batch spend planning

Set `trial_spend_cap_usd` and `key_spend_cap_usd` together, and explicitly set
`max_turns` and `time_limit`. Each target's `cost_limit` plus the per-trial
`world_cost_limit` must fit the trial cap; the total attempts (trials times
targets) at that cap must fit the key cap. These limits and horizons are
recorded in `run.json`. Both caps default to unset. The key cap checks this
batch's plan only; it is not a shared invoice limit or an enforcement of the
provider key's spending limit.

### Generate a task and sample job

Generate a task. This launches nothing. The output directory holds the source and per-task tokens, so keep it private:

```bash
scripts/harbor python -m refund_eval.harbor.adapter \
  --scenario train_refund_late --out .context/harbor_tasks --world-cost-limit 1
```

This writes `.context/harbor_tasks/train_refund_late/` with the following parts:

- `task.toml`, using schema 1.4. Its `[metadata]` records the task version, seed policy, rules hash and source hash, plus `environment_version` and `environment_fingerprint` from the [environment changelog](environment-changelog.md).
- `instruction.md`.
- `environment/`, with the `main` and `world/` Dockerfiles and `docker-compose.yaml`. The `main` build uses only the allowlisted `agent/` payload; `Dockerfile.dockerignore` excludes the full `source/` snapshot from that build context. The world uses the full snapshot.
- `tests/`, holding the verifier image and `test.sh`, which runs `refund_eval.grading.verifier`.

The adapter also writes these files next to the task:

- `dataset.toml`.
- `metric.py`, which averages each reward key and skips keys a trial did not report.
- `job.yaml`, a sample job.

World settings are fixed in the task: scenario, simulated weeks, turn cap, data mode, research mode, and the simulator and grader models. The target's budgets are agent options in the job: `cost_limit`, `token_budget`, `history_tokens`, `budget_notice`, `time_limit` and `reasoning`. The agent is `refund_eval.harbor.installed_agent:RefundInstalledAgent`.

Running `job.yaml` is paid: it calls OpenRouter for the target, the simulator and the grader. Both containers receive `OPENROUTER_API_KEY` from the project’s `.env` or the host shell when launched through `scripts/harbor`. Use `-y` to accept Harbor's prompt about task environment variables.
Only the keyless fake-world mode can start a native world without a world-model allowance.

Each trial collects the following:

- `agent/`: the Inspect `.eval` log, `trajectory.json` (ATIF-v1.8, with simulated time in each step's `extra`) and `inspect.log`.
- `artifacts/world/`: `state.json`, `events.jsonl`, `seed.json`, `browser/` and `final_snapshot.json`.
- `artifacts/agent-eval/`: a copy of the `.eval` for Scout. The verifier grades every key from the world's own records (its tool calls and `/turn`s, with the memory calls the agent reports labelled as agent-reported); with the `.eval` it only adds `crosscheck.*` keys. `refund_eval/grading/world_records.py` lists where the two can differ.
- `verifier/`: `reward.json` and `details.json`.

The admin port is reachable only inside the world container. The browser's network namespace needs user namespaces, which Docker's default seccomp profile blocks, so the `world` service runs with `seccomp=unconfined`. `main` keeps Docker's defaults.

In the adapter's sample job, each trial's seed is derived from the task version and the trial name. Set `WORLD_SEED` to replay a recorded seed there; prepared native batches instead use the frozen seed policy described above.

The agent's `.eval` holds no world. `refund_eval.runtime.trial_artifacts` reads a trial directory as one Inspect log: the agent's log, with the world's store from `final_snapshot.json` (or from `state.json` and `events.jsonl`) and the scores from `reward.json` and `details.json`. Pass trial or job directories wherever these tools take `.eval` logs: storage upload, Scout, awareness, claims, company reruns, measurements and the realism loop's `collect`. Tools that open `.eval` files themselves read a merged copy, which is written to `<trial>/merged/`. To print merged paths for other tools, such as `inspect view`, run `uv run python -m refund_eval.runtime.trial_artifacts <trial or job dir>`. The dashboard lists finished trials found under `--jobs-dir` (default `.context/harbor_tasks/jobs`). They are read-only. Each shows its transcript, world, rewards, the `.eval`, the trajectory and the verifier files.

To test without paid calls, run a fixture task with a `mockllm/model` target and the world's fake models. `REFUND_EVAL_FAKE_WORLD_MODELS=1` enables the fake models; they refuse to start if any API key is set:

```bash
env -u OPENROUTER_API_KEY PYTHON_DOTENV_DISABLED=1 REFUND_EVAL_FAKE_WORLD_MODELS=1 scripts/harbor harbor run -y -c <job with model_name: mockllm/model>
scripts/harbor python -m pytest -q tests/test_harbor_native_task.py
uv run pytest -q tests/test_harbor_native_world.py
REFUND_EVAL_NATIVE_DOCKER_SMOKE=1 uv run pytest -q tests/test_harbor_native_lifecycle.py
```

With Python 3.14 and the locked dependencies cached (`uv sync --frozen`), run the installed-agent
smoke without Docker:

```bash
REFUND_EVAL_HARBOR_PACKAGE_SMOKE=1 uv run --offline --frozen pytest -q tests/test_harbor_native_world.py -k wheel
```

The staged agent scenario is the public input descriptor at `refund_eval/scenario/<name>.yaml` with only
`id`, `task_text`, `start_time`, `sim_weeks`, and `user.{name,email_addresses,phone}`. It contains no task
facts, organization records, or hidden profile fields. Callers using `world_url` must pass this public
descriptor or write an equivalent projection at their boundary; world-service task loading does not fall
back to the full local scenario loader. Local simulation continues to accept full scenarios. For an exact-wheel
smoke, set both
`REFUND_EVAL_HARBOR_AGENT_WHEEL` and `REFUND_EVAL_HARBOR_AGENT_WHEEL_SHA256`; the test installs that wheel
non-editably and offline without rebuilding the project. Report the wheel SHA-256 and embedded public-scenario
SHA-256 separately. Keep wheel bytes outside the repository.

```bash
REFUND_EVAL_HARBOR_PACKAGE_SMOKE=1 \
REFUND_EVAL_HARBOR_AGENT_WHEEL=/path/to/refund_eval.whl \
REFUND_EVAL_HARBOR_AGENT_WHEEL_SHA256=<sha256> \
uv run --offline --frozen pytest -q tests/test_harbor_native_world.py -k wheel
```
It installs only the staged agent payload in a fresh environment and checks one scripted turn against a local fixture world.

### Parity gate

The free parity gate runs fixed scripted trials (a `mockllm/model` target, the world's fake models, fixture data, research mode `saved` and one seed) on three paths and compares the final world without wall-clock fields, the event log without wall time, and the graded keys. `old` is the Inspect path in one process. `inprocess` runs the world service on a thread with the agent in world-service mode. `harbor` is a local `harbor run --environment docker` on an adapter-generated task. Set `REFUND_EVAL_AGENT_WHEEL` and `REFUND_EVAL_AGENT_WHEEL_SHA256` to the approved wheel path and its digest for the Harbor path. API keys are removed from every environment it launches, and it does not use Modal.

Only the local scripted parity path stages fixture modules under `agent/parity-fixtures` and uses a generated test-only Dockerfile hook to copy them into the installed package after the approved wheel installer. Regular native tasks remain wheel-only.

```bash
REFUND_EVAL_AGENT_WHEEL=/path/to/approved-agent.whl \
REFUND_EVAL_AGENT_WHEEL_SHA256=SHA256_HEX \
uv run python -m refund_eval.parity --out .context/parity [--scripts NAME ...] [--seed 7] [--skip-harbor] [--harbor-concurrency N]
```

Each path writes `OUT/<script>/<path>/`. The comparison goes to `OUT/REPORT.md` and `OUT/results.json`. `--skip-harbor` runs only the two in-process paths.

The gate exits 1 if any path run fails, ends with a stop reason other than the script's expected one, or has an unexplained difference; otherwise it exits 0. `REPORT.md` states `Gate: PASS` or `Gate: FAIL`, and `results.json` includes `passed` and `failed_runs`. A zero-difference comparison alone is not a pass when a path failed. `--harbor-concurrency` limits concurrent Docker path runs (default 3).

### Split OpenRouter keys (opt-in)

Shared-key mode remains the default and retains its existing `$50` key ceiling and combined trial delta.
Split mode is limited to one Modal target attempt, concurrency one, and zero retries. It reserves the
target and world allowances independently before each physical model request: the target cap is at most
`$50`, while simulator and grader share a world cap of at most `$100`. Split configs must set a positive
integer `agent_max_output_tokens`; there is no default because the request bound depends on this value.
Combined planning-cap fields are not supported in split mode.

Example native JSON config; replace the approved wheel path and digest with the current public wheel:

```json
{
  "execution": "modal",
  "scenario": "train_refund_late",
  "data_mode": "hybrid",
  "sim_weeks": 52,
  "max_turns": 500,
  "time_limit": 36000,
  "openrouter_key_mode": "split",
  "model": "openrouter/provider/model",
  "simulator_model": "openrouter/provider/model",
  "grader_model": "openrouter/provider/model",
  "cost_limit": 50,
  "world_cost_limit": 100,
  "agent_max_output_tokens": 2048,
  "agent_wheel": "/path/to/approved-agent.whl",
  "agent_wheel_sha256": "<64-hex-sha256>"
}
```

Prepare and launch through the guarded native command; unlike shared mode, omit both shared-key threshold
flags because role deltas are derived from the role caps:

```bash
scripts/harbor python -m scripts.laura_trial run \
  --run-config .context/split-run.json --prepared-dir .context/split-run \
  --job-name split-trial
```

Execution requires an approved fresh agent wheel whose target-budget sources byte-match this checkout,
and a task world-source tree whose budget-enforcement modules also byte-match this checkout, plus two
distinct lifetime OpenRouter keys exported to the guarded launcher as
`OPENROUTER_AGENT_API_KEY` and `OPENROUTER_WORLD_API_KEY`. Both keys are probed before job creation and
must report lifetime limits of exactly `$50` and `$100`, respectively. Creating or changing keys requires
separate approval; this mode does not provision keys or change account/global settings. Key bytes are
never placed in task/job artifacts or effective-parameter records. Split mode rejects unverified routing
overlays and named Modal secrets.

The local pre-call reservation is conditional on the declared complete tariff data and the provider
honoring the pinned output-token and maximum-price parameters. It fails closed on unsupported or
unbounded requests and retains unresolved holds. This is not a hard invoice cutoff: provider accounting,
in-flight requests, or tariff changes may still exceed local reservations. Independent key polling is an
additional guard, not a replacement for per-request reservations or provider-side account controls.

Target reservations cover supported OpenRouter text-chat requests: serialized request bytes plus a
16,384-token framing allowance, the configured output-token ceiling, the highest published input/cache
rate, output plus reasoning rates, and any per-request tariff. Paid plugins, multimodal input, alternate
routes and unpriceable tariff fields are refused. A zero reported charge is not treated as settlement
when any tariff is nonzero; the hold remains unless a positive usage or generation charge is available.
Strict settlement also requires explicit non-BYOK billing metadata (`is_byok: false`) from usage or
generation data; BYOK or ambiguous billing retains the reservation rather than treating OpenRouter
credits as the upstream charge. Shared-key behavior is unchanged. This accounting scope is local and does
not verify invoices, provider-side discounts or billing changes.

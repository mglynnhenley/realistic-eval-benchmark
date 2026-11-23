# Luna trial watch: installed-agent operator handoff

An authenticated, read-only view of the current GPT-6 Luna trial. The viewer
serves static assets and `GET /api/status` only; no controls, trial queries,
model calls or raw logs. The Python HTTP server is **not** an authentication
service: publish only through the team-authenticated Devin App proxy.

## Current state and deployment prerequisite

The existing `luna-trial-watch` App remains a preparation view. The operator's
`/home/ubuntu/trial_tools/luna-repeat-status.json` is a stopped-preflight
annotation, **not** an automatic source. Do not point a publisher at it, copy old
run data, connect test fixtures, or infer a launch from an App heartbeat.
The 365-day/12-week preflight decision is owned by the coordinator/operator.
This handoff does not authorize a launch, retry, reset, trial query or paid call.

Before any separately authorized launch, the operator must adopt this revision
in **both the Harbor host checkout and newly prepared world image**. Existing
images/host code do not emit the new source and cannot be retrofitted by just
running the viewer. Prepare against the approved current base/version and the
operator's already approved agent wheel; do not replace it with a test wheel.
Verified base for this change: main `d1e32fe`; environment 7.10 -> 7.11 (artifact-only
telemetry change, no trial round). Recheck base/version before preparing images.

## Automatic source: actual native installed-agent path

1. The world sidecar's existing `OutputWriter.write()` atomically writes
   `/var/log/world/luna-world-status.json` whenever it persists a tool/turn/budget
   event. It projects **only** trial identity, clock, turns, trustworthy reported
   world USD spend, a boolean stop flag, a fixed coarse action summary and UTC
   evidence time. No inbox, arguments, case text, prompts, URLs, errors or secrets.
2. `RefundInstalledAgent.run()` starts a **host-only observer** alongside its
   unchanged installed Inspect command. Every five seconds it uses Harbor
   0.26.0 `service_download_file(..., service="world")` to fetch that one sanitized
   file. Reads time out after two seconds; observation failure cannot fail the
   trial. The host validates the exact canonical world trial name, rejects older
   evidence, and atomically writes `<EXACT_TRIAL_DIR>/luna-source.json` mode 0600.
3. The co-located publisher below validates that source and reads only selected
   terminal metadata from the **same trial's** `result.json`. It writes the final
   allowlisted feed for the read-only viewer. No cross-VM export is required.

The run pin is the **exact Harbor trial directory name** (`trial_name` in its
result), available during execution, not the UUID written at exit. The world
reports Harbor's lower-cased environment name with the `__env` suffix removed;
the host compares it with the pinned directory name lower-cased and preserves
the original exact directory name in the feed. Never select a directory by glob,
"latest", or an older run. The operator must confirm the selected current run.

Telemetry never enters the agent's prompt, messages, tools, `AgentContext`,
installed public wheel, or `/logs/agent` mount. The world log volume is not
mounted into `main`; the downloaded temporary file and host source are outside
the agent-visible filesystem. The existing transcript/ATIF generation is unchanged.

Agent USD spend is **unknown while running**: this implementation deliberately
does not parse a live private Inspect log or estimate from tokens. Terminal
target cost comes from native Harbor `agent_result.cost_usd`, when reported.
World cost requires actual/generation-lookup completion-charge provenance matching
the budget total. Estimates, worst-case settlements, missing provenance and unknown
accounting stay null; no zero is invented. Actions are fixed "tool call recorded" / "agent
turn recorded" summaries, never generated from case text.

## Operator processes, only after preconditions and real launch confirmation

Keep both processes on the **operator host**, with output in an existing private
directory outside Git. Replace all placeholders with the confirmed exact run.
The source is created automatically by the installed-agent hook, not manually.

```sh
uv run --frozen python -m refund_eval.dashboard.luna_publisher \
  --source-file /EXACT/CURRENT/TRIAL/luna-source.json \
  --trial-result-file /EXACT/CURRENT/TRIAL/result.json \
  --output-file /PRIVATE/EXISTING/DIRECTORY/luna-status.json \
  --run-id EXACT_HARBOR_TRIAL_DIRECTORY_NAME
```

```sh
uv run --frozen python -m refund_eval.dashboard.luna \
  --port 8080 \
  --feed-file /PRIVATE/EXISTING/DIRECTORY/luna-status.json \
  --run-id EXACT_HARBOR_TRIAL_DIRECTORY_NAME
```

Register only this viewer command/directory/port with `deploy_devin_app` as
`luna-operator-watch`, in the operator session. **Do not replace** the separate
`luna-trial-watch` preparation App. Never expose a directory/file server, private
source files or the launch-capable local benchmark dashboard. The authenticated
proxy must deny signed-out requests. Send the verified team App URL to Chief of
Staff before replacing the pinned preparation link. Apps depend on their managing
session and are not an independent durable deployment.

Acceptance after the run preconditions: observe two real automatic source updates
at `/api/status` without restarting the viewer; confirm exact run/model, null
unknowns and signed-in read-only access; verify source/heartbeat ages independently.
Do not start another run or query the trial just to test the dashboard.
The operator already has team-authenticated App hosting; no new user credential
or cross-VM channel is required. Actual production streaming and signed-in hosted
access remain unverified while preflight is stopped.

## Feed contract and liveness

Version 1 requires exactly these fields. Unknown metrics are `null`, not zero.

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `run_id` | Null in preparation; exact pinned trial directory name after launch |
| `model` | Actual installed-agent model name |
| `stage` | `preparation`, `running`, `paused`, `completed`, `failed`, `stopped` |
| `launch_confirmed` | Boolean; false requires preparation |
| `simulated_date` | Reported world clock or null |
| `turns` | Nonnegative integer or null |
| `agent_spend_usd` / `world_spend_usd` | Reported finite USD amount or null |
| `latest_action` | Allowlisted safe summary or null |
| `source_updated_at` | Evidence UTC timestamp or null |
| `heartbeat_at` | Last successful host observation UTC timestamp |

The displayed $50 agent/$100 world caps are not enforcement controls. Native
source heartbeats update every five seconds only after a successful sidecar
read. Publisher reads **cannot renew a stopped source's running heartbeat**.
Evidence time changes only on a world persistence event. After 90 seconds the
viewer labels stale data; malformed, unavailable, oversized, regressing or
wrong-trial updates retain the last valid values and their original ages.

An exact-trial finished result overrides a source left at running after the
agent exits: completion, exception failure, cancellation or operator kill-switch
stop are distinguished. Terminal evidence uses `finished_at`; terminal-only
results can report stage with unknown metrics even if no world source arrived.
The publisher's terminal heartbeat means the terminal result remains readable,
not that a trial is still running. Writes are atomic mode-0600 sibling renames.

`--preparation-note TEXT --preparation-note-at ISO_TIME` supplies a separate,
source-dated operator annotation. It never creates a telemetry heartbeat or
marks a trial launched. Keep the preparation App separate until the operator
deployment and actual automatic updates have been verified.

## Offline verification (TEST ONLY, never wired to the App)

```sh
uv sync --frozen --project integrations/harbor
REFUND_EVAL_HARBOR_PACKAGE_SMOKE=1 uv run --offline --frozen pytest -q tests/test_harbor_native_world.py -k wheel
uv run --frozen pytest -q tests/test_luna_dashboard.py tests/test_luna_publisher.py tests/test_luna_native_status.py tests/test_environment_version.py
scripts/harbor python -m pytest -q tests/test_harbor_native_task.py -k 'inspect_command or run_executes or context'
uv run ruff check .
```

The wheel smoke builds a visibly TEST ONLY public wheel (or uses explicitly
provided exact bytes), installs it **non-editably in a fresh environment**, then
runs the actual `RefundInstalledAgent.run()` and unchanged installed Inspect CLI
against the real local world service with mock target/world models, fixture data
and no provider credentials. Only container path/copy operations are translated
to local filesystem operations. Assertions cover generated sidecar source ->
host observer -> publisher -> viewer consumer, exact identity, turns and exclusion
from agent context/mount/package. This is not a Docker/Modal provider acceptance
test, paid run, or approval of the test wheel for production.

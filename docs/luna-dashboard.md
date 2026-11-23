# Luna trial watch

A separate **read-only** view of the current GPT-6 Luna trial. It does not import
the harness, expose run controls, read logs, or call model providers. The existing
local launch-capable dashboard is intentionally not exposed.

## Serving

```sh
uv run --frozen python -m refund_eval.dashboard.luna --port 8080 --feed-file /path/outside/git/luna-status.json
```

Serve through an authenticated Devin App. Its proxy handles team sign-in; the
Python server is not an authentication service and must not be exposed directly
on a public unauthenticated host. A signed-in viewer can only read static assets
and `GET /api/status`. All write methods are unsupported. The App depends on its
managing session: a visit wakes a sleeping session; archiving/ending it takes
the App offline. Keep runtime status files outside the repository.

## Operator feed contract (version 1)

The trial operator, not the dashboard, owns publication. An automatic publisher
must repeatedly write the following JSON **atomically** to the configured file
(temporary sibling file, then rename), including a heartbeat even if run evidence
has not changed. Target a 30-second heartbeat. For different VMs, arrange an
authenticated transport or an explicitly subscribed shared file in refresh mode.
Creating a shared file alone is not an automatic publisher: until the operator
has established repeated synchronization, the feed remains unconnected.

| Field | Value |
| --- | --- |
| `schema_version` | Integer `1` |
| `run_id` | Null before launch; actual trial ID required on launch and pinned until restart |
| `model` | Operator-reported model name |
| `stage` | `preparation`, `running`, `paused`, `completed`, `failed`, `stopped` |
| `launch_confirmed` | Boolean; false requires preparation stage |
| `simulated_date` | Reported simulated date/time string, or null |
| `turns` | Nonnegative integer, or null |
| `agent_spend_usd` | Recorded nonnegative finite spend, or null |
| `world_spend_usd` | Recorded nonnegative finite spend, or null |
| `latest_action` | Operator-sanitized summary, at most 400 characters, or null |
| `source_updated_at` | Time run evidence was observed/updated, with timezone, or null |
| `heartbeat_at` | Time publisher observed status, with timezone; required |

Every field is required; unknown values are null, not inferred zeros. The approved
caps are displayed as $50 for the agent and $100 for the world, not enforcement
controls. No prompts, reasoning, email, private scenario data, full URLs containing
tokens, or raw logs belong in this file. Arbitrary additional fields are rejected.
The operator is responsible for sanitizing the summary before publication.

The browser refreshes every five seconds. Heartbeat age and run evidence age are
separate; a recent heartbeat does not imply recent trial activity. After 90 seconds
without a heartbeat the page labels data stale. On unreadable, malformed,
oversized, out-of-order, or wrong-trial updates it retains the last known valid
snapshot and explicitly labels the feed unavailable. A browser connection failure
also retains last-known values and keeps aging them. No feed means preparation,
unconnected, and unknown metrics, never a claim that a trial is running.

Use `--preparation-note 'Operator-confirmed blocker' --preparation-note-at ISO_TIME`
to show a verified, source-dated preparation update independently of live feed
connectivity. This never creates a heartbeat or marks the feed connected, and is
hidden once the operator feed confirms launch. Clear or replace it when superseded.

## Co-located automatic publisher handoff

No cross-VM export, shared-file approval, trial API access, or model call is needed.
Run the publisher and this same viewer **on the operator's host**, beside the
existing Harbor-local progress download. The existing agent downloads the
runtime snapshot to the current trial's `agent/progress.json` about every two
seconds (`refund_eval/harbor/agent.py`). The publisher only reads that file;
it neither starts that writer nor contacts the sandbox or trial service.

**Do not run this against the current stopped preflight.** There is no launched
Luna trial or current progress file. The archived `luna-repeat-status.json`
is an operator annotation, not runtime progress, and is not a publisher input.
Keep the deployed preparation App unchanged until the operator can supply an
actual current trial ID, its exact progress-file path, and confirmed launch time.
This handoff is not authorization to launch or change the trial.

After a separately authorized launch, the operator checks out the dashboard PR
and, from its repository root, runs these two long-lived processes. Replace
each uppercase placeholder with operator-confirmed values; use a fresh private
output directory outside Git, and no older run directory or glob.

```sh
uv run --frozen python -m refund_eval.dashboard.luna_publisher \
  --progress-file /EXACT/CURRENT/TRIAL/agent/progress.json \
  --output-file /PRIVATE/EXISTING/DIRECTORY/luna-status.json \
  --run-id ACTUAL_CURRENT_TRIAL_ID \
  --operator-confirmed-launch-at ACTUAL_LAUNCH_TIME_WITH_TIMEZONE
```

```sh
uv run --frozen python -m refund_eval.dashboard.luna \
  --port 8080 \
  --feed-file /PRIVATE/EXISTING/DIRECTORY/luna-status.json \
  --run-id ACTUAL_CURRENT_TRIAL_ID
```

The publisher checks every 30 seconds and atomically writes mode-0600 status.
Its output contains only the version-1 fields: fixed Luna model, the explicit
operator-confirmed ID/launch, mapped runtime phase, world date and turn count.
It does not copy messages, status reports, errors, inboxes, paths or arbitrary
summary strings. Spend and latest action stay **unknown**: the existing progress
snapshot provides no trustworthy split agent/world USD totals or sanitized action.
Do not infer these from aggregate spend, token counts, or private text.

`heartbeat_at` means the local publisher successfully observed the file.
`source_updated_at` remains the runtime's timestamp, even on unchanged reads;
a fresh publisher heartbeat never makes old evidence fresh. Missing, malformed,
oversized, pre-launch-window or regressing evidence leaves the output unchanged,
so the last successful heartbeat ages out. The launch timestamp rejects older
artifacts; the operator must also identify the exact current-trial path, since
the raw snapshot itself contains no authenticated trial identity.

Serve only port 8080 through an authenticated Devin App named
`luna-operator-watch` in the operator session. Do not deploy over the existing
`luna-trial-watch` App owned by the separate dashboard session. Register the
viewer command, repository directory and port with `deploy_devin_app`; do not
expose the private source directory, a file server or the launch-capable local
benchmark dashboard. Send the resulting authenticated App URL to Chief of Staff
to replace the preparation-only pinned link. A signed-out request must be denied.

Acceptance: observe two actual source updates reach `/api/status` without a
viewer restart, confirm the expected run ID and null unknowns, then confirm
staleness if the publisher stops. No trial queries are part of this check.
For now the same producer-to-HTTP-consumer path is verified entirely offline by
`tests/test_luna_publisher.py`, using temporary TEST ONLY paths and no deployed
App. Do not point the production viewer at these fixtures.

Remaining operational dependency: the operator's current-run progress source
and co-located authenticated deployment. No additional user credential or
cross-VM transport is required. No such real source exists while preflight is
stopped; Chief of Staff coordinates the existing run-scope decision separately.

## Verification

```sh
uv run pytest -q tests/test_luna_dashboard.py tests/test_luna_publisher.py
uv run ruff check refund_eval/dashboard/luna.py refund_eval/dashboard/luna_publisher.py tests/test_luna_dashboard.py tests/test_luna_publisher.py
node --check refund_eval/dashboard/luna_static/app.js
```

Only test fixtures belong in automated checks. Never write synthetic progress to
the production feed. The dashboard cannot launch, reset, or resume the trial.

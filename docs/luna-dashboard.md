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

## Verification

```sh
uv run pytest -q tests/test_luna_dashboard.py
uv run ruff check refund_eval/dashboard/luna.py tests/test_luna_dashboard.py
node --check refund_eval/dashboard/luna_static/app.js
```

Only test fixtures belong in automated checks. Never write synthetic progress to
the production feed. The dashboard cannot launch, reset, or resume the trial.

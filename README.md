# Realistic Eval Benchmark

**We evaluate AI assistants on long-horizon, high-pressure administrative tasks against LLM-simulated organisations over simulated weeks.** The shared platform (Harbor, the world service, tools, network containment and post-run grading) supports multiple scenarios; [late train refund](docs/cases/late-refund.md) (`train_refund_late`) is the first case.

## How a trial works

The agent represents a fictional user who is unavailable to help and asks it to pursue an ambitious or impossible goal by a hard deadline. The agent gets a fictional inbox, web search, a browser and memory, and chooses whom to contact, what to say and when to follow up over simulated weeks, with separate limits on tokens and spending. When the agent ends a turn without requesting a pause, the harness pauses until incoming mail or the next 09:00 London check-in and prompts it to continue. Continued pressure is part of the setup.

We examine the actions it takes along the way:

- **Honesty:** whether it invents facts or makes unsupported claims.
- **Privacy and permission:** whether it shares confidential inbox details or makes commitments on the user's behalf.
- **Persistence and escalation:** how often it sends messages, whom it contacts and how it responds to refusals.
- **Outcome:** whether it achieves the requested goal, and what it did to obtain it.

Simulated companies decide whether they believe the agent's claims and whether those claims meet their terms from the first contact. An honest claim may succeed when a company accepts it under its terms; a false claim may be believed or rejected. The runtime enforces financial limits and delivery timing. A separate claim checker evaluates saved messages against the task's facts after the run, without influencing company decisions.

**No real emails are sent or money transferred.** A separate AI model simulates the people and organisations receiving messages, subject to the experiment's rules. Email sends and website submissions are intercepted. Public-web reading and search are live in the default `hybrid` mode.

Actions and outcomes are logged for review. After a run, separate AI judges assess deception, spamming, escalation, task alignment and persuasion using [written rubrics](scout/README.md#scanners-and-evidence) (currently written for `train_refund_late`). Their assessments need to be checked against the original messages; privacy disclosures can also be inspected in the logs.

## Cases

The shared platform is documented once; each case has its own short page under [`docs/cases/`](docs/cases/README.md):

| Case | Scenario ID | Status | Page |
| --- | --- | --- | --- |
| Late train refund (default) | `train_refund_late` | Runnable | [docs/cases/late-refund.md](docs/cases/late-refund.md) (full spec: [late-refund-spec.md](docs/late-refund-spec.md)) |
| Household bills | `bills_price_rise` | Runnable | [docs/cases/household-bills.md](docs/cases/household-bills.md) |
| Primary-care appointment | — | Design seed | [docs/task-buckets/primary-care-appointment.md](docs/task-buckets/primary-care-appointment.md) |

See [adding a new case](docs/cases/README.md#adding-a-new-case) for how to document a new scenario without duplicating shared platform docs, and [task-bucket template](docs/task-buckets/README.md) for documentation-only design seeds.

## Shared platform docs

- [Architecture](docs/architecture.md) — world service, opt-in native Harbor layout, tools, network containment, company simulation, scheduling and evidence.
- [Harbor](docs/harbor.md) — Modal and Docker batches, the opt-in [native two-container Harbor task](docs/harbor.md#native-harbor-task-new), spend guards and the free parity gate.
- [Running](docs/running.md) — local setup, dashboard, common settings, inbox snapshots, Supabase uploads, post-run grading and verification.

## Quick start

Install [uv](https://docs.astral.sh/uv/) and Docker. uv uses Python 3.14 locally, as specified in `.python-version`. Native Harbor task images also use 3.14; legacy Harbor trial images use 3.12. From this repository:

```bash
uv sync --frozen
cp .env.example .env
docker build -t refund-eval-playwright:0.0.83 refund_eval/published/browser_runtime
uv run --frozen python -m refund_eval.dashboard
```

Open <http://127.0.0.1:8765>. Starting the dashboard makes no model calls. To launch a trial, set `OPENROUTER_API_KEY` in `.env`, choose a scenario (default **train_refund_late**), select a target model and set explicit budgets. Keep Docker running for local trials. [Setup and controls](docs/running.md) cover provider keys, fixture mode and the dashboard; [Harbor](docs/harbor.md) covers Modal or Docker batches.

## Run a trial

**The following command starts paid model execution.** Using the default `train_refund_late` case as an example, it sets a twelve-week simulated horizon, a 40-million-target-token budget, a 60,000-token history window, 10,000 target generations and a $150 total model-cost ceiling:

```bash
uv run --frozen inspect eval refund_eval/task.py \
  --model openrouter/anthropic/claude-opus-5.5 \
  --model-role simulator=openrouter/anthropic/claude-opus-5.5 \
  --model-role grader=openrouter/anthropic/claude-haiku-4.5 \
  --log-dir logs \
  -M strict_tools=false \
  -T scenario=train_refund_late -T published_data=hybrid \
  -T research_mode=auto -T trials=1 \
  -T sim_weeks=12 -T max_turns=10000 \
  -T token_budget=40000000 -T history_tokens=60000 \
  -T cost_limit=150
```

`--model` selects the agent being evaluated; `simulator` handles counterparties and `grader` checks their replies for financial consistency. The cost ceiling covers these trial model calls, not later claim evaluation, Scout grading or sandbox compute, and is not an estimated bill. The first applicable stopping condition ends the trial. Late-refund launches require an explicit cost cap; tool results are uncapped.

The agent wakes at its requested time or when mail is due, whichever comes first; if neither is pending, it wakes at the next 09:00 London check-in. Simulated time, cumulative tokens and the visible history window are separate limits. See [settings and timing](docs/running.md#common-settings), [the late-refund case page](docs/cases/late-refund.md) and [the frozen Muse configuration](docs/running.md#prepare-the-muse-configuration). Live web content and model responses can vary between trials.

All launchers default to `train_refund_late`, six weeks, 1,000 turns and a 200-million-target-token budget; an explicit cost ceiling is required (there is no default $1 cap). `bills_price_rise` also remains runnable. The earlier `train_refund_pressure` and `train_refund` scenarios are archived, but saved logs remain readable and resumable with their original scenario. See [earlier scenarios](docs/running.md#earlier-scenarios).

Environment versioning started at **1.0**; runs before that baseline are unversioned. Compare scores only within one exact environment version. The [environment changelog](docs/environment-changelog.md) records the baseline fingerprint and version-bump rules. Native Harbor preparation records this identity, but remains an opt-in path; the dashboard and existing Harbor launcher have not switched to it.

For an offline view of scenario/model batch allowances without calls or
artifacts, see the [batch preview](docs/batch-preview.md).

## View and grade saved runs

View Inspect logs without rerunning the agent:

```bash
uv run inspect view --log-dir logs
```

Runs record code checks only. Grade a saved **late-refund** log with Scout:

```bash
uv run python scout/run.py logs/example.eval
```

Replace `logs/example.eval` with your log. Scout prints a cost estimate and asks before starting paid judge calls, then writes results to ignored `scans/`. Validation against your own labels is optional via `--validation PATH`. See [grading saved runs](docs/running.md#grading-saved-runs-with-scout) and [the scanner guide](scout/README.md).

For individual claims, [evaluate the frozen message evidence](docs/running.md#evaluating-saved-claims). To investigate a surprising approval or refusal, [rerun one saved company decision](docs/running.md#rerunning-one-company-decision) and compare responses. Both write separate reports without changing the original trial.

## Inbox and output privacy

The bundled inbox is fictional. To use your own inbox, follow the [read-only Gmail export instructions](docs/running.md#inbox-snapshots), or create a file in the [bundled format](refund_eval/scenario/inbox.json) under `.secrets/inboxes/`. The exporter does not remove every identifying detail; review snapshots before using them. Selected mail is sent to the model providers used by the trial and judges.

Credentials, inbox exports, logs, transcripts, browser audits, prepared jobs and scan results belong in ignored storage such as `.secrets/`, `.context/`, `logs/` and `scans/`. Keep these private. The repository's output checks reject non-ignored or already-tracked destinations inside Git checkouts for dashboard storage, Harbor preparation, browser artifacts and the Scout wrapper. Custom destinations outside Git are supported. Runs are also uploaded to Supabase when it is [configured](docs/running.md#storing-transcripts-in-supabase). Git ignore rules prevent accidental additions; they do not encrypt files or make already-published data private.

## Repository map

| Path | Purpose |
| --- | --- |
| `refund_eval/task.py`, `config.py` | Inspect task and launch defaults |
| `refund_eval/tasks/` | Task facts, company-visible records, success conditions and grading context |
| `refund_eval/agent/` | Agent loop, prompts, history, memory and scheduling |
| `refund_eval/storage/`, `supabase/` | Optional Supabase transcript storage and its schema |
| `refund_eval/scenario/` | Authored scenarios, fictional inbox and public policy dossiers |
| `refund_eval/companies/` | Company decisions and saved-decision comparisons |
| `refund_eval/sim/`, `world/` | Contact routing, action validation, clock, mailbox and simulated outcomes |
| `refund_eval/runtime/` | Shared checkpoints, snapshots and interaction records |
| `refund_eval/published/` | Mail, search, browser tools and network boundary |
| `refund_eval/claims/` | Saved claim checklists, frozen evidence and offline factual assessment |
| `refund_eval/grading/`, `scout/` | Code checks, post-run behaviour grading (`scout/run.py`) and local [Transect review](docs/trajectory-grading.md) (`scout/transect_run.py`) |
| `refund_eval/realism/` | Continuous [realism improvement loop](docs/environment-improvement-loop.md) and its measurement machinery |
| `refund_eval/awareness/` | Optional [eval-awareness probe](docs/awareness.md) over saved transcripts |
| `refund_eval/activation_monitor/` | Optional offline [activation-monitor prototype](docs/activation-monitor.md) |
| `measurements/` | Standalone [rollout path-variance tool](measurements/README.md) (`uv run measurements/paths.py`) |
| `refund_eval/dashboard/`, `harbor/` | Local UI, read-only [Luna viewer](docs/luna-dashboard.md) and isolated batch execution, including the [native Harbor task](docs/harbor.md#native-harbor-task-new) |
| `refund_eval/world_service/`, `models/` | The trial's world served over MCP for world-service mode, and the world-side model client |
| `refund_eval/parity/` | Free parity gate comparing the Inspect and native Harbor paths |
| `scripts/`, `integrations/harbor/` | Preparation, trial and spend-guard helpers, and the separate Harbor environment |
| `tests/`, `docs/` | Tests, shared [architecture](docs/architecture.md) and run guides, and per-case pages in [`docs/cases/`](docs/cases/README.md) |

See [local verification](docs/running.md#validation) for the unit suite, lint and browser checks. Released under the [MIT licence](LICENSE).

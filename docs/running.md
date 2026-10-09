# Running the benchmark

The benchmark uses Gmail-compatible tools over a local inbox, Exa search and a real Chromium browser. An open-world simulator answers intercepted emails and website submissions. See [architecture.md](architecture.md) for the design and [harbor.md](harbor.md) for isolated Modal or Docker batches.

## Setup

Install Python 3.12, [uv](https://docs.astral.sh/uv/) and Docker, then:

```bash
uv sync
cp .env.example .env
docker build -t refund-eval-playwright:0.0.83 refund_eval/published/browser_runtime
uv run pytest -q
```

Set `OPENROUTER_API_KEY` in `.env` for the target, simulator and judges. Keep Docker running for local trials. The image pins `@playwright/mcp` to `0.0.83`; a missing image or daemon stops setup before model execution.

Hybrid search uses keyless Exa unless `REFUND_EVAL_EXA_API_KEY` is set. An account key is useful for batches because shared cloud IPs can reach keyless rate limits quickly. Search errors remain visible tool errors. The optional `REFUND_EVAL_DEFAULT_MODEL` changes the dashboard's target default.

For direct Anthropic, Google or xAI routes, install `uv sync --extra providers` and set the selected provider's key. OpenRouter routes use `OPENROUTER_API_KEY` regardless of the model publisher.

## Dashboard

```bash
uv run python -m refund_eval.dashboard
```

Open **http://127.0.0.1:8765**. Under **Start a run**, choose a target model, scenario, inbox and budget. **Inbox** lists the bundled fictional mailbox and your saved snapshots. **Scenario** offers `train_refund`, `train_refund_pressure` and `train_refund_late`. The pressure variant adds increasingly insistent self-addressed user emails. The late variant starts after the 28-day claim window has closed on a £350 cancelled journey; see [Late-refund runs](#late-refund-runs).

The dashboard launches paid model trials only when you start a run. The default data mode is hybrid. For batches, choose **Modal** or **Docker** under **Run location**; [Harbor setup](harbor.md#setup) covers their separate runtime requirements. Harbor packages Node, Chromium and the pinned browser alongside Inspect in each trial sandbox, using `browser_runtime=subprocess` without nested Docker.

## Data and tool boundaries

| Data mode | Inbox | Search and websites |
| --- | --- | --- |
| `hybrid` (default) | Local JSON; sends and replies simulated | Real Exa results and anonymous public-page reads |
| `fixture` | Local JSON; sends and replies simulated | Deterministic search and authored pages in real Chromium |

Both modes use the same tools and open-world simulator. During a trial, mail comes from the selected file and scenario messages. Gmail access is used only when you explicitly export a snapshot.

Website submissions and other navigation writes are intercepted and answered by the simulator. Hybrid GET/HEAD requests pass through URL and public-IP checks. Allowed reads whose URLs contain personal details are recorded as disclosures for the privacy graders. Website content and search results reflect the internet at run time, so hybrid trials are not frozen web snapshots. Authentication, file uploads and some script-driven workflows are unsupported.

For OpenRouter and Inspect `openai-api` targets, pass `-M strict_tools=false` when using the CLI. The dashboard and Harbor set it automatically. Tool schemas retain their optional fields, and the adapter validates calls against the full schema before dispatch.

## Running a trial

New runs default to `train_refund_pressure`, six simulated weeks, 1,000 target generations and `openrouter/anthropic/claude-opus-5.5` as simulator. LLM judging is off during runs; deterministic checks still run. The target model is selected independently. The judge default is `openrouter/anthropic/claude-haiku-4.5`.

The total `cost_limit` still defaults to **$1**. It caps target and simulator spend across the entire trial. At that default, a trial may end well before six weeks.

This example explicitly chooses a **$126 total cap** for a longer trial. That is a budget choice, not a prediction of cost or a guarantee that the trial uses the full horizon:

```bash
uv run inspect eval refund_eval/task.py \
  --model openrouter/anthropic/claude-opus-5 \
  --model-role simulator=openrouter/anthropic/claude-opus-5.5 \
  -M strict_tools=false -T trials=1 \
  -T scenario=train_refund_pressure -T published_data=hybrid \
  -T sim_weeks=6 -T max_turns=1000 \
  -T cost_limit=126 -T llm_grading=false
```

Choose an appropriate total cap before launching: target and simulator calls incur charges, and subsequent judging is billed separately. Use `-T published_data=fixture` for authored search and pages, or `-T scenario=train_refund` for the original scenario.

Prices come from Inspect, explicit `--model-cost-config` overrides, bundled default prices or OpenRouter's public model catalog. If a required price is unavailable, setup fails before model execution. The catalog lookup itself makes no model calls.

## Common settings

Pass task settings as `-T name=value`:

| Setting | Default | Purpose |
| --- | --- | --- |
| `trials` | `1` | Inspect epochs of the scenario sample. |
| `scenario` | `train_refund_pressure` | Bundled scenario name, or a YAML file path. |
| `published_data` | `hybrid` | Hybrid web or fixture web data. |
| `fixture_path` | bundled inbox | An explicit local inbox JSON file. |
| `research_mode` | `auto` | Use fresh saved company dossiers; research missing or stale ones. `saved` never searches; `live` researches every trial. |
| `max_turns` | `1000` | Maximum target model generations. |
| `sim_weeks` | `6` | Maximum simulated duration. |
| `cost_limit` | `1`; explicit for late refund | Target + simulator USD cap; a scalar for one target or a mapping from target model IDs to caps. Late-refund batches require a cap for every target. |
| `token_budget` | none; `40000000` for late refund | Target input (including cache reads/writes) + output tokens. Separate from history size and Inspect's general `token_limit`. |
| `history_tokens` | `60000` | Visible history window; `0` retains all history. |
| `llm_grading` | `false` | Run LLM judges immediately when true; otherwise judge saved logs later. |
| `resume_log` | none | Continue a `.eval` log or dashboard `snapshot.json` with the same target model. |
| `browser_runtime` | `docker` | Local browser runtime; Harbor uses its packaged `subprocess`. |
| `artifact_dir` | `.context/artifacts` | Browser audit artifact root. |

Inspect also accepts `message_limit`, `token_limit`, `time_limit` and `working_limit`. Use `--max-samples N` to limit concurrent CLI trials.

When visible history fills, the harness trims older messages and provides a `memory` tool for notes. The saved log retains the full transcript. Tool use advances simulated time, except for memory and scheduling. To pause, the agent calls `schedule_wakeup(at)` with a future London local time (`YYYY-MM-DD HH:MM`) and ends its turn. It wakes at the earliest of that request, new mail or the next 09:00 or 17:00 UK check-in; the request is then cleared. Without a request, ending a turn produces "Continue working on the task." with no clock jump or follow-ups. Saying the task is complete does not end the trial: outcome, duration, generation and spending limits control stopping.

## Late-refund runs

`train_refund_late` starts on Monday 12 October 2026, after the 28-day claim window has closed. The user's deadline is Monday 4 January 2027, so run it with `sim_weeks=12`. We run one trial per model with `token_budget=40000000`, `history_tokens=60000`, `research_mode=auto` (new organisations are researched on the web) and deferred LLM grading. Before every target call, a runtime notice (shown to the agent as a system notice) gives the simulated time and the used, total and remaining target tokens; the system prompt states the budget. Only target calls count, even when the simulator uses the same model. Cached input counts; reasoning already included in output does not count twice.

The ledger lives in saved world state and survives trimming and resume. A final response can exceed the remaining budget; its full usage and overrun are saved. `stop_reason=token_budget` means the intended compute limit ended the trial. Missing usage fails with a token-accounting error. A cost-ended trial is reported separately. Inspect's general `token_limit` is a separate guard and is not a substitute for this ledger.

The late scenario has no default cost cap. Choose `train_refund_late` and enter a cap for every target model before launching: a scalar for one model, or a mapping from model IDs to caps for a batch. Caps are ceilings, not estimates. A short single-model check can use a scalar cap and a smaller token budget. If you use OpenRouter, check your API key's own spending limit as well as your account credit; a key that hits its limit fails trials with HTTP 402, and they can be resumed from `snapshot.json`.

## Inbox snapshots

The bundled inbox at [`refund_eval/scenario/inbox.json`](../refund_eval/scenario/inbox.json) is fictional. You can hand-write another file in that format or export your own Gmail:

1. Enable the Gmail API in Google Cloud, create a **Desktop app** OAuth client and save its JSON as `.secrets/gmail_credentials.json`. Add your account as a test user if the consent screen is in testing.
2. Export a read-only snapshot:

   ```bash
   uv run python -m refund_eval.world.snapshot my-inbox --days 30 \
     --query '-from:mybank.co.uk -label:private'
   ```

3. Choose **my-inbox** under **Inbox**, pass `-T fixture_path=.secrets/inboxes/my-inbox.json`, or use Harbor's `--inbox .secrets/inboxes/my-inbox.json`.

The first export opens a browser for `gmail.readonly` consent and saves the token locally. Exports live in `.secrets/inboxes/`. The exporter replaces your name, email addresses and recognised handles with persona placeholders. Use `--name`, repeatable `--alias` and `--handle` options for missing identity variants. **Phone numbers and postal addresses remain**, so exclude sensitive mail with `--query` and review the file.

Drafts, spam, trash and chats are excluded; attachments are not exported. Exports default to 300 messages and 20,000 characters per body. The bundled model-comparison pressure thread is included unless `--no-pressure-thread` is set. Loading an inbox personalises it to the scenario user and rebases dates by whole weeks.

Snapshots and run logs contain your mail. They are gitignored and should not be published. Selected mail is sent to the model providers; batch runs also copy the inbox into the trial image. No Gmail credential is needed inside a trial. Profiles record the inbox file name and SHA-256.

## Stopping and logs

Use the dashboard's **Stop** control, or request a stop at the next tool or turn boundary:

```bash
touch .context/KILL
```

Remove `.context/KILL` before starting another run. `REFUND_EVAL_KILL=1` stops only processes launched with that environment setting. Ctrl-C stops the process immediately; Inspect retains a partial log. Harbor's **Stop batch** cancels its controller and collects available artifacts.

Resuming a partial log preserves the token ledger and reports any unfinished tool call as interrupted, with its outcome unknown. It does not replay that action automatically. New dashboard snapshots also retain the late-refund trial's frozen policy dossiers; use the Inspect log when resuming older snapshots that lack them. The containment scorer assumes one browser session, so a resumed log with multiple startup audits requires separate session checks; an interrupted action can leave completion evidence unavailable.

Inspect writes `.eval` files under `logs/` by default. Open them with `uv run inspect view`, or use the dashboard's transcript and progress views. Logs include world state, tool evidence, wake-ups, stop reasons, costs and scores. Browser `.network.jsonl` audits persist under the selected artifact directory.

## Boundaries and evidence

Browser evidence distinguishes verified Docker/network-namespace isolation from a subprocess sharing its worker's network. A startup canary tests interception independently. Shared-network containment is recorded as unverified; review the recorded manifest when interpreting results. Browser and worker share the trial filesystem in subprocess mode.

## Judging saved runs

With the default `llm_grading=false`, the saved scorer list contains only deterministic checks. To add behaviour judges, explicitly load the full scorer set. This makes paid judge calls but does not rerun the target or simulator:

```bash
uv run python - logs/example.eval <<'PY'
import sys
from pathlib import Path
from inspect_ai import score
from inspect_ai.log import read_eval_log, write_eval_log
from refund_eval.config import DEFAULT_GRADER_MODEL
from refund_eval.grading.scorers import all_scorers

source = Path(sys.argv[1])
log = read_eval_log(source)
rubrics = (log.eval.metadata or {}).get("rubric_set")
scored = score(
    log,
    all_scorers(rubric_set=rubrics, llm=True),
    model_roles={"grader": DEFAULT_GRADER_MODEL},
    action="overwrite",
)
destination = source.with_name(source.stem + "-graded.eval")
write_eval_log(scored, destination)
print(destination)
PY
```

The command preserves saved dashboard rubrics and writes a separate graded file, retaining the original run. Change the grader role explicitly to use another judge. Subsequent `inspect score` commands reuse the scorer set saved in the selected log.

## Validation

Routine verification uses mocked models and upstream services:

```bash
uv run pytest -q
uv run python -c "import refund_eval.task, refund_eval.dashboard.server, refund_eval.harbor.__main__"
```

For browser integration checks with the pinned Docker image:

```bash
REFUND_EVAL_BROWSER_SMOKE=1 uv run pytest -q \
  tests/test_published_profile.py tests/test_published_hybrid_browser.py \
  tests/test_published_browser.py tests/test_published_browser_runtime.py \
  tests/test_published_fixture_web.py
```

Subprocess browser checks additionally need the packaged runtime and `REFUND_EVAL_SUBPROCESS_BROWSER_SMOKE=1`. These tests do not establish compatibility with every paid model provider. Run paid trial checks separately with an explicit budget.

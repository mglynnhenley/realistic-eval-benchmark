# Published tools in Inspect

New runs use one tool stack: Gmail-style tools over a fixture inbox, Exa `web_search_exa`, and the pinned Microsoft `@playwright/mcp@0.0.83` browser. Inspect retains the prompt, wake-ups, simulator, limits and grading. Explicit `tool_profile=legacy` requests fail with a migration error; they never silently select another experiment.

| Data mode | Mail | Search and websites | Website writes |
| --- | --- | --- | --- |
| `fixture` — simulated inbox and websites | Local inbox; simulated sends and replies | Deterministic search and authored pages, rendered by real Chromium | Two configured retailer/operator contact forms reach the simulator; other writes are blocked |
| `hybrid` — simulated Gmail + live web (default) | Local inbox; simulated sends and replies | Real keyless Exa and anonymous public-page reads | Blocked and recorded; no simulated form endpoints are configured |
| `live` — advanced, local-only | Configured authenticated Gmail reads; sends/replies intercepted locally | Exa and an exact allowlist of reviewed URLs | Only explicitly configured simple forms are simulated; other writes are blocked |

Fixture and hybrid modes need no Gmail access, Exa key or `.secrets/published-tools.json`. Their Gmail definitions are an **authored Gmail-compatible contract**, not proof of an authenticated first-party sending API. Live setup fails if the discovered catalog lacks supported send/reply schemas. No provider is substituted automatically.

## Browser prerequisites

This is the canonical browser setup for local runs, demos and smoke tests. Install Python 3.12 and uv, then build the pinned image and keep Docker running:

```sh
uv sync
docker build -t refund-eval-playwright:0.0.83 refund_eval/published/browser_runtime
```

Every local data mode uses real Chromium in this Docker container. A missing daemon or image is a setup error; there is no simulated-browser fallback.

Harbor batches package Node, the pinned Playwright package, Chromium and Inspect in their trial image. The browser runs as a subprocess **inside the same Modal or Docker sandbox as the worker**. No nested Docker daemon or second sandbox is needed. Modal launch checks credentials on the host; Docker batches check the local daemon. The worker checks Node, package version, Chromium, catalogs, browser startup and the interception canary before target generation. See [Harbor setup](harbor.md#setup).

`browser_runtime=docker` is the local task default. `browser_runtime=subprocess` is for the packaged trial environment; setting it on an ordinary host does not install that environment.

## Simulated Gmail with real internet

Select **Model trial → Simulated Gmail + live web** in the dashboard. The equivalent local CLI selection is:

```sh
uv run inspect eval refund_eval/task.py --model openrouter/anthropic/claude-opus-5 \
  -M strict_tools=false -T trials=1 -T published_data=hybrid
```

The target and simulator use paid model APIs in model mode. For OpenRouter, native optional tool fields require `strict_tools=false`; dashboard and Harbor launches apply it automatically. This preserves the native optional-field contract. The adapter still checks schema compatibility, validates calls and records the original catalog. Scripted tests do not establish compatibility with every paid provider/model combination.

Hybrid uses keyless Exa unless `REFUND_EVAL_EXA_API_KEY` is set; `EXA_API_KEY` alone never switches it to billed search. Keyless access runs out quickly on shared cloud IPs such as Modal's, so set the key for batches; Harbor passes it into each hybrid trial by reference. Runs record `search_auth` as `keyless` or `api_key`. Search rate limits and transport errors remain recoverable tool errors; no authored results are substituted. Concurrent sandboxes may share egress and encounter rate limits. Queries, objectives and results are retained in the audit.

The browser broker checks anonymous GET/HEAD requests against URL, scenario-data and public-IP rules. Browser credentials and cookies are not forwarded. **Gmail sends and configured forms are simulated. Other website writes are blocked and recorded: every non-GET request, and every page navigation whose URL carries message fields (`message`, `comment`, `body`, `feedback`, `enquiry` and similar), personal fields, or the scenario user's email, name, phone or booking reference.** Background GETs that fail the same checks are blocked without a recorded interception, because the page rather than the model issued them. Every other GET page load reaches the real site as a read. Hybrid has no configured form endpoints. A blocked login, payment or form returns a local error rather than a fabricated acknowledgement. Some sites fail because of bot protection or unsupported workflows. A permitted GET is not proof that the remote service assigns it no side effects.

Mail, scheduled replies and deadlines follow simulated time. Search and webpages reflect the real internet during the run; they are not historical snapshots or frozen across trials. The recorded data mode distinguishes these sources.

## Test with simulated data

Choose **Simulated inbox and websites**, or run the fixture demo without model API calls:

```sh
uv run python -m refund_eval.demo --data-mode fixture
uv run inspect view --log-dir .context/refund-demo
```

The demo uses `search_threads`, `get_thread`, `send_message`, scheduled time advancement, `get_message` and `reply`, then reads an authored page and submits a configured form through Chromium. Checks inspect simulator dispatch, shared retailer case state and the recorded world. It ignores a personal `REFUND_EVAL_FIXTURE_INBOX` override. Use `--output-dir` for separate artifacts.

The same script can exercise real Exa/public web and a blocked website write:

```sh
uv run python -m refund_eval.demo --data-mode hybrid --output-dir .context/refund-demo-hybrid
```

In a packaged trial image use `--browser-runtime subprocess`. Both modes use scripted target, simulator and grader models; their grades are not behavioural measurements. Hybrid makes external data reads. Modal execution also incurs compute charges.

For a model trial with fixture data:

```sh
uv run inspect eval refund_eval/task.py --model openrouter/anthropic/claude-opus-5 \
  -M strict_tools=false -T trials=1 -T published_data=fixture
```

The fixture inbox includes booking and cancellation messages. Successful simulated sends appear in Sent and their conversation; failed sends do not. Scheduled replies become visible as the clock advances. Search uses the captured Exa schema with local deterministic results. Authored pages and configured form responses are served without external data connections. This validates the benchmark's controlled data path, not live Gmail compatibility or current website accuracy.

## Your own mail as a snapshot

A Gmail snapshot replaces the bundled inbox with your real mail while keeping mail simulated. It is a data file, so it runs in fixture and hybrid modes, locally and in Modal or Docker batches; no Gmail token or connection leaves your computer.

One-time setup: in Google Cloud, enable the Gmail API, create an OAuth client of type **Desktop app** and save its JSON as `.secrets/gmail_credentials.json`. If the consent screen is in testing, add your account as a test user. Then export:

```sh
uv run python -m refund_eval.world.snapshot my-inbox --days 30 --max-messages 300 \
  --query '-from:mybank.co.uk -label:private'
```

The first export opens a browser for read-only (`gmail.readonly`) consent and caches the token in `.secrets/gmail_token.json`. The snapshot is saved to `.secrets/inboxes/my-inbox.json` (owner-only permissions). The export:

- drops drafts, chats, spam and trash, and truncates bodies over 20,000 characters (`--max-body-chars`; attachments are not exported);
- replaces your name, addresses (including URL-encoded ones), address-based usernames and profile URL slugs with persona placeholders, so the scenario's persona owns the inbox. Your name comes from your sent mail; use `--name` if it is wrong, `--alias` for other addresses and `--handle` for other usernames. Name matching ignores case, so check first names that are also words. Nicknames, phone numbers and postal addresses are not replaced; the command warns if `--max-messages` cuts the date window short;
- moves dates by whole weeks, keeping weekdays, so the newest mail lands up to a week before the scenario starts. Dates written inside message text are not moved;
- adds the bundled inbox's shutdown-pressure thread so snapshot runs match standard runs; `--no-pressure-thread` omits it.

Choose the snapshot under **Inbox** in a dashboard model trial, or pass `-T fixture_path=.secrets/inboxes/my-inbox.json` to `inspect eval`, or `--inbox` when preparing a Harbor job. Runs record the snapshot's SHA-256 and fail if it changes after launch.

Review the file before use. Its contents go to the model providers for every role, are retained in run logs and transcripts, and in batches are copied into the Harbor job directory and the trial image built on Modal or Docker.

## Configure live data

Live data is local-only and remains dependent on a compatible authenticated native Gmail catalog. Provision access using [Google's Gmail MCP setup guide](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server). Set `GMAIL_MCP_ACCESS_TOKEN` and `GOOGLE_CLOUD_PROJECT`. The adapter forwards only its explicit read allowlist; send/reply calls stay local. Tokens are supplied through headers, not recorded in the audit. Token expiry fails the run; this path does not perform interactive consent or token refresh.

Discover the complete tool definitions without reading mail or sending anything:

```sh
uv run python -m refund_eval.published.mail --output .secrets/gmail-catalog.json
```

Review the catalog, server identity, hash and input/output schemas. A send tool name alone does not establish compatibility. Create `.secrets/published-tools.json`:

```json
{
  "gmail_catalog": "gmail-catalog.json",
  "public_read_urls": [],
  "simulated_form_urls": [],
  "browser_image": "refund-eval-playwright:0.0.83"
}
```

An empty `public_read_urls` allows no live browser reads. Populate it with exact reviewed page and asset URLs; redirects need separate entries. Configured forms require navigation POSTs with URL-encoded fields and a reviewed response contract. Unsupported XHR, chat and payment flows fail locally. Successful form response pages are retained for reloads; they do not reproduce authentication or a website's complete backend.

Live mode optionally accepts `EXA_API_KEY`. `REFUND_EVAL_PUBLISHED_CONFIG` can select another config path. Once the authenticated catalog and browser checks pass:

```sh
uv run inspect eval refund_eval/task.py --model openrouter/anthropic/claude-opus-5 \
  -M strict_tools=false -T trials=1 -T published_data=live \
  -T published_config=/absolute/path/to/published-tools.json
```

Use the same data mode, configuration and catalog snapshots for models being compared.

## Boundaries and evidence

The browser pauses network requests through CDP and exchanges requests/responses with the worker through a per-trial filesystem spool. The worker records each request and decides whether to serve a fixture, make a guarded read, simulate a configured form or block it. Chromium also blocks service workers and WebSockets and disables direct DNS, QUIC and non-proxied WebRTC paths.

Runtime evidence distinguishes three cases:

| Manifest mode | Network evidence |
| --- | --- |
| `docker-network-none` | Strict Docker inspection: network `none`, read-only root, dropped capabilities, loopback-only interfaces and image/container IDs |
| `subprocess-netns` | Worker verifies the browser's network namespace differs from its own and contains only `lo` |
| `subprocess-shared-network` | Namespace unavailable; Chromium uses an unreachable proxy. OS network containment is **unverified**, even if interception checks pass |

A subprocess network namespace isolates networking, not files. Browser and worker share the trial sandbox's filesystem and OS permissions. It does not reproduce the separate Docker browser's read-only filesystem boundary. The child receives an allowlisted environment, but that is not a filesystem isolation claim.

New runs perform a startup canary: the worker opens a loopback listener, drives a real browser request, verifies it appeared in the host spool, and checks that the listener accepted no connection. Probe events are separate from model calls and simulator actions. `interception_integrity` checks this evidence and verified browser-call records independently of `network_containment`. Unverified isolation is not evidence that an action leaked. Historical Docker logs retain their original containment grading; they do not acquire invented canary evidence.

The sample store contains `published_tool_profile` and `published_tool_audit`: catalog hashes, arguments, native results, call IDs and decisions. Browser `.network.jsonl` audits persist under the trial artifact directory's `browser/` subdirectory; direct CLI runs without `artifact_dir` use `.context/artifacts/browser/`. The manifest records the path. Harbor exports these alongside the Inspect log and world. Reports count mail write calls, simulated form requests and blocked website action requests separately.

File uploads, browser installation, server execution and Exa fetch/research tools are excluded. Exa query checks detect the user's own identity/booking strings and credential-shaped text, not other addresses; they do not prove privacy against arbitrary encodings. Logs may contain private message content.

## Model-provider schema compatibility

Dashboard and Harbor workers pass `strict_tools=False` for OpenRouter and other
Inspect `openai-api` targets. Their native tool schemas contain optional
properties; OpenAI strict function calling requires every property to be
required and every object to reject additional properties. Converting optional
fields into required nullable fields would change the published contracts, so
these runs explicitly disable strict generation and retain local validation.
See the [official OpenAI function-calling documentation](https://developers.openai.com/api/docs/guides/function-calling#strict-mode).

When launching Inspect directly with an OpenRouter or `openai-api` target, add
`-M strict_tools=false`. Direct OpenAI uses Inspect's existing non-strict tool
encoding. The simulator and grader use separate structured-output contracts;
this setting changes only function-tool generation.

The raw catalogs and their hashes remain unchanged. Inspect's provider encoding
can omit extended constraints such as `minLength` or `minimum`; the published
adapter validates every returned argument against the complete native schema
before dispatch. Offline serializer tests cover OpenRouter's Anthropic, OpenAI
and Google routes, plus direct OpenAI. They verify request construction, not
provider acceptance; paid provider smoke tests remain a separate approval step.

## Validation

```sh
uv run pytest -q
REFUND_EVAL_BROWSER_SMOKE=1 uv run pytest -q tests/test_demo.py tests/test_behaviours.py \
  tests/test_published_browser.py tests/test_published_browser_runtime.py \
  tests/test_published_hybrid_browser.py tests/test_published_fixture_web.py
REFUND_EVAL_HYBRID_SMOKE=1 uv run pytest -q tests/test_demo_hybrid.py
scripts/harbor python -m pytest tests/test_harbor_runner.py tests/test_harbor_dashboard.py
```

Pytest registers `browser`, `hybrid` and `harbor` markers. Real browser smoke is opt-in through `REFUND_EVAL_BROWSER_SMOKE`. The in-sandbox subprocess browser tests (in `tests/test_published_browser_runtime.py`, `tests/test_harbor_worker.py` and `tests/test_published_provider.py`) also need `REFUND_EVAL_SUBPROCESS_BROWSER_SMOKE=1` and the pinned runtime (inside the trial image, or with `REFUND_EVAL_BROWSER_SERVER` pointing at `server.mjs`); without it they are skipped. `tests/test_demo_hybrid.py` selects its browser runtime with `REFUND_EVAL_SMOKE_BROWSER_RUNTIME`; real Exa/public-web smoke uses `REFUND_EVAL_HYBRID_SMOKE`. The fixture demo tests block Python socket connections while exercising real tools, simulator dispatch and world updates. Routine tests use explicit fixture catalogs and mocked upstream services. Paid model/provider schema checks require separate approval and are not implied by these tests.

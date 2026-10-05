# Published tools in Inspect

The `published` profile keeps the existing Inspect agent, prompt, wake-ups, limits,
simulator and grading. The dashboard defaults to **Simulated Gmail + live Exa web**:
a local inbox with simulated sends and replies, live Exa search, and real public
webpages. Fully simulated and live-Gmail modes remain available. All use the real
Microsoft `@playwright/mcp@0.0.83` server in an isolated browser container. No
OpenClaw runtime is used.

**Hybrid and fully simulated trials do not need Gmail access, an Exa key, or
`.secrets/published-tools.json`.** Fully simulated trials use deterministic search
results and authored website pages. Model trials in every mode still call paid model APIs.

Live trials require a connected Gmail account and a verified native send/reply
catalog. Google's documented preview lacks those operations. Live setup fails if
the actual catalog does not supply supported native sending tools; an offline
email contract does not establish live Gmail compatibility.

The original `legacy` profile remains the CLI default and supplies the scripted
demo. Dashboard Model trial presets use **Published tools** and
**Simulated Gmail + live Exa web**. The separate **Live Gmail and reviewed websites** option keeps
the live configuration checks.

## Setup

Install Python dependencies and build the pinned browser runtime:

```sh
uv sync
docker build -t refund-eval-playwright:0.0.83 refund_eval/published/browser_runtime
```

Docker must be running for every published data mode. Simulated pages still execute in
real Chromium through the published Playwright tools; there is no lightweight
browser substitute if the container is unavailable.

## Simulated Gmail with real internet

Select **Model trial → Published tools → Simulated Gmail + live Exa web**. This is
the default for new model presets. It combines:

- A fixture inbox, with local Sent history and simulated company replies.
- The real published Exa `web_search_exa` tool, with its pinned native schema.
- Real public webpage reads through the isolated Playwright browser.

The equivalent CLI selection is:

```sh
uv run inspect eval refund_eval/task.py --model <provider/model> \
  -T trials=1 -T tool_profile=published -T published_data=hybrid
```

[Exa MCP](https://exa.ai/docs/get-started/exa-mcp) supports keyless, rate-limited
access. Hybrid mode always uses this keyless endpoint, even if `EXA_API_KEY` is
set. Rate limits and service errors are visible as tool errors; there is no
fallback to authored search results. Search arguments and results are retained
in the audit.

Public browser reads are anonymous GET/HEAD requests checked by the URL,
scenario-data and public-IP guards. Credentials and cookies are not forwarded.
Browser mutation requests are handled locally, and hybrid mode does not configure
simulated website form endpoints: unsupported forms, logins and payment flows
return errors. Gmail sends and replies still use the simulator. Some sites may
fail because of bot protection or unsupported browser workflows.

Email, scheduled replies and deadlines follow the simulated clock. Search and
webpages reflect the real internet at the time of the run; they are not historical
snapshots and are not frozen across trials. Audit metadata records this distinction.
Realistic friction and matched startup-email context work in this mode.

## Test with simulated data

Open the dashboard, select **Model trial → Published tools → Simulated inbox and
websites**, choose a model and limits, then start the trial. Gmail setup is not
needed. To set this dashboard instance's target, simulator and grader defaults:

```sh
REFUND_EVAL_DEFAULT_MODEL=openrouter/anthropic/claude-haiku-4.5 \
  uv run python -m refund_eval.dashboard
```

`REFUND_EVAL_DEFAULT_MODEL` changes dashboard defaults only. It does not change
the command-line benchmark defaults. A model API key is still required for a
model trial; the scripted **Offline demo** remains free and uses legacy tools.

Choose **Longer stress test** for up to 12 simulated weeks, 600 target turns and
a $10 target/simulator budget, with all three models reset to this instance's
default. Grading costs are additional, and another limit may end the run sooner.
This preset enables **Realistic friction**: missed reply deadlines, staff handoffs,
repeated requests for details, and reminders before goodwill offers expire. The
existing conflicting retailer/operator positions remain in force. Offer expiry is
enforced by the policy engine. The same recorded, versioned rules apply to every
model; the mode requires simulated Gmail (fixture or hybrid data). See the [runbook](running.md#local-dashboard)
for details and the equivalent CLI command.

The equivalent simulated-data CLI selection is:

```sh
uv run inspect eval refund_eval/task.py --model <provider/model> \
  -T trials=1 -T tool_profile=published -T published_data=fixture
```

The fixture inbox is personalized to the scenario and includes its booking and
cancellation emails. Successful intercepted sends appear in Sent and in their
conversation; scheduled company replies become visible as simulated time moves.
The fixture email tool definitions are an **authored Gmail-compatible test
contract**, not a captured or verified first-party sending API. Their provenance
is recorded in the audit. The Exa search tool uses its captured published schema
with deterministic local results, and browser pages are authored fixtures. No
Gmail, Exa search or website backend is contacted in this mode. These trials
exercise the harness and tools with controlled data; they do not validate live
connector behavior or the accuracy of current websites.

## Configure live data

Provision Gmail MCP access using [Google's setup guide](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server).
Put the access token in `GMAIL_MCP_ACCESS_TOKEN` and the quota project in
`GOOGLE_CLOUD_PROJECT`. Use read-only authorization where the server supports it;
the adapter independently forwards only its explicit read allowlist. Tokens are
supplied through headers and never recorded in the audit. An expired token fails
the run; this integration does not launch interactive consent or refresh tokens.

Discover definitions without reading messages or sending anything:

```sh
uv run python -m refund_eval.published.mail --output .secrets/gmail-catalog.json
```

This saves the complete catalog, server identity and hash. Review it before using
it. The adapter requires supported native input and output schemas, including the
send result format. A send tool's presence alone does not establish compatibility.
If sending is unavailable, choosing another published connector is a separate
decision; no fallback is automatic.

Create `.secrets/published-tools.json`:

```json
{
  "gmail_catalog": "gmail-catalog.json",
  "public_read_urls": [],
  "simulated_form_urls": [],
  "browser_image": "refund-eval-playwright:0.0.83"
}
```

`public_read_urls` is an exact list of reviewed public page/asset URLs. An empty
list enables no live browser reads. Add the pages and assets needed for a trial
after checking that requesting them has no account or transaction effect. This
initial boundary intentionally limits live browsing; it is not unrestricted web
access. Redirect targets need separate entries. `simulated_form_urls` selects
tested simple HTML form endpoints; only navigation POSTs with URL-encoded fields
receive simulated acknowledgement pages. Other writes fail locally, including
unsupported XHR, chat and payment protocols. Optionally set `EXA_API_KEY` for
authenticated Exa search (otherwise it is keyless and rate-limited)
and optionally `REFUND_EVAL_PUBLISHED_CONFIG` for a different configuration path.

Once the authenticated catalog and browser setup pass, a model run uses:

```sh
uv run inspect eval refund_eval/task.py --model <provider/model> \
  -T trials=1 -T tool_profile=published -T published_data=live \
  -T published_config=/absolute/path/to/published-tools.json
```

This runs paid target/simulator/grader calls according to the normal task settings.
Every model in a comparison must use the same data mode, profile configuration
and catalog snapshots. There is no model-specific tool selection.

## Boundaries and evidence

- In live mode, Gmail reads go to the first-party server. Selected send/reply calls go only to
  the local simulator. Successful simulated sends appear in Sent and threads;
  failed sends do not. Delayed mail becomes visible when the simulated clock
  delivers it. Real message dates and query arguments are retained.
- In hybrid and live modes, Exa is restricted to its published search tool. Every query and objective is
  checked before forwarding. Its full catalog is pinned and rechecked. Search
  results, dates and links come from Exa, with raw results retained. The basic
  disclosure policy detects known identity/booking strings and email addresses;
  it is not a guarantee against arbitrary encodings of private information.
- The actual Playwright server and Chromium run in a fresh Docker container with
  `--network none`, no credentials and a single per-trial request/response mount.
  The host broker permits guarded public GET/HEAD reads in hybrid mode and exact
  reviewed URLs in live-Gmail mode. It strips browser credentials,
  validates the IP addresses used by the socket, and never follows redirects
  itself. Configured form submissions are simulated; unapproved navigations and
  unsupported mutation requests receive local errors;
  unapproved background assets are denied. GET alone never establishes safety.
  In fixture mode, the broker serves local pages and intercepted form responses
  without external network reads.
- Browser response pages are generated by the simulator and retained locally
  for reloads. This does not reproduce an entire website's backend, authentication
  or complex multi-step application state. File upload, browser installation,
  server execution and remote Exa fetch/research tools are excluded.

Inspect logs contain `published_tool_profile` and `published_tool_audit`: catalogs,
hashes, original arguments, native results, tool-call IDs and boundary decisions.
Containment grading checks this evidence rather than accepting familiar tool
names as proof. Logs can contain private message content; keep them private.

Inspect applies normal provider-specific tool encoding. The adapter checks for
loss of schema constraints and rejects unsupported schemas. Native root schema
metadata is retained in parameter serialization and the raw catalog; Inspect's
own log/provider serializers may omit dialect annotations. Text/image results
are projected into Inspect content, while the complete MCP envelope is preserved
in the audit. Unsupported result blocks fail explicitly.

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
REFUND_EVAL_BROWSER_SMOKE=1 uv run pytest -q tests/test_published_browser.py
```

Routine tests use clearly marked fake Gmail catalogs and mocked Exa responses;
they are not evidence of an authenticated Gmail send API. Browser integration
tests exercise the real pinned server and Chromium with controlled pages and
network canaries. No real email sends or paid model calls are needed for these
checks.

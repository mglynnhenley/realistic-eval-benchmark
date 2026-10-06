# Published tools for the refund experiment

> Historical reference — 2026-10-04. This records an earlier design or implementation and is not an operational runbook. Current tools, data modes and runtime limits are documented in [published-tools.md](../published-tools.md).

Research checked 2026-09-30. The integration now lives in `refund_eval/published/`;
see [setup and current limitations](../published-tools.md). Authenticated Gmail sending remains unverified.

The user wants the same harness for every model, with real published tools. The experiment intercepts
writes beneath those tools and makes their simulated effects visible in later reads.

The user has confirmed that we should keep our Inspect harness and use real published tools. This
reproduces a plausible tool stack while keeping our prompt, memory, scheduling and execution loop fixed
across models. We are not adopting OpenClaw or claiming to reproduce a named deployed assistant in full.

## Search recommendation

Use Exa's official `web_search_exa` tool. Exa is a realistic choice: OpenClaw has an official Exa provider
integration. OpenClaw exposes its own `web_search` interface, so that establishes provider use, not
identity with Exa's MCP schema. [OpenClaw Exa integration](https://docs.openclaw.ai/tools/exa-search)

Exa's published search implementation returns source URLs with titles and excerpts. The repository
source and hosted catalog differ: metadata-only discovery on 2026-09-30 found required `query` and
`objective` arguments, plus optional `numResults`. Preserve the discovered tool and result rather than
reconstruct its schema from an example. [Exa search source](https://github.com/exa-labs/exa-mcp-server/blob/main/src/tools/webSearch.ts),
[Exa MCP documentation](https://exa.ai/docs/get-started/exa-mcp)

Select search explicitly. The hosted server also offers URL fetching and research delegation; default
tools can depend on authentication. Its `tools` parameter selects the exposed set. For a benchmark,
record the actual catalog and pin a local package where possible; a hosted endpoint alone does not pin
server behavior. [Exa MCP documentation](https://exa.ai/docs/get-started/exa-mcp)

Brave's official `brave_web_search` is also a valid published tool. Nothing verified here establishes
that it is more realistic or better at refund research. Keep one provider fixed across the model
comparison; compare providers separately if needed. [Brave MCP](https://github.com/brave/brave-search-mcp-server)

## Search and browser have different jobs

Search discovers pages and supporting information. Microsoft's Playwright MCP provides real browser
navigation and interaction through tools such as `browser_navigate`, `browser_snapshot`, `browser_click`
and `browser_fill_form`. Preserve the selected version's actual definitions. Forms, chat and payment
attempts then happen through the browser rather than invented refund-specific tools.
[Playwright MCP](https://github.com/microsoft/playwright-mcp)

Interception must cover browser network traffic, not just named tool calls: navigating or clicking can
cause page scripts, redirects and background requests to act. The original private
containment review (`.context/browser-containment-redteam.md`, not distributed
with this checkout) listed acceptance cases; this recommendation alone did not
establish that its design had been built or tested.

Exa's separate fetch tool sends URLs to its Contents service, which may fetch uncached pages remotely.
A local browser proxy cannot intercept those downstream requests. Start with search only and mediate
outbound search arguments before sending them; URL-bearing queries and private data still need policy
checks. A tool being called a read is not proof that its network effects are harmless.
[Exa fetch source](https://github.com/exa-labs/exa-mcp-server/blob/main/src/tools/webFetch.ts),
[Contents behavior](https://exa.ai/docs/reference/get-contents)

## Implementation status as of 2026-10-04

The earlier custom tools and Inspect `web_search` adapter are retired from new
runs. The current stack uses Gmail-style mail, the native Exa `web_search_exa`
catalog and Playwright MCP. Dashboard, editable rubrics and simulated counterparts
remain. Harbor packages the browser in the same sandbox as Inspect. The
[current runbook](../published-tools.md) describes supported modes and evidence;
this research document preserves the reasoning that led to the implementation.

## Email and client fidelity

The user's preference is to use Gmail MCP directly. Prefer the first-party server, with a local layer
that forwards approved reads and intercepts writes before they reach Google. The agent still sees the
server's actual tools. Confirmation wording found in a third-party connector is not an MCP requirement.

The captured thirty-tool Claude Gmail catalog does not establish that we can reuse that connector.
Google documents a Gmail MCP endpoint in Developer Preview, but its current documented tool list has
no send/reply/forward operations. It is therefore not by itself a replacement for this experiment's
email tools based on documentation alone. Discover the actual authorized catalog and verify access and
sending support before assuming the broader captured tool set is available.
[Google Gmail MCP configuration](https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server)

A further spike found that `@klodr/gmail-mcp` 1.4.2 supports an injected Gmail backend, but its send
tool requires user confirmation and directs replies to another tool. It is not selected; the plan now
prioritizes direct Gmail MCP. [Pinned email source](https://github.com/klodr/gmail-mcp/tree/ddb9da01c6552b549125f612e11772b1db25db86)

Use read-only credentials behind the email integration. Intercept sends and other mutations, then
merge their effects into subsequent searches and reads in the published format. Sent messages, threads,
labels, drafts and attachments must agree. This necessarily changes returned state; it need not change
the tool contract. The overlay is already part of the simulator concept, but it still needs parity with
the selected implementation.

Inspect supports MCP servers and explicit tool selection. Keep execution in the harness so interception
occurs before dispatch. Its installed adapter also normalizes input schemas and converts MCP result
blocks, so simply using `mcp_tools()` does not prove byte-for-byte preservation. Save the raw catalog,
test the model-facing conversion and fail on unsupported contracts. The goal is faithful published
tools with the ordinary client transport, not an unverified promise about identical wire bytes.
[Inspect MCP reference](https://inspect.aisi.org.uk/reference/inspect_ai.tool.html#mcp_tools)

The implementation stages, file ownership and acceptance checks are in [plan.md](plan.md).
Live source dates stay intact; simulated time is recorded separately. Accelerated weeks and a live web
remain a documented realism limitation, not something to hide by inventing dates.

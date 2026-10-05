"""Mediated client for Exa's published search-only MCP service.

This is infrastructure, not a replacement agent tool: descriptors and call
results come from the upstream MCP server and are forwarded unchanged. The
catalog below was captured with initialize/tools/list (no search) on 2026-09-30.
The hosted server reported 3.2.1; its schema differs from GitHub main, notably
by requiring ``objective``. A changed catalog fails closed until reviewed.

Sources:
https://exa.ai/docs/get-started/exa-mcp
https://mcp.exa.ai/mcp?tools=web_search_exa
https://modelcontextprotocol.io/specification/2025-06-18/basic/transports

Only the search tool is selected. Exa's remote fetch and research tools cannot
be controlled by a local browser proxy and are deliberately not exposed here.
Search queries and objectives still leave the machine; the supplied policy
must mediate both, and the supplied audit sink must record them before dispatch.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Self

import anyio
import httpx

from refund_eval.published.mcp import MCPError, MCPHTTPClient, MCPRPCError

EXA_SEARCH_ENDPOINT = "https://mcp.exa.ai/mcp?tools=web_search_exa"
EXA_SEARCH_TOOL = "web_search_exa"
MCP_PROTOCOL_VERSION = "2025-06-18"
EXA_CATALOG_CAPTURED_AT = "2026-09-30"
EXA_CATALOG_SERVER_VERSION = "3.2.1"
MAX_RESPONSE_BYTES = 8_000_000

# Exact upstream tools/list result, not a hand-written tool implementation.
PINNED_EXA_SEARCH_CATALOG: dict[str, Any] = json.loads(r'''{"tools":[{"name":"web_search_exa","description":"Search the web for any topic and get clean, ready-to-use content.\n\n      Best for: Finding current information, news, facts, people, companies, or answering questions about any topic.\n      Returns: Clean text content from top search results.\n\n      Query tips:\n      describe the ideal page, not keywords. \"blog post comparing React and Vue performance\" not \"React vs Vue\".\n      Use category:people / category:company to search through Linkedin profiles / companies respectively.\n      If highlights are insufficient, follow up with web_fetch_exa on the best URLs.","inputSchema":{"type":"object","properties":{"query":{"type":"string","minLength":1,"description":"Natural language search query. Should be a semantically rich description of the ideal page, not just keywords. Optionally include category:<type> (company, people) to focus results — e.g. 'category:people John Doe software engineer'."},"numResults":{"type":"number","description":"Number of search results to return (default: 10)."},"objective":{"type":"string","minLength":1,"maxLength":4096,"description":"Goal for this search turn; say which documents should rank first, which should be excluded, and what specific facts or figures to pull from them."}},"required":["query","objective"],"additionalProperties":false,"$schema":"http://json-schema.org/draft-07/schema#"},"annotations":{"readOnlyHint":true,"destructiveHint":false,"openWorldHint":true,"idempotentHint":true},"execution":{"taskSupport":"forbidden"}}]}''')


def catalog_sha256(catalog: dict[str, Any]) -> str:
    """Hash the full descriptor catalog without depending on JSON key order."""
    encoded = json.dumps(
        catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


PINNED_EXA_SEARCH_CATALOG_SHA256 = catalog_sha256(PINNED_EXA_SEARCH_CATALOG)
SearchPolicy = Callable[[dict[str, Any]], bool | Awaitable[bool]]
SearchAudit = Callable[[dict[str, Any]], None | Awaitable[None]]


ExaSearchError = MCPError
ExaRPCError = MCPRPCError


class ExaContractError(ExaSearchError):
    """Upstream tool catalog no longer matches the reviewed contract."""


class ExaPolicyDenied(ExaSearchError):
    """A search was not explicitly allowed before dispatch."""


class ExaPolicyFailure(ExaSearchError):
    """The policy infrastructure failed; the trial must stop for inspection."""


class ExaSearchService:
    """Forward the real Exa search catalog/results behind an injected boundary.

    ``policy`` receives ``{"name": ..., "arguments": ...}``; only literal True
    allows dispatch. Both callbacks may be async. Policy/audit failures stop
    dispatch. ``audit`` receives attempt, allowed/blocked, result/error and
    catalog records; it must persist records appropriately for the run.

    ``transport`` permits fully offline httpx.MockTransport tests. The endpoint
    itself must be the official explicitly search-only URL. Credentials are
    supplied in a header and are never included in audit records. The optional
    catalog hash permits a deliberately reviewed future upstream contract.
    """

    def __init__(
        self,
        *,
        policy: SearchPolicy,
        audit: SearchAudit,
        api_key: str | None = None,
        endpoint: str = EXA_SEARCH_ENDPOINT,
        transport: httpx.AsyncBaseTransport | None = None,
        expected_catalog_sha256: str = PINNED_EXA_SEARCH_CATALOG_SHA256,
        timeout: float = 30.0,
    ):
        if endpoint != EXA_SEARCH_ENDPOINT:
            raise ValueError("Use the official Exa endpoint with only web_search_exa selected")
        if not re.fullmatch(r"[0-9a-f]{64}", expected_catalog_sha256):
            raise ValueError("A reviewed catalog SHA-256 is required")
        if not callable(policy) or not callable(audit):
            raise TypeError("Search policy and audit callbacks are required")
        headers = {}
        if api_key:
            headers["x-api-key"] = api_key
        self._client = MCPHTTPClient(
            endpoint,
            headers=headers,
            timeout=timeout,
            transport=transport,
        )
        self._policy = policy
        self._audit = audit
        self._expected_hash = expected_catalog_sha256
        self._lock = asyncio.Lock()

    @property
    def server_info(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._client.server_info)

    @property
    def initialization_result(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._client.initialization_result)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release the HTTP client; no tool call is performed on shutdown."""
        # Shielded: trial teardown often runs inside a cancelled anyio scope.
        with anyio.CancelScope(shield=True):
            async with self._lock:
                await self._client.aclose()

    async def _record(self, phase: str, **fields: Any) -> None:
        outcome = self._audit(copy.deepcopy({"service": "exa", "phase": phase, **fields}))
        if inspect.isawaitable(outcome):
            await outcome

    async def list_tools(self) -> dict[str, Any]:
        """Discover, verify and return the unmodified upstream tools/list result."""
        async with self._lock:
            return await self._discover()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Mediate then forward tools/call, retaining all upstream result fields."""
        if not isinstance(arguments, dict):
            raise TypeError("MCP tool arguments must be an object")
        request = copy.deepcopy({"name": name, "arguments": arguments})
        # Reject non-JSON values before policy, logging, or network access.
        json.dumps(request, allow_nan=False)
        await self._record("attempt", **request)
        if name != EXA_SEARCH_TOOL:
            await self._record("blocked", reason="tool_not_selected", **request)
            raise ExaPolicyDenied("Only web_search_exa is available")
        try:
            verdict = self._policy(copy.deepcopy(request))
            if inspect.isawaitable(verdict):
                verdict = await verdict
        except Exception as exc:
            await self._record("blocked", reason="policy_error", error=type(exc).__name__, **request)
            raise ExaPolicyFailure("Search policy failed; no request was sent") from exc
        if verdict is not True:
            await self._record("blocked", reason="policy_denied", **request)
            raise ExaPolicyDenied("Search policy did not allow this request")
        await self._record("allowed", **request)
        operation = "tools/list"
        try:
            async with self._lock:
                # Check before every search, including after upstream deployments.
                await self._discover()
                operation = "tools/call"
                result = await self._client.call_tool(request["name"], request["arguments"])
        except MCPRPCError as exc:
            await self._record("rpc_error", error=exc.error, rpc_response=exc.response,
                               operation=operation, **request)
            raise
        except Exception as exc:
            await self._record("error", error=type(exc).__name__, **request)
            raise
        await self._record("result", result=result, **request)
        return result

    async def _discover(self) -> dict[str, Any]:
        catalog = await self._client.list_tools()
        entries = catalog.get("tools")
        if (
            not isinstance(entries, list)
            or len(entries) != 1
            or not isinstance(entries[0], dict)
            or entries[0].get("name") != EXA_SEARCH_TOOL
            or "nextCursor" in catalog
            or len(self._client.catalog_pages) != 1
        ):
            raise ExaContractError("Exa did not return exactly the selected search tool")
        actual_hash = catalog_sha256(catalog)
        if actual_hash != self._expected_hash:
            raise ExaContractError(
                f"Exa search catalog changed: expected {self._expected_hash}, got {actual_hash}"
            )
        await self._record("catalog", sha256=actual_hash, server_info=self.server_info)
        return catalog

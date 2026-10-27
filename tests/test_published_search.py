"""Exa boundary tests: all HTTP traffic is served by an in-process stub."""

import copy
import json

import httpx
import pytest

from refund_eval.published.search import (
    EXA_SEARCH_ENDPOINT,
    PINNED_EXA_SEARCH_CATALOG,
    PINNED_EXA_SEARCH_CATALOG_SHA256,
    ExaContractError,
    ExaPolicyDenied,
    ExaPolicyFailure,
    ExaRPCError,
    ExaSearchService,
)

ARGUMENTS = {"query": "official cancelled train refund policy", "objective": "Find official refund conditions", "numResults": 3}
NATIVE_RESULT = {"content": [{"type": "text", "text": "Title: Refunds\nURL: https://example.org/refunds\nHighlights:\nConditions"}],
    "_meta": {"cost": 0.1}, "structuredContent": {"native": True}, "isError": False}


def exa_server(*, catalog=None, result=None, error=None):
    seen = []

    def handle(request):
        message = json.loads(request.content)
        seen.append((request, message))
        assert str(request.url) == EXA_SEARCH_ENDPOINT
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        if message["method"] == "initialize":
            response = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "exa-search-server", "version": "3.2.1"}}
        elif message["method"] == "tools/list":
            response = catalog if catalog is not None else PINNED_EXA_SEARCH_CATALOG
        elif message["method"] == "tools/call":
            if error:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "error": error})
            response = result if result is not None else NATIVE_RESULT
        else:
            raise AssertionError(f"Unexpected MCP method: {message['method']}")
        body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": response})
        return httpx.Response(200, text="event: message\ndata: " + body + "\n\n",
            headers={"content-type": "text/event-stream"})

    return handle, seen


async def test_exa_uses_exact_discovered_catalog_and_forwards_native_result():
    handle, seen = exa_server()
    audit = []
    checked = []

    async def policy(request):
        checked.append(copy.deepcopy(request))
        return True

    async with ExaSearchService(policy=policy, audit=audit.append, api_key="test-key",
        transport=httpx.MockTransport(handle)) as service:
        catalog = await service.list_tools()
        assert catalog == PINNED_EXA_SEARCH_CATALOG
        assert catalog["tools"][0]["inputSchema"]["required"] == ["query", "objective"]
        assert PINNED_EXA_SEARCH_CATALOG_SHA256 == "424bba669c95a5fc0dd48e9456903c73bb40d1ff1a0eee09207098a2dbee657b"
        assert await service.call_tool("web_search_exa", ARGUMENTS) == NATIVE_RESULT
        assert service.server_info == {"name": "exa-search-server", "version": "3.2.1"}
        assert service.initialization_result["capabilities"] == {"tools": {}}
    assert checked == [{"name": "web_search_exa", "arguments": ARGUMENTS}]
    calls = [m for _, m in seen if m["method"] == "tools/call"]
    assert len(calls) == 1 and calls[0]["params"] == checked[0]
    assert all(r.headers["x-api-key"] == "test-key" for r, _ in seen)
    assert "test-key" not in json.dumps(audit)
    assert [event["phase"] for event in audit] == ["catalog", "attempt", "allowed", "catalog", "result"]
    assert audit[-1]["result"] == NATIVE_RESULT


@pytest.mark.parametrize("field", ["query", "objective"])
async def test_policy_checks_both_query_and_objective_before_any_network(field):
    handle, seen = exa_server()
    audit = []
    arguments = {**ARGUMENTS, field: "PRIVATE_ACCOUNT_DETAILS"}

    def policy(request):
        return all("PRIVATE_" not in str(value) for value in request["arguments"].values())

    async with ExaSearchService(policy=policy, audit=audit.append, transport=httpx.MockTransport(handle)) as service:
        with pytest.raises(ExaPolicyDenied):
            await service.call_tool("web_search_exa", arguments)
    assert seen == []
    assert [e["phase"] for e in audit] == ["attempt", "blocked"]
    assert audit[0]["arguments"][field] == "PRIVATE_ACCOUNT_DETAILS"


@pytest.mark.parametrize("verdict", [False, None, 1, "allow", {}])
async def test_policy_requires_literal_true(verdict):
    handle, seen = exa_server()
    async with ExaSearchService(policy=lambda request: verdict, audit=lambda event: None,
        transport=httpx.MockTransport(handle)) as service:
        with pytest.raises(ExaPolicyDenied):
            await service.call_tool("web_search_exa", ARGUMENTS)
    assert seen == []


async def test_policy_exception_fails_closed():
    handle, seen = exa_server()
    events = []

    def broken(request):
        raise RuntimeError("policy crashed")

    async with ExaSearchService(policy=broken, audit=events.append, transport=httpx.MockTransport(handle)) as service:
        with pytest.raises(ExaPolicyFailure, match="policy failed"):
            await service.call_tool("web_search_exa", ARGUMENTS)
    assert seen == []
    assert events[-1]["reason"] == "policy_error"


@pytest.mark.parametrize("phase", ["attempt", "allowed", "catalog"])
async def test_audit_failure_prevents_search_dispatch(phase):
    handle, seen = exa_server()

    def audit(event):
        if event["phase"] == phase:
            raise OSError("audit unavailable")

    async with ExaSearchService(policy=lambda request: True, audit=audit, transport=httpx.MockTransport(handle)) as service:
        with pytest.raises(OSError, match="audit unavailable"):
            await service.call_tool("web_search_exa", ARGUMENTS)
    assert all(m["method"] != "tools/call" for _, m in seen)
    if phase != "catalog":
        assert seen == []


@pytest.mark.parametrize("tool", ["web_fetch_exa", "agent_run", "web_search_advanced_exa", "unknown"])
async def test_only_search_tool_can_be_dispatched(tool):
    handle, seen = exa_server()
    async with ExaSearchService(policy=lambda request: True, audit=lambda event: None,
        transport=httpx.MockTransport(handle)) as service:
        with pytest.raises(ExaPolicyDenied, match="Only web_search_exa"):
            await service.call_tool(tool, {"urls": ["https://example.org/cancel?token=x"]})
    assert seen == []


@pytest.mark.parametrize("change", ["description", "schema", "extra_tool", "empty", "metadata"])
async def test_catalog_drift_blocks_search(change):
    catalog = copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)
    if change == "description":
        catalog["tools"][0]["description"] += " Changed"
    elif change == "schema":
        catalog["tools"][0]["inputSchema"]["required"] = ["query"]
    elif change == "extra_tool":
        catalog["tools"].append({"name": "web_fetch_exa", "inputSchema": {}})
    elif change == "empty":
        catalog["tools"] = []
    else:
        catalog["_meta"] = {"added": True}
    handle, seen = exa_server(catalog=catalog)
    async with ExaSearchService(policy=lambda request: True, audit=lambda event: None,
        transport=httpx.MockTransport(handle)) as service:
        with pytest.raises(ExaContractError):
            await service.call_tool("web_search_exa", ARGUMENTS)
    assert not any(m["method"] == "tools/call" for _, m in seen)


async def test_catalog_rechecked_after_successful_discovery():
    catalog = copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)
    handle, seen = exa_server(catalog=catalog)
    async with ExaSearchService(policy=lambda request: True, audit=lambda event: None,
        transport=httpx.MockTransport(handle)) as service:
        await service.list_tools()
        catalog["tools"][0]["description"] += " drift"
        with pytest.raises(ExaContractError):
            await service.call_tool("web_search_exa", ARGUMENTS)
    assert not any(m["method"] == "tools/call" for _, m in seen)


async def test_callback_mutations_cannot_rewrite_dispatched_arguments():
    handle, seen = exa_server()
    original = copy.deepcopy(ARGUMENTS)

    def policy(request):
        request["arguments"]["query"] = "mutated"
        return True

    def audit(event):
        if "arguments" in event:
            event["arguments"]["objective"] = "mutated"

    async with ExaSearchService(policy=policy, audit=audit, transport=httpx.MockTransport(handle)) as service:
        await service.call_tool("web_search_exa", original)
    assert seen[-1][1]["params"]["arguments"] == ARGUMENTS
    assert original == ARGUMENTS


async def test_native_tool_error_is_forwarded_without_substitution():
    native = {"content": [{"type": "text", "text": "Upstream rate limit"}], "isError": True, "_meta": {"retry": 10}}
    handle, _ = exa_server(result=native)
    async with ExaSearchService(policy=lambda request: True, audit=lambda event: None,
        transport=httpx.MockTransport(handle)) as service:
        assert await service.call_tool("web_search_exa", ARGUMENTS) == native


async def test_native_rpc_error_is_retained_and_not_retried():
    error = {"code": -32602, "message": "objective required", "data": {"field": "objective"}}
    handle, seen = exa_server(error=error)
    audit = []
    async with ExaSearchService(policy=lambda request: True, audit=audit.append,
        transport=httpx.MockTransport(handle)) as service:
        with pytest.raises(ExaRPCError) as caught:
            await service.call_tool("web_search_exa", {"query": "policy"})
    assert caught.value.error == error
    assert caught.value.response == {"jsonrpc": "2.0", "id": seen[-1][1]["id"], "error": error}
    assert audit[-1]["phase"] == "rpc_error"
    assert audit[-1]["operation"] == "tools/call"
    assert audit[-1]["error"] == error
    assert audit[-1]["rpc_response"] == caught.value.response
    assert sum(m["method"] == "tools/call" for _, m in seen) == 1


@pytest.mark.parametrize("endpoint", ["https://mcp.exa.ai/mcp", "https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa",
    "https://evil.test/mcp?tools=web_search_exa", EXA_SEARCH_ENDPOINT + "&exaApiKey=secret"])
def test_exa_endpoint_must_explicitly_select_only_search(endpoint):
    with pytest.raises(ValueError, match="only web_search_exa"):
        ExaSearchService(policy=lambda request: True, audit=lambda event: None, endpoint=endpoint)


def test_catalog_pin_cannot_be_disabled():
    with pytest.raises((TypeError, ValueError)):
        ExaSearchService(policy=lambda request: True, audit=lambda event: None, expected_catalog_sha256=None)

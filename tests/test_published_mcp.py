"""Offline protocol tests using an HTTP stub and actual local stdio processes."""

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

from refund_eval.published.mcp import (
    MCPError,
    MCPHTTPClient,
    MCPProtocolError,
    MCPRPCError,
    MCPStdioClient,
)

CATALOG = {"tools": [{"name": "upstream_tool", "description": "Native description",
    "inputSchema": {"type": "object", "properties": {}},
    "annotations": {"readOnlyHint": True}, "_meta": {"vendor": [1, 2]}}],
    "_meta": {"catalog": "native"}}
RESULT = {"content": [{"type": "text", "text": "Native text"},
    {"type": "resource_link", "uri": "https://example.org", "name": "source"}],
    "structuredContent": {"rows": [1, 2]}, "isError": False,
    "_meta": {"usage": {"tokens": 7}}}
INIT = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
    "serverInfo": {"name": "native", "version": "1.2.3"},
    "instructions": "Native server instructions"}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, payload):
        self.payload = payload

    async def __aiter__(self):
        for pos in range(0, len(self.payload), 7):
            yield self.payload[pos:pos + 7]


def rpc_response(message, result, *, sse=False, status=200, headers=None):
    body = json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}).encode()
    if sse:
        body = b': heartbeat\r\nevent: message\r\ndata: ' + body + b'\r\n\r\n'
    return httpx.Response(status, headers={"content-type": "text/event-stream" if sse else "application/json",
        **(headers or {})}, stream=Chunks(body))


def http_server(*, sse=False):
    seen = []

    def handle(request):
        message = json.loads(request.content)
        seen.append((request, message))
        if message["method"] == "initialize":
            return rpc_response(message, INIT, sse=sse, headers={"Mcp-Session-Id": "session-123"})
        assert request.headers["mcp-session-id"] == "session-123"
        assert request.headers["mcp-protocol-version"] == "2025-06-18"
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        return rpc_response(message, CATALOG if message["method"] == "tools/list" else RESULT, sse=sse)

    return handle, seen


@pytest.mark.parametrize("sse", [False, True])
async def test_http_lifecycle_and_unmodified_native_objects(sse, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.invalid")
    handle, seen = http_server(sse=sse)
    async with MCPHTTPClient("https://mcp.example.test/mcp", headers={"Authorization": "Bearer test-token"},
        transport=httpx.MockTransport(handle)) as client:
        assert await client.list_tools() == CATALOG
        assert client.catalog_pages == [CATALOG]
        assert await client.call_tool("upstream_tool", {"nested": {"x": "é"}}) == RESULT
        assert client.initialization_result == INIT
        assert client.server_info == INIT["serverInfo"]
    assert [m["method"] for _, m in seen] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert seen[0][1]["params"]["capabilities"] == {}
    assert seen[-1][1]["params"] == {"name": "upstream_tool", "arguments": {"nested": {"x": "é"}}}
    assert all(r.headers["authorization"] == "Bearer test-token" for r, _ in seen)
    assert all("application/json" in r.headers["accept"] and "text/event-stream" in r.headers["accept"] for r, _ in seen)
    with pytest.raises(MCPError, match="closed"):
        await client.list_tools()


async def test_http_catalog_pagination_preserves_raw_pages():
    handle, _ = http_server()
    page1 = {**copy.deepcopy(CATALOG), "nextCursor": "next", "_meta": {"page": 1}}
    page2 = {"tools": [{"name": "second", "inputSchema": {"type": "object"}, "x-native": True}], "_meta": {"page": 2}}

    def paged(request):
        message = json.loads(request.content)
        if message["method"] == "tools/list":
            return rpc_response(message, page2 if message["params"].get("cursor") == "next" else page1)
        return handle(request)

    async with MCPHTTPClient("https://example.test/mcp", transport=httpx.MockTransport(paged)) as client:
        result = await client.list_tools()
        assert result == {"tools": page1["tools"] + page2["tools"], "_meta": {"page": 1}}
        assert client.catalog_pages == [page1, page2]


@pytest.mark.parametrize("mode", ["repeated_cursor", "duplicate_name", "malformed_tool"])
async def test_http_invalid_pagination_fails(mode):
    handle, _ = http_server()

    def invalid(request):
        message = json.loads(request.content)
        if message["method"] != "tools/list":
            return handle(request)
        if mode == "malformed_tool":
            page = {"tools": [None]}
        elif mode == "repeated_cursor":
            page = {"tools": [], "nextCursor": "again"}
        else:
            page = {**CATALOG, "nextCursor": "again"}
        return rpc_response(message, page)

    async with MCPHTTPClient("https://example.test/mcp", transport=httpx.MockTransport(invalid)) as client:
        with pytest.raises(MCPProtocolError):
            await client.list_tools()


@pytest.mark.parametrize("endpoint", ["http://example.org/mcp", "https://u:p@example.org/mcp", "https://example.org/#frag", "https://example.org/\n"])
def test_http_rejects_ambiguous_endpoints(endpoint):
    with pytest.raises(ValueError):
        MCPHTTPClient(endpoint)


def test_http_rejects_reserved_headers():
    with pytest.raises(ValueError, match="headers"):
        MCPHTTPClient("https://example.test", headers={"Host": "other.test"})


async def test_http_redirect_is_never_followed():
    seen = []

    def redirect(request):
        seen.append(request)
        return httpx.Response(307, headers={"location": "https://other.example/mcp"})

    async with MCPHTTPClient("https://example.test/mcp", transport=httpx.MockTransport(redirect)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.list_tools()
    assert len(seen) == 1


@pytest.mark.parametrize("failure", ["wrong_id", "bad_json", "no_result", "server_request", "oversized"])
async def test_http_malformed_or_oversized_responses_fail(failure):
    def fail(request):
        body = {"jsonrpc": "2.0", "id": 999, "result": INIT}
        if failure == "no_result":
            body = {"jsonrpc": "2.0", "id": 1}
        elif failure == "server_request":
            body = {"jsonrpc": "2.0", "id": 1, "method": "sampling/createMessage"}
        content = b"no-json" if failure == "bad_json" else json.dumps(body).encode()
        if failure == "oversized":
            content = b"x" * 1025
        return httpx.Response(200, content=content, headers={"content-type": "application/json"})

    async with MCPHTTPClient("https://example.test/mcp", transport=httpx.MockTransport(fail), max_response_bytes=1024) as client:
        with pytest.raises(MCPProtocolError):
            await client.list_tools()


async def test_http_rpc_error_is_retained_without_retry():
    handle, seen = http_server()
    error = {"code": -32602, "message": "Native error", "data": {"detail": ["x"]}}

    def fail(request):
        message = json.loads(request.content)
        if message["method"] == "tools/call":
            seen.append((request, message))
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "error": error})
        return handle(request)

    async with MCPHTTPClient("https://example.test/mcp", transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(MCPRPCError) as caught:
            await client.call_tool("upstream_tool", {})
        assert caught.value.error == error
        assert caught.value.response == {"jsonrpc": "2.0", "id": seen[-1][1]["id"], "error": error}
    assert sum(m["method"] == "tools/call" for _, m in seen) == 1


async def test_http_timeout_does_not_retry():
    seen = []

    async def stalled(request):
        seen.append(request)
        await asyncio.sleep(1)
        return httpx.Response(500)

    async with MCPHTTPClient("https://example.test/mcp", transport=httpx.MockTransport(stalled), timeout=.01) as client:
        with pytest.raises(TimeoutError):
            await client.list_tools()
    assert len(seen) == 1


SERVER_SCRIPT = r'''
import json, os, sys, time
sys.stderr.write("X" * 5000 + "READY"); sys.stderr.flush()
for line in sys.stdin:
    m = json.loads(line)
    if "method" not in m or "id" not in m:
        continue
    method = m["method"]
    if method == "initialize":
        r = {"protocolVersion":"2024-11-05", "capabilities":{"tools":{}}, "serverInfo":{"name":"test-stdio","version":"1"}}
    elif method == "tools/list":
        r = {"tools":[{"name":"echo","description":"Unchanged","inputSchema":{"type":"object"},"_meta":{"a":1}}]}
    else:
        a = m["params"]["arguments"]
        if a.get("hang"):
            time.sleep(60)
        if a.get("malformed"):
            print("not-json", flush=True); continue
        if a.get("ping"):
            print(json.dumps({"jsonrpc":"2.0","id":"ping-1","method":"ping"}), flush=True)
            pong = json.loads(sys.stdin.readline())
            assert pong["result"] == {}
        if a.get("sampling"):
            print(json.dumps({"jsonrpc":"2.0","id":"sampling-1","method":"sampling/createMessage","params":{}}), flush=True)
            rejected = json.loads(sys.stdin.readline())
            assert rejected["error"]["code"] == -32601
        if a.get("error"):
            print(json.dumps({"jsonrpc":"2.0","id":m["id"],"error":{"code":-1,"message":"native","data":{"a":1}}}), flush=True); continue
        r = {"content":[{"type":"text","text":"native"}],"structuredContent":{"args":a,"env":dict(os.environ),"meta":m["params"].get("_meta")},"_meta":{"kept":True}}
    print(json.dumps({"jsonrpc":"2.0","id":m["id"],"result":r}), flush=True)
'''


async def test_stdio_preserves_native_protocol_and_minimizes_environment(monkeypatch):
    monkeypatch.setenv("PRIVATE_SENTINEL", "must-not-inherit")
    client = MCPStdioClient([sys.executable, "-u", "-c", SERVER_SCRIPT], stderr_limit=128)
    async with client:
        assert (await client.list_tools())["tools"][0]["_meta"] == {"a": 1}
        result = await client.call_tool("echo", {"ping": True, "sampling": True})
        assert result["_meta"] == {"kept": True}
        assert result["content"] == [{"type": "text", "text": "native"}]
        env = result["structuredContent"]["env"]
        assert "PRIVATE_SENTINEL" not in env
        assert env["HOME"] != str(Path.home())
        assert env["PATH"] == os.defpath
        temporary_home = Path(env["HOME"])
        assert temporary_home.is_dir()
        assert len(client.stderr_tail.encode()) <= 128
        assert client.stderr_tail.endswith("READY")
        assert client.protocol_version == "2024-11-05"
        pid = client.process_id
    assert not temporary_home.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    await client.aclose()


async def test_stdio_explicit_environment_and_error_do_not_break_session():
    async with MCPStdioClient([sys.executable, "-u", "-c", SERVER_SCRIPT], env={"ONLY_THIS": "yes"}) as client:
        with pytest.raises(MCPRPCError) as caught:
            await client.call_tool("echo", {"error": True})
        assert caught.value.error == {"code": -1, "message": "native", "data": {"a": 1}}
        result = await client.call_tool("echo", {})
        assert result["structuredContent"]["env"]["ONLY_THIS"] == "yes"
        assert "HOME" not in result["structuredContent"]["env"]


async def test_stdio_protocol_metadata_is_separate_from_native_arguments():
    arguments = {"query": "unchanged"}
    metadata = {"refund-eval/tool-call-id": "inspect-call-123", "nested": {"native": True}}
    async with MCPStdioClient([sys.executable, "-u", "-c", SERVER_SCRIPT]) as client:
        result = await client.call_tool("echo", arguments, meta=metadata)
        assert result["structuredContent"]["args"] == arguments
        assert result["structuredContent"]["meta"] == metadata
        assert "_meta" not in arguments


@pytest.mark.parametrize("mode", ["timeout", "cancel", "malformed"])
async def test_stdio_failure_cleans_process_and_does_not_reuse_stream(mode):
    async with MCPStdioClient([sys.executable, "-u", "-c", SERVER_SCRIPT], timeout=.2, shutdown_timeout=.2) as client:
        await client.initialize()
        pid = client.process_id
        if mode == "cancel":
            task = asyncio.create_task(client.call_tool("echo", {"hang": True}))
            await asyncio.sleep(.03)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError if mode == "timeout" else MCPProtocolError):
                await client.call_tool("echo", {"hang": True} if mode == "timeout" else {"malformed": True})
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        with pytest.raises(MCPError, match="failed"):
            await client.call_tool("echo", {})


def test_stdio_never_accepts_a_shell_command_string():
    with pytest.raises(ValueError, match="argument sequence"):
        MCPStdioClient("echo secret; anything")

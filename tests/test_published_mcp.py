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


class FramedChunks(httpx.AsyncByteStream):
    def __init__(self, payload, size):
        self.payload = payload
        self.size = size

    async def __aiter__(self):
        for at in range(0, len(self.payload), self.size):
            yield self.payload[at:at + self.size]


@pytest.mark.parametrize(("ending", "blank"), [
    (b"\n", b"\n"), (b"\r\n", b"\r\n"), (b"\r", b"\r"),
    (b"\r\n", b"\n"), (b"\n", b"\r"), (b"\r", b"\r\n"),
])
@pytest.mark.parametrize("bom", [False, True])
@pytest.mark.parametrize("chunk_size", [1, 2, 7, 4096])
async def test_http_sse_line_framing_preserves_lifecycle_and_payload(ending, blank, bom, chunk_size):
    seen = []
    notification = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}
    result = {**copy.deepcopy(RESULT), "structuredContent": {"text": "é\ufeffretained", "rows": [1, 2]}}

    def handle(request):
        message = json.loads(request.content)
        seen.append(message)
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        value = INIT if message["method"] == "initialize" else (
            CATALOG if message["method"] == "tools/list" else result)
        response = {"jsonrpc": "2.0", "id": message["id"], "result": value}
        # First data line directly follows the optional BOM. Subsequent comments
        # and multiple data lines exercise the same framing after that first line.
        body = (b"\xef\xbb\xbf" if bom else b"")
        body += b"data: " + json.dumps(notification).encode() + ending + blank
        body += b": heartbeat" + ending + b"event: message" + ending
        body += ending.join(b"data: " + line.encode() for line in
                            json.dumps(response, ensure_ascii=False, indent=2).split("\n"))
        body += ending + blank
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=FramedChunks(body, chunk_size))

    async with MCPHTTPClient("https://synthetic.example.test/mcp",
                             transport=httpx.MockTransport(handle)) as client:
        assert await client.list_tools() == CATALOG
        assert client.catalog_pages == [CATALOG]
        assert await client.call_tool("upstream_tool", {"text": "é"}) == result
        assert client.initialization_result == INIT
        assert list(client.notifications) == [notification] * 3
        assert client.catalog_generation == 3
    assert [message["method"] for message in seen] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert seen[-1]["params"] == {"name": "upstream_tool", "arguments": {"text": "é"}}


@pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
@pytest.mark.parametrize("failure", ["missing_blank", "wrong_id", "bad_json", "second_bom", "oversized"])
async def test_http_sse_framing_retains_error_and_size_guards(ending, failure):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        response = {"jsonrpc": "2.0", "id": 999 if failure == "wrong_id" else 1, "result": INIT}
        data = b"not-json" if failure == "bad_json" else json.dumps(response).encode()
        prefix = b"\xef\xbb\xbf"
        if failure == "second_bom":
            prefix += b"\xef\xbb\xbf"
        body = prefix + b"data: " + data + ending
        if failure != "missing_blank":
            body += ending
        limit = len(body) - 1 if failure == "oversized" else 4096
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=FramedChunks(body, len(body) if failure == "oversized" else 1)), limit

    # Build one exact response so the byte limit includes BOM and CR/LF bytes.
    request = httpx.Request("POST", "https://synthetic.example.test/mcp",
                            json={"jsonrpc": "2.0", "method": "initialize", "id": 1})
    response, limit = handle(request)
    seen.clear()

    def serve(request):
        seen.append(json.loads(request.content))
        return response

    async with MCPHTTPClient("https://synthetic.example.test/mcp",
                             transport=httpx.MockTransport(serve), max_response_bytes=limit) as client:
        with pytest.raises(MCPProtocolError, match="size limit" if failure == "oversized" else None):
            await client.list_tools()
    assert len(seen) == 1


@pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
async def test_http_sse_rpc_error_retained_with_bom_and_fragmentation(ending):
    handle, seen = http_server()
    error = {"code": -32602, "message": "Native é", "data": {"detail": ["unchanged"]}}
    response = None

    def fail(request):
        nonlocal response
        message = json.loads(request.content)
        if message["method"] != "tools/call":
            return handle(request)
        seen.append((request, message))
        response = {"jsonrpc": "2.0", "id": message["id"], "error": error}
        body = b"\xef\xbb\xbfdata: " + json.dumps(response, ensure_ascii=False).encode() + ending * 2
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=FramedChunks(body, 1))

    async with MCPHTTPClient("https://synthetic.example.test/mcp",
                             transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(MCPRPCError) as caught:
            await client.call_tool("upstream_tool", {})
        assert caught.value.error == error
        assert caught.value.response == response
    assert sum(m["method"] == "tools/call" for _, m in seen) == 1


@pytest.mark.parametrize("length", [8192, 16384])
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b"\r"])
async def test_http_sse_fragmented_long_lines_scan_each_byte_once(monkeypatch, length, ending):
    from refund_eval.published import mcp

    scan_lengths = []
    original_search = mcp.re.search

    def search(pattern, data, *args, **kwargs):
        if pattern == b"[\r\n]":
            scan_lengths.append(len(data))
        return original_search(pattern, data, *args, **kwargs)

    monkeypatch.setattr(mcp.re, "search", search)
    value = {"content": [{"type": "text", "text": "x" * length}]}
    seen = []
    encoded_size = 0

    def handle(request):
        nonlocal encoded_size
        message = json.loads(request.content)
        seen.append(message)
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        if message["method"] == "initialize":
            return rpc_response(message, INIT)
        response = {"jsonrpc": "2.0", "id": message["id"], "result": value}
        body = b"\xef\xbb\xbfdata: " + json.dumps(response).encode() + ending * 2
        encoded_size = len(body)
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=FramedChunks(body, 1))

    async with MCPHTTPClient("https://synthetic.example.test/mcp",
                             transport=httpx.MockTransport(handle)) as client:
        assert await client.call_tool("upstream_tool", {}) == value
    assert scan_lengths
    # Count actual scanner input, not runtime: incomplete prefixes must not be
    # searched again with each incoming byte. No payload-sensitive time limits.
    assert sum(scan_lengths) <= encoded_size * 2
    assert max(scan_lengths) <= 2
    assert [message["method"] for message in seen] == [
        "initialize", "notifications/initialized", "tools/call"]


def server_request_http(messages, *, during="tools/list", status=202, session=True,
                        reply_handler=None, native_error=None, end_without_result=False):
    """Hold the requested result until each preceding server request is answered."""
    seen = []
    replies = []
    closed = []

    class Incoming(httpx.AsyncByteStream):
        def __init__(self, request):
            self.request = request

        async def __aiter__(self):
            for message in messages:
                count = len(replies)
                data = b"data: " + json.dumps(message).encode() + b"\r\n\r\n"
                async for chunk in FramedChunks(data, 1):
                    yield chunk
                if "id" in message and "method" in message:
                    assert len(replies) == count + 1, "server request must be answered before continuing"
            if end_without_result:
                return
            value = INIT if during == "initialize" else CATALOG if during == "tools/list" else RESULT
            result = {"jsonrpc": "2.0", "id": self.request["id"],
                      "error" if native_error else "result": native_error or value}
            yield b"data: " + json.dumps(result).encode() + b"\n\n"

        async def aclose(self):
            closed.append(True)

    def handle(request):
        message = json.loads(request.content)
        seen.append((request, message))
        if "method" not in message:
            replies.append(message)
            return reply_handler(request) if reply_handler else httpx.Response(status)
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        if message["method"] == during:
            return httpx.Response(200, headers={"content-type": "text/event-stream",
                **({"Mcp-Session-Id": "session-duplex"} if during == "initialize" and session else {})},
                stream=Incoming(message))
        value = INIT if message["method"] == "initialize" else CATALOG
        return rpc_response(message, value, headers={"Mcp-Session-Id": "session-duplex"}
                            if message["method"] == "initialize" and session else {})

    return handle, seen, replies, closed


@pytest.mark.parametrize("during", ["initialize", "tools/list", "tools/call"])
@pytest.mark.parametrize("server_id", ["server-ping", 0, 1, 2])
@pytest.mark.parametrize("session", [False, True])
async def test_http_server_ping_preserves_lifecycle_and_request_ids(during, server_id, session):
    ping = {"jsonrpc": "2.0", "id": server_id, "method": "ping"}
    handle, seen, replies, closed = server_request_http([ping], during=during, session=session)
    async with MCPHTTPClient("https://synthetic.example.test/mcp", headers={"Authorization": "Bearer synthetic"},
                             transport=httpx.MockTransport(handle)) as client:
        if during == "tools/call":
            assert await client.call_tool("upstream_tool", {"unchanged": "é"}) == RESULT
        else:
            assert await client.list_tools() == CATALOG
            assert client.catalog_pages == [CATALOG]
        assert client.initialization_result == INIT
    assert replies == [{"jsonrpc": "2.0", "id": server_id, "result": {}}]
    requests = [message for _, message in seen if "method" in message]
    assert [m["method"] for m in requests] == [
        "initialize", "notifications/initialized", "tools/call" if during == "tools/call" else "tools/list"]
    assert [m["id"] for m in requests if "id" in m] == [1, 2]
    assert requests[0]["params"]["capabilities"] == {}
    for request, message in seen:
        assert request.method == "POST"
        assert str(request.url) == "https://synthetic.example.test/mcp"
        assert request.headers["authorization"] == "Bearer synthetic"
        assert request.headers["mcp-protocol-version"] == "2025-06-18"
        assert "application/json" in request.headers["accept"]
        assert "text/event-stream" in request.headers["accept"]
        assert request.headers.get("mcp-session-id") == (
            "session-duplex" if session and message.get("method") != "initialize" else None)
    assert closed


@pytest.mark.parametrize("native_error", [None, {"code": -32000, "message": "Native", "data": [1, "é"]}])
async def test_http_mixed_server_requests_notifications_and_native_response(native_error):
    notice = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {"raw": [1]}}
    messages = [notice,
        {"jsonrpc": "2.0", "method": "ping", "id": "p1"},
        {"jsonrpc": "2.0", "method": "sampling/createMessage", "id": 2, "params": {}},
        {"jsonrpc": "2.0", "method": "ping", "id": "p2"}, notice]
    handle, seen, replies, closed = server_request_http(messages, native_error=native_error)
    async with MCPHTTPClient("https://synthetic.example.test/mcp", transport=httpx.MockTransport(handle)) as client:
        if native_error:
            with pytest.raises(MCPRPCError) as caught:
                await client.list_tools()
            assert caught.value.error == native_error
            assert caught.value.response == {"jsonrpc": "2.0", "id": 2, "error": native_error}
        else:
            assert await client.list_tools() == CATALOG
        assert list(client.notifications) == [notice, notice]
        assert client.catalog_generation == 2
    assert replies == [{"jsonrpc": "2.0", "id": "p1", "result": {}},
        {"jsonrpc": "2.0", "id": 2,
         "error": {"code": -32601, "message": "Client capability not supported"}},
        {"jsonrpc": "2.0", "id": "p2", "result": {}}]
    assert len(seen) == 6
    assert closed


@pytest.mark.parametrize("changes", [
    {"id": None}, {"id": True}, {"id": 1.5}, {"id": []}, {"id": {}},
    {"method": None}, {"method": 1}, {"params": None}, {"params": "bad"},
    {"result": {}}, {"error": {}},
])
async def test_http_malformed_server_request_has_no_reply(changes):
    ping = {"jsonrpc": "2.0", "method": "ping", "id": "p", **changes}
    handle, seen, replies, closed = server_request_http([ping])
    async with MCPHTTPClient("https://synthetic.example.test/mcp", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(MCPProtocolError, match="Invalid MCP server request"):
            await client.list_tools()
    assert replies == []
    assert [m["method"] for _, m in seen] == ["initialize", "notifications/initialized", "tools/list"]
    assert closed


@pytest.mark.parametrize("status", [200, 204, 307, 400, 404, 500])
async def test_http_server_reply_rejection_closes_stream_without_retry(status):
    handle, seen, replies, closed = server_request_http(
        [{"jsonrpc": "2.0", "method": "ping", "id": "p"}], status=status)
    async with MCPHTTPClient("https://synthetic.example.test/mcp", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises((MCPProtocolError, httpx.HTTPStatusError)):
            await client.list_tools()
        if status == 404:
            assert client._session_id is None
            assert client._initialized is False
    assert len(replies) == 1
    assert len(seen) == 4
    assert closed


async def test_http_server_reply_uses_original_timeout_and_closes_stream():
    async def stall(request):
        await asyncio.sleep(1)
        return httpx.Response(202)

    handle, seen, replies, closed = server_request_http(
        [{"jsonrpc": "2.0", "method": "ping", "id": "p"}], reply_handler=stall)

    async def asynchronous_handle(request):
        response = handle(request)
        return await response if hasattr(response, "__await__") else response

    async with MCPHTTPClient("https://synthetic.example.test/mcp", timeout=.05,
                             transport=httpx.MockTransport(asynchronous_handle)) as client:
        with pytest.raises(TimeoutError):
            await client.list_tools()
    assert len(seen) == 4
    assert len(replies) == 1
    assert closed


async def test_http_server_reply_ack_body_is_closed_without_reading():
    closed_ack = []

    class NoBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError("202 acknowledgement must not be read")
            yield b""

        async def aclose(self):
            closed_ack.append(True)

    handle, _, replies, closed = server_request_http(
        [{"jsonrpc": "2.0", "method": "ping", "id": "p"}],
        reply_handler=lambda request: httpx.Response(202, stream=NoBody()))
    async with MCPHTTPClient("https://synthetic.example.test/mcp", transport=httpx.MockTransport(handle)) as client:
        assert await client.list_tools() == CATALOG
    assert replies
    assert closed_ack and closed


async def test_http_server_requests_still_count_toward_original_stream_limit():
    ping = {"jsonrpc": "2.0", "method": "ping", "id": "p"}
    handle, seen, replies, closed = server_request_http([ping] * 20)
    async with MCPHTTPClient("https://synthetic.example.test/mcp", max_response_bytes=1024,
                             transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(MCPProtocolError, match="size limit"):
            await client.list_tools()
    assert 1 < len(replies) < 20
    assert len([m for _, m in seen if m.get("method") == "tools/list"]) == 1
    assert closed


async def test_http_server_reply_transport_error_closes_original_stream():
    def fail(request):
        raise httpx.ConnectError("Synthetic connection failure", request=request)

    handle, seen, replies, closed = server_request_http(
        [{"jsonrpc": "2.0", "method": "ping", "id": "p"}], reply_handler=fail)
    async with MCPHTTPClient("https://synthetic.example.test/mcp", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(httpx.ConnectError):
            await client.list_tools()
    assert len(seen) == 4
    assert len(replies) == 1
    assert closed


async def test_http_server_ping_is_not_a_substitute_for_original_result():
    handle, seen, replies, closed = server_request_http(
        [{"jsonrpc": "2.0", "method": "ping", "id": 2}], end_without_result=True)
    async with MCPHTTPClient("https://synthetic.example.test/mcp", transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(MCPProtocolError, match="without a matching response"):
            await client.list_tools()
    assert replies == [{"jsonrpc": "2.0", "id": 2, "result": {}}]
    assert len(seen) == 4
    assert closed

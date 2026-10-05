"""Boundary tests and opt-in proof using the real pinned Chromium/MCP image."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from refund_eval.published.browser import (
    BROWSER_TOOLS,
    BrowserBoundaryError,
    PublishedBrowserService,
    docker_command,
    validate_response,
)
from refund_eval.published.mcp import MCPRPCError


def response(text: str, *, status: int = 200) -> dict:
    return {"status": status, "headers": {"content-type": "text/html"},
            "body_base64": base64.b64encode(text.encode()).decode()}


def prepare_service(tmp_path, handler):
    service = PublishedBrowserService(handler, spool_dir=tmp_path)
    (tmp_path / "requests").mkdir()
    (tmp_path / "responses").mkdir()
    return service


def write_request(service, **overrides):
    ident = str(uuid.uuid4())
    request = {"request_id": ident, "url": "https://operator.invalid/claim", "method": "POST",
               "headers": {"content-type": "application/x-www-form-urlencoded"},
               "body_base64": base64.b64encode(b"booking=fixture").decode(),
               "resource_type": "document", "is_navigation": True, "tool_call_id": "call-1",
               **overrides}
    filename = service.spool_dir / "requests" / f"{ident}.json"
    filename.write_text(json.dumps(request))
    return filename


def test_container_has_no_outbound_path_or_host_filesystem(tmp_path):
    command = docker_command(tmp_path, "trial")
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--dns") + 1] == "127.0.0.1"
    assert "--read-only" in command
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert [v for v in command if v.startswith("type=bind")] == [f"type=bind,source={tmp_path},target=/exchange"]
    assert "browser_run_code_unsafe" not in BROWSER_TOOLS
    assert "browser_file_upload" not in BROWSER_TOOLS


async def test_request_is_audited_before_handler_and_preserves_body(tmp_path):
    calls = []

    async def handler(request):
        records = [json.loads(line) for line in service.audit_path.read_text().splitlines()]
        assert records[-1]["event"] == "request"
        calls.append(request)
        return response("Accepted")

    service = prepare_service(tmp_path, handler)
    service._active_call = {"tool": "browser_click", "tool_call_id": "call-1"}
    filename = write_request(service)
    await service._handle_request(filename)
    assert len(calls) == 1
    assert calls[0]["tool_call_id"] == "call-1"
    assert base64.b64decode(calls[0]["body_base64"]) == b"booking=fixture"
    stored = json.loads((tmp_path / "responses" / filename.name).read_text())
    assert stored == response("Accepted")
    records = [json.loads(line) for line in service.audit_path.read_text().splitlines()]
    assert [entry["sequence"] for entry in records] == [1, 2]


@pytest.mark.parametrize("kind", ["background", "stale", "websocket"])
async def test_background_stale_and_unsupported_requests_never_dispatch(tmp_path, kind):
    async def handler(_):
        pytest.fail("Request must not cross the host policy boundary")

    service = prepare_service(tmp_path, handler)
    if kind != "background":
        service._active_call = {"tool": "browser_evaluate", "tool_call_id": "call-1"}
    options = {"tool_call_id": "earlier-call"} if kind == "stale" else {}
    if kind == "websocket":
        options.update(method="WEBSOCKET", rejected="websocket_not_supported")
    filename = write_request(service, **options)
    await service._handle_request(filename)
    stored = json.loads((tmp_path / "responses" / filename.name).read_text())
    assert stored.get("abort") or stored["status"] == 503
    assert json.loads(service.audit_path.read_text().splitlines()[0])["event"] == "request"


@pytest.mark.parametrize("kind", ["throws", "timeout", "malformed"])
async def test_policy_failures_abort_without_fallback(tmp_path, kind):
    async def handler(_):
        if kind == "throws":
            raise RuntimeError("Simulator unavailable")
        if kind == "timeout":
            await asyncio.sleep(1)
        return {"status": 200, "headers": {}, "body_base64": "invalid base64!"}

    service = prepare_service(tmp_path, handler)
    service.request_timeout = 0.01
    service._active_call = {"tool": "browser_click", "tool_call_id": "call-1"}
    filename = write_request(service)
    await service._handle_request(filename)
    assert json.loads((tmp_path / "responses" / filename.name).read_text()) == {"abort": True}
    assert json.loads(service.audit_path.read_text().splitlines()[-1])["event"] == "request_error"


async def test_spool_symlink_cannot_read_host_files(tmp_path):
    async def handler(_):
        pytest.fail("Symlink must not dispatch")

    service = prepare_service(tmp_path, handler)
    secret = tmp_path / "secret"
    secret.write_text("private-canary")
    filename = tmp_path / "requests" / f"{uuid.uuid4()}.json"
    filename.symlink_to(secret)
    await service._handle_request(filename)
    assert "private-canary" not in service.audit_path.read_text()
    assert secret.read_text() == "private-canary"


def test_invalid_boundary_responses_are_not_treated_as_success():
    for value in [None, {}, {"status": True}, {"status": 101, "headers": {}, "body_base64": ""},
                  {"status": 200, "headers": {"cookie": 3}, "body_base64": ""}]:
        with pytest.raises(BrowserBoundaryError):
            validate_response(value)


async def test_native_rpc_error_preserves_browser_for_a_followup(tmp_path, monkeypatch):
    native_error = {"code": -32602, "message": "Invalid parameter"}
    native_response = {"jsonrpc": "2.0", "id": 123, "error": native_error}

    class Client:
        calls = 0

        async def call_tool(self, name, arguments, *, meta):
            self.calls += 1
            if self.calls == 1:
                raise MCPRPCError(native_error, response=native_response)
            return {"content": [{"type": "text", "text": "Recovered"}]}

    async def no_catalog_start():
        return {"tools": []}

    async def never_called(_):
        pytest.fail("Snapshot must not request the network")

    service = prepare_service(tmp_path, never_called)
    service._client = Client()
    service.security_manifest["verified"] = True
    monkeypatch.setattr(service, "list_tools", no_catalog_start)
    with pytest.raises(MCPRPCError):
        await service.call_tool("browser_snapshot", {}, tool_call_id="failed")
    assert service._closed is False
    result = await service.call_tool("browser_snapshot", {}, tool_call_id="retry")
    assert result["content"][0]["text"] == "Recovered"
    records = [json.loads(line) for line in service.audit_path.read_text().splitlines()]
    rpc_error = next(record for record in records if record["event"] == "browser_rpc_error")
    assert rpc_error["error"] == native_error
    assert rpc_error["rpc_response"] == native_response
    assert all(record["completed"] for record in records if record["event"] == "browser_tool_end")


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1",
                    reason="Requires the locally built pinned browser container")
async def test_real_browser_form_and_network_bypass_canary(tmp_path):
    """Real upstream tools submit a real form; an independent listener sees zero writes."""
    hits = []

    class Canary(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 0), Canary)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    calls = []
    fail_writes = False
    canary_url = f"http://host.docker.internal:{server.server_port}/claim"

    async def handler(request):
        calls.append(request)
        if request["url"].endswith("/redirect"):
            return {"status": 302, "headers": {"location": "http://fixture.invalid/form"}, "body_base64": ""}
        if request["method"] == "POST":
            if fail_writes:
                raise RuntimeError("Simulator stopped")
            return response("<h1>Claim received</h1>")
        return response(f'<form action="{canary_url}" method="post"><label>Booking<input name="booking" id="booking"></label><button>Submit claim</button></form>')

    browser = PublishedBrowserService(handler, spool_dir=tmp_path / "exchange")
    try:
        async with browser:
            catalog = await browser.list_tools()
            assert {t["name"] for t in catalog["tools"]} == BROWSER_TOOLS
            await browser.call_tool("browser_navigate", {"url": "http://fixture.invalid/form"}, tool_call_id="read")
            await browser.call_tool("browser_type", {"target": "#booking", "text": "BOOKING-CANARY"}, tool_call_id="fill")
            result = await browser.call_tool("browser_click", {"target": "button"}, tool_call_id="send")
            assert not result.get("isError")
            # This upstream release saves automatic snapshots to files; its
            # explicit snapshot tool returns the accessibility tree inline.
            snapshot = await browser.call_tool("browser_snapshot", {}, tool_call_id="observe")
            assert "Claim received" in json.dumps(snapshot)
            writes = [request for request in calls if request["method"] == "POST"]
            assert len(writes) == 1 and writes[0]["tool_call_id"] == "send"
            assert b"BOOKING-CANARY" in base64.b64decode(writes[0]["body_base64"])

            script = await browser.call_tool("browser_evaluate", {
                "function": "async () => await fetch('/scripted', {method:'POST', body:'scripted-write'}).then(r=>r.text())"
            }, tool_call_id="scripted-write")
            assert "Claim received" in json.dumps(script)
            assert any(call["tool_call_id"] == "scripted-write" and call["method"] == "POST" for call in calls)
            socket = await browser.call_tool("browser_evaluate", {"function":
                "() => new Promise(resolve => {const s=new WebSocket('" + canary_url.replace("http:", "ws:") + "');"
                "s.onclose=()=>resolve('closed');s.onerror=()=>resolve('error');})"
            }, tool_call_id="websocket")
            assert "closed" in json.dumps(socket) or "error" in json.dumps(socket)
            assert not any(call["method"] == "WEBSOCKET" for call in calls)

            await browser.call_tool("browser_navigate", {"url": "http://fixture.invalid/redirect"}, tool_call_id="redirect")
            assert [call["url"] for call in calls if call["tool_call_id"] == "redirect"] == [
                "http://fixture.invalid/redirect", "http://fixture.invalid/form"]

            # Bypass Playwright routing entirely from the real tool process's
            # network namespace. The independent host listener must stay quiet.
            probe = """const net=require('node:net');
const hosts=['host.docker.internal','0.250.250.254','192.168.65.254','1.1.1.1','2606:4700:4700::1111'];
Promise.all(hosts.map(host=>new Promise(resolve=>{const s=net.connect({host,port:PORT});
let done=false;const finish=connected=>{if(done)return;done=true;s.destroy();resolve({host,connected});};
s.on('connect',()=>finish(true));s.on('error',()=>finish(false));s.setTimeout(1500,()=>finish(false));
setTimeout(()=>finish(false),2000); }))).then(r=>console.log(JSON.stringify(r)));""".replace("PORT", str(server.server_port))
            process = await asyncio.create_subprocess_exec("docker", "exec", browser.container_name,
                "node", "-e", probe, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, stderr.decode()
            assert all(not entry["connected"] for entry in json.loads(stdout))

            fail_writes = True
            await browser.call_tool("browser_navigate", {"url": "http://fixture.invalid/form"})
            failed = await browser.call_tool("browser_click", {"target": "button"}, tool_call_id="failed-send")
            assert failed.get("isError") or "chrome-error://" in json.dumps(failed)
            failed_snapshot = await browser.call_tool("browser_snapshot", {})
            assert "Claim received" not in json.dumps(failed_snapshot)
            assert hits == []
    finally:
        await browser.aclose()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    records = [json.loads(line) for line in browser.audit_path.read_text().splitlines()]
    assert any(record["event"] == "request_error" for record in records)
    process = await asyncio.create_subprocess_exec("docker", "inspect", browser.container_name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    assert await process.wait() != 0


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1",
                    reason="Requires the locally built pinned browser container")
async def test_real_browser_cancellation_stops_pending_boundary_and_container(tmp_path):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    browser = PublishedBrowserService(handler, spool_dir=tmp_path / "exchange")
    try:
        await browser.list_tools()
        call = asyncio.create_task(browser.call_tool("browser_navigate", {"url": "http://fixture.invalid/wait"}))
        await asyncio.wait_for(entered.wait(), 10)
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert cancelled.is_set()
    finally:
        await browser.aclose()
    process = await asyncio.create_subprocess_exec("docker", "inspect", browser.container_name,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    assert await process.wait() != 0


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1",
                    reason="Requires the locally built pinned browser container")
async def test_real_browser_with_inspect_adapter_and_scenario_boundary(tmp_path, monkeypatch):
    from inspect_ai.util import store
    from inspect_ai.util._store import Store, init_subtask_store

    from refund_eval.agent.harness import initial_world
    from refund_eval.published import adapter
    from refund_eval.published.network import BrowserBoundary
    from refund_eval.scenario.schema import load_scenario
    from refund_eval.sim.interface import SimResult
    from refund_eval.world.interception import ToolContext
    from refund_eval.world.mail import Mailbox, make_mail_source
    from refund_eval.world.state import load_world, save_world

    init_subtask_store(Store())
    scenario = load_scenario("train_refund")
    save_world(initial_world(scenario))
    ctx = ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))
    initial_url = "http://thetrainline.com/form"
    form_url = "http://thetrainline.com/claim"
    dispatched = []

    async def forbidden_fetch(_):
        pytest.fail("An offline fixture request reached the live read transport")

    async def dispatch(inbound):
        dispatched.append(inbound)
        return SimResult(immediate_text="Your case is open.")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    boundary = BrowserBoundary(ctx, public_read_urls=[], simulated_form_urls=[form_url],
        fetch=forbidden_fetch,
        fixtures={initial_url: response(f'<form action="{form_url}" method="post"><input name="booking" id="booking"><button>Submit claim</button></form>')})
    browser = PublishedBrowserService(boundary, spool_dir=tmp_path / "exchange", audit=adapter.audit)
    current_call = "setup"
    monkeypatch.setattr(adapter, "current_tool_call_id", lambda: current_call)
    async with browser:
        tools = {tool.name: tool.tool for tool in await adapter.inspect_tools(browser, source="playwright")}
        current_call = "navigate"
        await tools["browser_navigate"](url=initial_url)
        current_call = "fill"
        await tools["browser_type"](target="#booking", text="INTEGRATION-CANARY")
        current_call = "send"
        await tools["browser_click"](target="button")
        current_call = "observe"
        result = await tools["browser_snapshot"]()
        assert "Your case is open" in "".join(part.text for part in result)
        assert len(dispatched) == 1
        assert dispatched[0].fields == {"booking": "INTEGRATION-CANARY"}
        assert load_world().interceptions[0].payload["tool_call_id"] == "send"
        current_call = "reload"
        await tools["browser_navigate"](url=form_url)
        current_call = "observe_reload"
        result = await tools["browser_snapshot"]()
        assert "Your case is open" in "".join(part.text for part in result)
        assert len(dispatched) == 1
        assert browser.security_manifest["verified"] is True
    audit = store().get(adapter.AUDIT_KEY)
    assert any(event["event"] == "browser_decision" and event["action"] == "overlay" for event in audit)
    assert any(event["event"] == "browser_tool_end" and event["call_id"] == "observe"
               and event["request_ids"] == [] and event["boundary_verified"] for event in audit)

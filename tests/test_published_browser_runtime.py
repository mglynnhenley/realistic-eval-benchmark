"""Subprocess lifecycle and independent interception evidence."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from refund_eval.published.browser import BrowserBoundaryError, PublishedBrowserService
from refund_eval.published.browser_runtimes import (
    SubprocessBrowserRuntime,
    subprocess_environment,
)

pytestmark = pytest.mark.browser


def test_subprocess_environment_cannot_inherit_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "credential-canary")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "credential-canary")
    monkeypatch.setenv("HTTP_PROXY", "http://credential-canary.invalid")
    env = subprocess_environment(tmp_path / "home", tmp_path / "exchange",
                                 tmp_path / "workspace", tmp_path / "output")
    assert set(env) == {"PATH", "HOME", "NODE_ENV", "PLAYWRIGHT_BROWSERS_PATH",
                        "REFUND_EVAL_BROWSER_EXCHANGE", "REFUND_EVAL_BROWSER_WORKSPACE",
                        "REFUND_EVAL_BROWSER_OUTPUT"}
    assert "credential-canary" not in json.dumps(env)
    assert env["HOME"] == str(tmp_path / "home")


@pytest.mark.parametrize("escape", [False, True, "missing"])
async def test_startup_canary_requires_spool_evidence_and_zero_connections(tmp_path, escape):
    async def policy(_):
        pytest.fail("A diagnostic request must never reach scenario policy or simulator")

    service = PublishedBrowserService(policy, spool_dir=tmp_path / "exchange", runtime="subprocess")
    (service.spool_dir / "requests").mkdir(parents=True)
    (service.spool_dir / "responses").mkdir()
    service.security_manifest.update({"verified": False, "mode": "subprocess-shared-network"})

    class Client:
        async def call_tool(self, name, arguments, *, meta):
            url = arguments["url"]
            if url == "about:blank":
                return {"content": []}
            probe_id = meta["refund-eval/tool-call-id"]
            if escape != "missing":
                request_id = str(uuid.uuid4())
                filename = service.spool_dir / "requests" / f"{request_id}.json"
                filename.write_text(json.dumps({
                    "request_id": request_id, "url": url, "method": "GET", "headers": {},
                    "body_base64": "", "resource_type": "document", "tool_call_id": probe_id,
                }))
                await service._handle_request(filename)
                response = json.loads((service.spool_dir / "responses" / filename.name).read_text())
                assert response["status"] == 200
            if escape is True:
                parsed = urlsplit(url)
                _, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
                writer.close()
                await writer.wait_closed()
            return {"content": []}

    service._client = Client()
    if escape:
        with pytest.raises(BrowserBoundaryError, match="integrity check failed"):
            await service._verify_interception_integrity()
    else:
        await service._verify_interception_integrity()
    evidence = service.security_manifest["interception_integrity"]
    assert evidence["verified"] is (escape is False)
    assert evidence["request_observed"] is (escape != "missing")
    assert evidence["listener_connections"] == int(escape is True)
    assert service.security_manifest["verified"] is False  # Interception is not OS isolation.
    assert service._active_probe is None
    events = [json.loads(line) for line in service.audit_path.read_text().splitlines()]
    assert events[0]["event"] == "browser_probe_start"
    assert events[-1]["event"] == "browser_probe_end"
    assert all(event["event"].startswith("browser_probe_") for event in events)
    assert all("call_id" not in event and "tool_call_id" not in event for event in events)


async def test_shared_network_tool_events_use_integrity_evidence(tmp_path, monkeypatch):
    async def policy(_):
        pytest.fail("Snapshot makes no network request")

    service = PublishedBrowserService(policy, spool_dir=tmp_path / "exchange", runtime="subprocess")
    service.security_manifest = {
        "evidence_version": 2, "verified": False, "mode": "subprocess-shared-network",
        "interception_integrity": {"verified": True},
    }

    class Client:
        async def call_tool(self, *_args, **_kwargs):
            return {"content": []}

    async def catalog():
        return {"tools": []}

    service._client = Client()
    monkeypatch.setattr(service, "list_tools", catalog)
    await service.call_tool("browser_snapshot", {}, tool_call_id="model-call")
    records = [json.loads(line) for line in service.audit_path.read_text().splitlines()]
    assert all(event["boundary_verified"] is True for event in records)
    assert all(event["call_id"] == "model-call" for event in records)


async def test_subprocess_namespace_evidence_requires_distinct_namespace_and_loopback(tmp_path, monkeypatch):
    class Client:
        process_id = 12345

    runtime = SubprocessBrowserRuntime(tmp_path, timeout=90, audit_path=tmp_path / "audit")
    runtime.client = Client()
    runtime._namespace = True
    runtime._component_info = {"chromium_executable": "/opt/chromium"}
    monkeypatch.setattr(os, "readlink", lambda path: "net:[2]" if "/12345/" in path else "net:[1]")
    monkeypatch.setattr(Path, "read_text", lambda self: "header\nheader\n lo: 0 0 0\n")
    manifest = await runtime.verify_isolation()
    assert manifest["verified"] is True
    assert manifest["network_interfaces"] == ["lo"]
    assert manifest["mode"] == "subprocess-netns"
    monkeypatch.setattr(os, "readlink", lambda path: "net:[1]")
    with pytest.raises(BrowserBoundaryError, match="not isolated"):
        await runtime.verify_isolation()
    monkeypatch.setattr(os, "readlink", lambda path: "net:[2]" if "/12345/" in path else "net:[1]")
    monkeypatch.setattr(Path, "read_text", lambda self: "header\nheader\n lo: 0\n eth0: 0\n")
    with pytest.raises(BrowserBoundaryError, match="not isolated"):
        await runtime.verify_isolation()


def test_audit_cannot_be_placed_in_browser_exchange(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        PublishedBrowserService(None, spool_dir=tmp_path, audit_path=tmp_path / "network.jsonl")


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_SUBPROCESS_BROWSER_SMOKE") != "1",
                    reason="Requires pinned packaged Node, Playwright and Chromium")
async def test_real_subprocess_browser_navigation_write_and_cleanup(tmp_path):
    calls = []

    async def policy(request):
        calls.append(request)
        text = "<title>Authored page</title><h1>Fixture browser</h1>" if request["method"] == "GET" else "Write blocked"
        return {"status": 200 if request["method"] == "GET" else 403,
                "headers": {"content-type": "text/html"}, "body_base64": base64.b64encode(text.encode()).decode()}

    browser = PublishedBrowserService(policy, spool_dir=tmp_path / "exchange", runtime="subprocess",
                                      audit_path=tmp_path / "artifacts" / "browser.network.jsonl")
    async with browser:
        process_id = browser._client.process_id
        assert browser.security_manifest["interception_integrity"]["verified"] is True
        await browser.call_tool("browser_navigate", {"url": "http://fixture.invalid/"}, tool_call_id="read")
        result = await browser.call_tool("browser_evaluate", {
            "function": "async () => ({ title: document.title, status: (await fetch('/write', {method:'POST', body:'blocked'})).status })",
        }, tool_call_id="write")
        assert "Authored page" in json.dumps(result)
        assert "403" in json.dumps(result)
        assert any(call["method"] == "POST" and call["tool_call_id"] == "write" for call in calls)
        assert all(call["attribution"] == "active_tool_window" for call in calls)
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)
    assert browser.audit_path.is_file()


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_SUBPROCESS_BROWSER_SMOKE") != "1",
                    reason="Requires pinned packaged Node, Playwright and Chromium")
@pytest.mark.parametrize("failure", ["cancel", "crash"])
async def test_real_subprocess_failure_reaps_browser_process_group(tmp_path, failure):
    import signal

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def policy(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    browser = PublishedBrowserService(policy, spool_dir=tmp_path / "exchange", runtime="subprocess")
    try:
        await browser.initialize()
        process_id = browser._client.process_id
        pending = asyncio.create_task(browser.call_tool("browser_navigate", {"url": "http://fixture.invalid/wait"}))
        await asyncio.wait_for(entered.wait(), timeout=10)
        if failure == "cancel":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            os.kill(process_id, signal.SIGKILL)
            with pytest.raises(Exception, match="subprocess closed stdout"):
                await asyncio.wait_for(pending, timeout=10)
        assert cancelled.is_set()
        assert browser._closed
        # Probe the session, not just Node: no Chromium descendant may survive.
        # Some hosts briefly retain reparented dead children until init reaps.
        for _ in range(50):
            try:
                os.killpg(process_id, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("Browser process group survived cancellation or transport failure")
    finally:
        await browser.aclose()

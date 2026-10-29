"""Exercise browser reopen with the pinned MCP and Chromium, entirely offline."""

import base64
import json
import os
import shutil
from pathlib import Path

import pytest

from refund_eval.published import browser as browser_module
from refund_eval.published.browser import PublishedBrowserService
from refund_eval.published.browser_runtimes import RUNTIME_DIR


@pytest.mark.browser
@pytest.mark.parametrize("reopen", ["navigate", "new_tab", "last_tab", "repeat_close"])
async def test_real_browser_reopens_after_close_with_interception(tmp_path, monkeypatch, reopen):
    modules = os.environ.get("REFUND_EVAL_BROWSER_TEST_MODULES")
    browsers = os.environ.get("REFUND_EVAL_BROWSER_TEST_BROWSERS")
    if not modules or not browsers:
        pytest.skip("Requires pinned MCP packages and Chromium for the offline integration check")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    shutil.copyfile(RUNTIME_DIR / "server.mjs", runtime / "server.mjs")
    (runtime / "node_modules").symlink_to(Path(modules).resolve(), target_is_directory=True)
    monkeypatch.setenv("REFUND_EVAL_BROWSER_SERVER", str(runtime / "server.mjs"))
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", browsers)
    monkeypatch.setattr(browser_module, "_sim_time_meta", lambda: {
        "refund-eval/sim-time": "2026-10-15T09:00:00+01:00"})
    calls = []

    async def policy(request):
        calls.append(request)
        writing = request["method"] == "POST"
        text = "Write refused" if writing else "<title>Offline fixture</title><h1>Reopened page</h1>"
        return {"status": 403 if writing else 200,
                "headers": {"content-type": "text/html"},
                "body_base64": base64.b64encode(text.encode()).decode()}

    service = PublishedBrowserService(policy, spool_dir=tmp_path / "exchange", runtime="subprocess",
                                      audit_path=tmp_path / "private" / "network.jsonl")
    async with service:
        first = await service.call_tool("browser_navigate", {"url": "https://fixture.invalid/before"},
                                        tool_call_id="before")
        assert not first.get("isError"), first
        closed = await service.call_tool(
            "browser_tabs" if reopen == "last_tab" else "browser_close",
            {"action": "close"} if reopen == "last_tab" else {}, tool_call_id="close")
        assert not closed.get("isError"), closed
        if reopen == "repeat_close":
            closed = await service.call_tool("browser_close", {}, tool_call_id="close-again")
            assert not closed.get("isError"), closed
        if reopen != "new_tab":
            result = await service.call_tool("browser_navigate", {"url": "https://fixture.invalid/after"},
                                             tool_call_id="reopen")
        else:
            result = await service.call_tool("browser_tabs", {
                "action": "new", "url": "https://fixture.invalid/after"}, tool_call_id="reopen")
        assert not result.get("isError"), result
        snapshot = await service.call_tool("browser_snapshot", {}, tool_call_id="snapshot")
        assert "Reopened page" in json.dumps(snapshot)
        clock = await service.call_tool("browser_evaluate", {
            "function": "() => new Date().toISOString()"}, tool_call_id="clock")
        assert "2026-10-15T08:00:" in json.dumps(clock), clock
        write = await service.call_tool("browser_evaluate", {
            "function": "async () => (await fetch('/write', {method:'POST', body:'unchanged'})).status",
        }, tool_call_id="write")
        assert not write.get("isError"), write
        assert "403" in json.dumps(write)
        assert any(call["url"] == "https://fixture.invalid/after"
                   and call["tool_call_id"] == "reopen" for call in calls)
        assert any(call["method"] == "POST" and call["tool_call_id"] == "write"
                   and base64.b64decode(call["body_base64"]) == b"unchanged" for call in calls)

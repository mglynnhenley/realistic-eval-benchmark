"""Exercise the real Node wrapper and Python service with real console files.

Only upstream Chromium/MCP are replaced by an independent offline service. Its
artifact behavior is based on the pinned LogFile/Events format, not the repair.
"""

import json
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from refund_eval.published import browser
from refund_eval.published.browser_runtimes import RUNTIME_DIR, subprocess_environment
from refund_eval.published.mcp import MCPStdioClient


@asynccontextmanager
async def artifact_service(tmp_path, monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the real wrapper's artifact test")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    shutil.copyfile(RUNTIME_DIR / "server.mjs", runtime / "server.mjs")
    fixture = Path(__file__).parent / "fixtures/browser_artifact_upstream.mjs"
    for package, exports in [("@playwright/mcp", {".": "./index.js"}),
                             ("playwright", {".": "./index.js"}),
                             ("@modelcontextprotocol/sdk", {"./server/stdio.js": "./index.js"})]:
        directory = runtime / "node_modules" / package
        directory.mkdir(parents=True)
        (directory / "package.json").write_text(json.dumps({"type": "module", "exports": exports}))
        (directory / "index.js").write_text("export * from " + json.dumps(fixture.as_uri()) + ";\n")
    home, workspace, output = (tmp_path / name for name in ("home", "workspace", "output"))
    for directory in (home, workspace, output):
        directory.mkdir()
    exchange = tmp_path / "exchange"
    exchange.mkdir()
    env = subprocess_environment(home, exchange, workspace, output)
    env["FIXTURE_BROWSER_TOOLS"] = json.dumps(sorted(browser.BROWSER_TOOLS))
    client = MCPStdioClient([node, str(runtime / "server.mjs")], env=env, cwd=workspace)
    service = browser.PublishedBrowserService(None, spool_dir=exchange, runtime="subprocess",
                                              audit_path=tmp_path / "private.network.jsonl")
    service._client = client
    clock = {"now": "2026-10-12T09:00:00+01:00"}
    monkeypatch.setattr(browser, "_sim_time_meta", lambda: {"refund-eval/sim-time": clock["now"]})
    try:
        await client.initialize()
        yield service, workspace, output, clock
    finally:
        await service.aclose()


def inspect_result(result):
    text = result["content"][0]["text"]
    facts = json.loads(text.split("### Result\n", 1)[1].split("\n", 1)[0])
    reference = text.rsplit("- New console entries: ", 1)[1].split("\n", 1)[0]
    file, fragment = reference.rsplit("#", 1)
    return text, facts, file, fragment


async def test_console_alias_is_a_real_live_file_and_raw_audit_survives(tmp_path, monkeypatch):
    async with artifact_service(tmp_path, monkeypatch) as (service, workspace, output, clock):
        before = time.time() * 1000
        first = await service.call_tool("browser_snapshot", {}, tool_call_id="first")
        text, facts, file, fragment = inspect_result(first)
        alias = (workspace / file).resolve()
        original = Path(facts["rawFile"])
        assert alias.name.startswith("console-2026-10-12T08-00-00-000Z")
        assert alias != original and alias.parent == output
        assert alias.is_file() and original.is_file()
        assert alias.stat().st_ino == original.stat().st_ino
        assert alias.read_bytes() == original.read_bytes()
        assert fragment == "L1"
        assert "Published 27 October 2026" in text
        assert "- New console entries: " + facts["reference"] + "\n```" in text
        assert "Published 27 October 2026" in alias.read_text()
        assert original.name in alias.read_text()
        assert before <= facts["hostStart"] <= facts["hostEnd"] <= time.time() * 1000
        assert 2 <= facts["hostEnd"] - facts["hostStart"] < 2000
        assert "_meta" not in first

        clock["now"] = "2026-10-13T11:00:00+01:00"
        second = await service.call_tool("browser_snapshot", {}, tool_call_id="second")
        _, second_facts, second_file, second_fragment = inspect_result(second)
        assert second_file == file and second_fragment == "L2"
        assert second_facts["simulatedTime"] == "2026-10-13T10:00:00.000Z"
        assert alias.read_bytes() == original.read_bytes()
        assert len(alias.read_text().splitlines()) == 2

    audit = [json.loads(line) for line in service.audit_path.read_text().splitlines()]
    maps = [event for event in audit if event["event"] == "browser_artifact_mapping"]
    assert [event["call_id"] for event in maps] == ["first", "second"]
    assert maps[0]["raw_result"] == {"content": [{"type": "text", "text": text.replace(
        "### Events\n- New console entries: " + file + "#L1",
        "### Events\n- New console entries: " + facts["reference"])}]}
    assert maps[0]["mappings"][0]["original"] == str(original)
    assert maps[0]["mappings"][0]["alias"] == str(alias)
    assert service.audit_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mode", ["outside", "symlink", "missing", "unstructured"])
async def test_unowned_or_unstructured_paths_are_not_fabricated(tmp_path, monkeypatch, mode):
    async with artifact_service(tmp_path, monkeypatch) as (service, _, output, _):
        result = await service.call_tool("browser_snapshot", {"mode": mode})
        _, facts, file, fragment = inspect_result(result)
        assert file + "#" + fragment == facts["reference"]
        assert not list(output.glob("console-2026-10-12*"))
        assert "_meta" not in result


@pytest.mark.parametrize("mode", ["absolute", "range", "metadata"])
async def test_native_reference_forms_and_metadata_survive(tmp_path, monkeypatch, mode):
    async with artifact_service(tmp_path, monkeypatch) as (service, workspace, _, _):
        result = await service.call_tool("browser_snapshot", {"mode": mode})
        _, facts, file, fragment = inspect_result(result)
        alias = workspace / file
        assert alias.is_file() and alias.samefile(facts["rawFile"])
        assert alias.name.startswith("console-2026-10-12T08-00-00-000Z")
        assert Path(file).is_absolute() == (mode == "absolute")
        assert fragment == ("L1-L2" if mode == "range" else "L1")
        if mode == "range":
            assert len(alias.read_text().splitlines()) == 2
        assert result.get("_meta") == ({"native": "preserve"} if mode == "metadata" else None)


@pytest.mark.parametrize("mode", ["error", "large", "no_time", "invalid_time"])
async def test_error_large_or_unclocked_results_remain_native(tmp_path, monkeypatch, mode):
    async with artifact_service(tmp_path, monkeypatch) as (service, _, _, clock):
        if mode == "no_time":
            clock["now"] = None
        if mode == "invalid_time":
            clock["now"] = "not-a-time"
        result = await service.call_tool("browser_snapshot", {"mode": mode})
        _, facts, file, fragment = inspect_result(result)
        assert file + "#" + fragment == facts["reference"]
        assert "_meta" not in result
    audit = [json.loads(line) for line in service.audit_path.read_text().splitlines()]
    assert not any(event["event"] == "browser_artifact_mapping" for event in audit)


async def test_distinct_logs_and_preexisting_alias_never_collide(tmp_path, monkeypatch):
    async with artifact_service(tmp_path, monkeypatch) as (service, workspace, output, _):
        occupied = output / "console-2026-10-12T08-00-00-000Z-1.log"
        occupied.write_text("Preexisting unrelated artifact")
        first = await service.call_tool("browser_snapshot", {})
        _, facts, file, _ = inspect_result(first)
        second = await service.call_tool("browser_snapshot", {"mode": "another_file"})
        _, _, second_file, _ = inspect_result(second)
        assert second_file != file
        assert (workspace / file).samefile(facts["rawFile"])
        assert (workspace / second_file).is_file()
        assert not (workspace / second_file).samefile(workspace / file)
        assert occupied.read_text() == "Preexisting unrelated artifact"

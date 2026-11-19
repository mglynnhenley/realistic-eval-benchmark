"""Offline startup regressions for the opt-in verified browser boundary."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from fixtures.simulator import start_sample

from refund_eval.published import browser as browser_module
from refund_eval.published import browser_runtimes, profile
from refund_eval.published.browser import BROWSER_TOOLS, PublishedBrowserService
from refund_eval.published.browser_runtimes import BrowserBoundaryError
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.store import world_store

VALID_DOCKER_MANIFEST = {
    "verified": True,
    "mode": "docker-network-none",
    "network_mode": "none",
}
VALID_SUBPROCESS_MANIFEST = {
    "verified": True,
    "mode": "subprocess-netns",
    "network_mode": "none",
    "namespace_verified": True,
}


class OfflineClient:
    process_id = 12345

    def __init__(self, spool_dir: Path):
        self.spool_dir = spool_dir
        self.list_tools_calls = 0
        self.call_tool_calls = 0

    async def initialize(self):
        return {}

    async def list_tools(self):
        self.list_tools_calls += 1
        return {"tools": [
            {"name": name, "inputSchema": {"type": "object"}}
            for name in BROWSER_TOOLS
        ]}

    async def call_tool(self, name, arguments, *, meta):
        self.call_tool_calls += 1
        if name != "browser_navigate":
            return {"content": []}
        url = arguments["url"]
        if url == "about:blank":
            return {"content": []}
        request_id = str(uuid.uuid4())
        request = {
            "request_id": request_id,
            "url": url,
            "method": "GET",
            "headers": {},
            "body_base64": "",
            "resource_type": "document",
            "tool_call_id": meta["refund-eval/tool-call-id"],
        }
        request_path = self.spool_dir / "requests" / f"{request_id}.json"
        request_path.write_text(json.dumps(request))
        response_path = self.spool_dir / "responses" / request_path.name
        for _ in range(500):
            if response_path.exists():
                response = json.loads(response_path.read_text())
                if response.get("abort"):
                    return {"isError": True, "content": []}
                text = base64.b64decode(response["body_base64"]).decode()
                return {"content": [{"type": "text", "text": text}]}
            await asyncio.sleep(0.001)
        raise AssertionError(f"offline browser request was not mediated: {url}")

    async def aclose(self):
        return None


class OfflineRuntime:
    def __init__(self, spool_dir: Path, manifest: dict[str, Any]):
        self.spool_dir = spool_dir
        self.manifest = manifest
        self.client = None
        self.closed = False
        self.start_calls = 0

    async def start(self):
        self.start_calls += 1
        self.client = OfflineClient(self.spool_dir)
        await self.client.initialize()
        return self.client

    async def verify_isolation(self):
        return dict(self.manifest)

    async def aclose(self):
        self.closed = True


def test_verified_isolation_contract_accepts_only_trusted_modes():
    browser_runtimes.require_verified_browser_isolation(VALID_DOCKER_MANIFEST)
    browser_runtimes.require_verified_browser_isolation(VALID_SUBPROCESS_MANIFEST)


@pytest.mark.parametrize("manifest", [
    {},
    {"verified": False, "mode": "docker-network-none", "network_mode": "none"},
    {"verified": True, "mode": "subprocess-shared-network", "network_mode": "shared"},
    {"verified": True, "mode": "unknown", "network_mode": "none"},
    {"verified": True, "mode": "subprocess-netns", "network_mode": "none", "namespace_verified": False},
    {"verified": True, "mode": "docker-network-none", "network_mode": "shared"},
    {"verified": True, "mode": "subprocess-netns", "network_mode": "none"},
])
def test_verified_isolation_contract_rejects_absent_unverified_shared_or_contradictory(manifest):
    with pytest.raises(BrowserBoundaryError):
        browser_runtimes.require_verified_browser_isolation(manifest)


def _stub_successful_canary(service, monkeypatch):
    async def successful_canary():
        service.security_manifest["interception_integrity"] = {
            "verified": True,
            "request_observed": True,
            "listener_connections": 0,
        }

    monkeypatch.setattr(service, "_verify_interception_integrity", successful_canary)


def _service(tmp_path, manifest, monkeypatch):
    service = PublishedBrowserService(
        lambda _request: pytest.fail("the isolation guard must run before policy dispatch"),
        spool_dir=tmp_path / "exchange",
        runtime="subprocess",
        require_verified_browser_isolation=True,
    )
    runtime = OfflineRuntime(service.spool_dir, manifest)
    service._runtime = runtime
    _stub_successful_canary(service, monkeypatch)
    return service, runtime


@pytest.mark.parametrize("manifest", [
    {},
    {"verified": False, "mode": "docker-network-none", "network_mode": "none"},
    {"verified": True, "mode": "subprocess-shared-network", "network_mode": "shared"},
])
async def test_browser_startup_rejects_bad_isolation_before_exposing_tools(tmp_path, manifest, monkeypatch):
    service, runtime = _service(tmp_path, manifest, monkeypatch)
    with pytest.raises(BrowserBoundaryError):
        await service.list_tools()
    assert runtime.closed is True
    assert service._closed is True
    assert runtime.start_calls == 1
    assert runtime.client is not None
    assert runtime.client.list_tools_calls == 0
    assert runtime.client.call_tool_calls == 0


async def test_interception_success_does_not_override_isolation_failure(tmp_path, monkeypatch):
    service, runtime = _service(tmp_path, {
        "verified": False,
        "mode": "subprocess-shared-network",
        "network_mode": "shared",
    }, monkeypatch)
    with pytest.raises(BrowserBoundaryError):
        await service.list_tools()
    assert runtime.closed is True
    assert service._closed is True
    assert runtime.client is not None
    assert runtime.client.list_tools_calls == 0
    assert runtime.client.call_tool_calls == 0


async def test_published_tools_rejects_bad_isolation_before_profile_ready(tmp_path, monkeypatch):
    start_sample()
    monkeypatch.setattr(profile, "fixture_readiness", lambda **_kwargs: {"configured": True})
    runtime_holder = {}
    real_service = PublishedBrowserService

    class Service(real_service):
        def __init__(self, handler, **kwargs):
            super().__init__(handler, **kwargs)
            runtime_holder["runtime"] = OfflineRuntime(self.spool_dir, {})
            self._runtime = runtime_holder["runtime"]
            _stub_successful_canary(self, monkeypatch)

    monkeypatch.setattr(browser_module, "PublishedBrowserService", Service)
    scenario = load_scenario("train_refund")
    with pytest.raises(BrowserBoundaryError):
        async with profile.published_tools(
            scenario,
            data_mode="fixture",
            artifact_dir=str(tmp_path / "artifacts"),
            require_verified_browser_isolation=True,
        ):
            pytest.fail("bad isolation must not expose tools")
    assert runtime_holder["runtime"].closed is True
    assert runtime_holder["runtime"].start_calls == 1
    assert runtime_holder["runtime"].client is not None
    assert runtime_holder["runtime"].client.list_tools_calls == 0
    assert runtime_holder["runtime"].client.call_tool_calls == 0
    events = world_store().get("published_tool_audit") or []
    assert not any(event.get("event") == "profile_ready" for event in events)
    assert not any(event.get("event") == "catalog" and event.get("source") == "playwright" for event in events)


@pytest.mark.parametrize("manifest", [VALID_DOCKER_MANIFEST, VALID_SUBPROCESS_MANIFEST])
async def test_published_tools_allows_verified_mediated_fixture_browsing(tmp_path, monkeypatch, manifest):
    start_sample()
    monkeypatch.setattr(profile, "fixture_readiness", lambda **_kwargs: {"configured": True})
    async def forbidden_network(*_args, **_kwargs):
        pytest.fail("fixture browsing attempted external network access")

    monkeypatch.setattr("refund_eval.published.network.public_read", forbidden_network)
    real_service = PublishedBrowserService

    class Service(real_service):
        def __init__(self, handler, **kwargs):
            super().__init__(handler, **kwargs)
            self._runtime = OfflineRuntime(self.spool_dir, manifest)
            _stub_successful_canary(self, monkeypatch)

    monkeypatch.setattr(browser_module, "PublishedBrowserService", Service)
    scenario = load_scenario("train_refund")
    async with profile.published_tools(
        scenario,
        data_mode="fixture",
        artifact_dir=str(tmp_path / "artifacts"),
        require_verified_browser_isolation=True,
    ) as tools:
        navigate = next(tool.tool for tool in tools if tool.name == "browser_navigate")
        result = await navigate(url=scenario.org("trainline").policy_url)
    assert "refund" in json.dumps(result).casefold()
    events = world_store().get("published_tool_audit") or []
    assert any(event.get("event") == "profile_ready" for event in events)

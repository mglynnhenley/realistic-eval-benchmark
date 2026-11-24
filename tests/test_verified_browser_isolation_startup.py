"""Offline startup regressions for the opt-in verified browser boundary."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from refund_eval.published import browser as browser_module
from refund_eval.published import browser_runtimes
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.browser import BROWSER_TOOLS, PublishedBrowserService
from refund_eval.published.browser_runtimes import BrowserBoundaryError
from refund_eval.scenario.schema import load_scenario
from refund_eval.world_service.settings import WorldSettings
from refund_eval.world_service.store import ACTIVITY_KEY
from refund_eval.world_service.trial import WorldTrial

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
        self.closed = False

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
        self.closed = True


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
        if self.client is not None:
            await self.client.aclose()
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


def _install_offline_browser(monkeypatch, manifest, runtime_holder):
    class Service(PublishedBrowserService):
        def __init__(self, handler, **kwargs):
            super().__init__(handler, **kwargs)
            runtime = OfflineRuntime(self.spool_dir, manifest)
            self._runtime = runtime
            runtime_holder["runtime"] = runtime
            runtime_holder["service"] = self
            _stub_successful_canary(self, monkeypatch)

    monkeypatch.setattr(browser_module, "PublishedBrowserService", Service)


def _settings(tmp_path, *, browser_runtime, require_verified_browser_isolation=True):
    return WorldSettings(
        scenario="train_refund",
        seed=7,
        data_mode="fixture",
        research_mode="saved",
        browser_runtime=browser_runtime,
        require_verified_browser_isolation=require_verified_browser_isolation,
        output_dir=str(tmp_path / "world"),
    )


REJECTED_MANIFESTS = [
    {},
    {"verified": False, "mode": "docker-network-none", "network_mode": "none"},
    {"verified": True, "mode": "subprocess-shared-network", "network_mode": "shared"},
    {"verified": True, "mode": "unknown", "network_mode": "none"},
    {"verified": True, "mode": "subprocess-netns", "network_mode": "none", "namespace_verified": False},
    {"verified": True, "mode": "docker-network-none", "network_mode": "shared"},
    {"verified": True, "mode": "subprocess-netns", "network_mode": "none"},
]


@pytest.mark.parametrize("manifest", REJECTED_MANIFESTS)
async def test_native_trial_rejects_bad_isolation_before_profile_ready(tmp_path, monkeypatch, manifest):
    monkeypatch.setattr("refund_eval.published.profile.fixture_readiness", lambda **_kwargs: {"configured": True})
    runtime_holder = {}
    _install_offline_browser(monkeypatch, manifest, runtime_holder)
    trial = WorldTrial(_settings(tmp_path, browser_runtime="subprocess"), model_client=_NoopModels())
    with pytest.raises(BrowserBoundaryError):
        async with trial:
            pytest.fail("untrusted isolation must not yield a trial to the agent")
    assert runtime_holder["runtime"].closed is True
    assert runtime_holder["runtime"].start_calls == 1
    assert runtime_holder["runtime"].client is not None
    assert runtime_holder["runtime"].client.closed is True
    assert runtime_holder["service"]._closed is True
    assert not any(event.get("event") == "profile_ready"
                   for event in trial.trial.store().get(AUDIT_KEY, []))
    assert not trial.trial.store().get(ACTIVITY_KEY)


@pytest.mark.parametrize(("manifest", "browser_runtime"), [
    (VALID_DOCKER_MANIFEST, "docker"),
    (VALID_SUBPROCESS_MANIFEST, "subprocess"),
])
async def test_native_trial_allows_verified_mediated_fixture_browsing(
    tmp_path, monkeypatch, manifest, browser_runtime
):
    async def forbidden_network(*_args, **_kwargs):
        pytest.fail("fixture browsing attempted external network access")

    monkeypatch.setattr("refund_eval.published.network.public_read", forbidden_network)
    monkeypatch.setattr("refund_eval.published.profile.fixture_readiness", lambda **_kwargs: {"configured": True})
    runtime_holder = {}
    _install_offline_browser(monkeypatch, manifest, runtime_holder)
    scenario = load_scenario("train_refund")
    async with WorldTrial(_settings(tmp_path, browser_runtime=browser_runtime), model_client=_NoopModels()) as trial:
        result = await trial.call_tool(
            "browser_navigate", {"url": scenario.org("trainline").contact_url}, call_id="offline-browse"
        )
    assert result["isError"] is False
    assert "refund" in json.dumps(result).casefold()
    assert runtime_holder["runtime"].closed is True
    events = trial.trial.store().get(AUDIT_KEY) or []
    assert any(event.get("event") == "profile_ready" for event in events)


class _NoopModels:
    def model_name(self, role):
        return f"offline/{role}"

    async def complete(self, *args, **kwargs):
        pytest.fail("native startup test attempted a model call")

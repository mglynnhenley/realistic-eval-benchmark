"""Hybrid setup keeps mail simulated and selects real web services, offline."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fixtures.simulator import start_sample
from inspect_ai.solver import generate
from inspect_ai.util import store

from refund_eval import task as task_module
from refund_eval.agent import adapters
from refund_eval.agent.harness import refund_agent
from refund_eval.published import profile
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.fixtures import FixtureGmailRemote
from refund_eval.published.mail import GmailService
from refund_eval.published.search import ExaSearchService
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.task import impossible_refund
from refund_eval.world.state import load_world


def fixture_inbox(path):
    scenario = load_scenario("train_refund")
    path.write_text(json.dumps({"emails": [{
        "id": "1111111111111111", "thread_id": "1111111111111111",
        "sender": "Colleague <colleague@example.test>", "to": ["{{user_email}}"],
        "subject": "Hybrid fixture", "body": "Hi {{user_first_name}}, this mailbox is local.",
        "sent_at": (scenario.start_time - timedelta(hours=1)).isoformat(),
    }]}))
    return path


@pytest.fixture
def offline_profile(monkeypatch):
    start_sample()
    captured = {"services": {}, "browser": [], "boundaries": []}

    def forbid(*args, **kwargs):
        pytest.fail("Hybrid setup attempted a live call or loaded Gmail configuration")

    monkeypatch.setattr(profile, "hybrid_readiness", lambda: {"configured": True})
    monkeypatch.setattr("refund_eval.published.mcp.MCPHTTPClient.call_tool", forbid)
    monkeypatch.setattr("refund_eval.published.mcp.MCPHTTPClient.list_tools", forbid)
    monkeypatch.setattr("refund_eval.world.mail.GmailMailSource.service", forbid)
    monkeypatch.setattr("refund_eval.published.fixture_web.fixture_web_config", forbid)
    monkeypatch.setattr("refund_eval.published.fixture_web.FixtureSearchService", forbid)
    monkeypatch.setenv("EXA_API_KEY", "unused-test-key")
    monkeypatch.delenv("REFUND_EVAL_EXA_API_KEY", raising=False)

    def boundary(ctx, **kwargs):
        captured["boundaries"].append(kwargs)
        return SimpleNamespace(ctx=ctx, config=kwargs)

    class Browser:
        def __init__(self, boundary, **kwargs):
            self.closed = False
            self.security_manifest = {"public_read_policy": boundary.config["public_read_policy"]}
            captured["browser"].append(kwargs)

        async def aclose(self):
            self.closed = True

    async def inspect_tools(service, *, source, live_web=False):
        captured["services"][source] = service
        # Inspect the actual fixture mailbox without any external service calls.
        if source == "gmail":
            assert isinstance(service, GmailService)
            assert isinstance(service.remote, FixtureGmailRemote)
            await service.list_tools()
        elif source == "exa":
            assert isinstance(service, ExaSearchService)
        return []

    monkeypatch.setattr("refund_eval.published.network.BrowserBoundary", boundary)
    monkeypatch.setattr("refund_eval.published.browser.PublishedBrowserService", Browser)
    monkeypatch.setattr(profile, "inspect_tools", inspect_tools)
    return captured


async def test_hybrid_uses_fixture_mail_keyless_exa_and_public_browser(offline_profile, tmp_path, monkeypatch):
    scenario = load_scenario("train_refund")
    inbox = fixture_inbox(tmp_path / "inbox.json")
    dispatched = []

    async def simulate(inbound):
        dispatched.append(inbound)
        return SimResult(immediate_text="Message sent.")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", simulate)
    async with profile.published_tools(scenario, data_mode="hybrid", fixture_path=str(inbox)):
        services = offline_profile["services"]
        mail = services["gmail"]
        read = await mail.call_tool("get_thread", {"threadId": "1111111111111111"})
        assert scenario.user.name.split()[0] in read["structuredContent"]["messages"][0]["plaintextBody"]
        sent = await mail.call_tool("send_message", {
            "to": ["sales.support@info.thetrainline.com"], "subject": "Refund", "body": "Please review my refund.",
        })
        assert not sent.get("isError")
        assert len(dispatched) == len(load_world().interceptions) == 1
        # An existing Exa key must not opt this comparison into extra billed usage.
        assert "x-api-key" not in services["exa"]._client._client.headers
        assert offline_profile["boundaries"] == [{"public_read_policy": "public_web", "public_read_urls": []}]
        assert offline_profile["browser"][0]["image"] == profile.DEFAULT_BROWSER_IMAGE
        metadata = store().get(profile.PROFILE_KEY)
        assert metadata["data_mode"] == "hybrid"
        assert metadata["mail_data_mode"] == "fixture"
        assert metadata["web_data_mode"] == "live"
        assert metadata["web_time_basis"] == "wall_clock"
        assert metadata["search_auth"] == "keyless"
        assert metadata["public_read_policy"] == "public_web"
        assert "simulated_form_urls" not in metadata
        assert metadata["fixture_provenance"]["inbox"] == {
            "name": inbox.name, "sha256": hashlib.sha256(inbox.read_bytes()).hexdigest(),
        }
        assert "web_dataset" not in metadata["fixture_provenance"]
        ready = next(event for event in store().get(AUDIT_KEY) if event["event"] == "profile_ready")
        assert ready["public_read_policy"] == "public_web"
        assert ready["web_time_basis"] == "wall_clock"
    assert mail.remote.closed
    assert services["exa"]._client._client.is_closed
    assert services["playwright"].closed


async def test_hybrid_search_uses_only_the_opt_in_exa_key(offline_profile, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_EXA_API_KEY", "opt-in-test-key")
    async with profile.published_tools(load_scenario("train_refund"), data_mode="hybrid"):
        assert offline_profile["services"]["exa"]._client._client.headers["x-api-key"] == "opt-in-test-key"
        metadata = store().get(profile.PROFILE_KEY)
        assert metadata["search_auth"] == "api_key"
        assert "opt-in-test-key" not in json.dumps(metadata)


async def test_hybrid_records_default_inbox_provenance(offline_profile, monkeypatch):
    async with profile.published_tools(load_scenario("train_refund"), data_mode="hybrid"):
        source = offline_profile["services"]["gmail"].remote.source
        recorded = store().get(profile.PROFILE_KEY)["fixture_provenance"]["inbox"]
        assert recorded == {"name": source.path.name,
                            "sha256": hashlib.sha256(source.path.read_bytes()).hexdigest()}


async def test_hybrid_search_setup_failure_closes_mail_without_fixture_fallback(offline_profile, monkeypatch):
    original = profile.inspect_tools

    async def inspect_tools(service, *, source, live_web=False):
        await original(service, source=source)
        if source == "exa":
            raise RuntimeError("Exa rate limit")
        return []

    monkeypatch.setattr(profile, "inspect_tools", inspect_tools)
    with pytest.raises(RuntimeError, match="Exa rate limit"):
        async with profile.published_tools(load_scenario("train_refund"), data_mode="hybrid"):
            pytest.fail("Unavailable Exa was replaced with a fixture")
    assert offline_profile["services"]["gmail"].remote.closed
    assert offline_profile["services"]["exa"]._client._client.is_closed
    assert offline_profile["browser"] == []


def test_hybrid_readiness_is_local_and_independent_of_gmail(monkeypatch):
    monkeypatch.delenv("REFUND_EVAL_EXA_API_KEY", raising=False)
    profile._fixture_readiness.cache_clear()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(profile.shutil, "which", lambda name: "/usr/local/bin/docker")
    monkeypatch.setattr(profile.subprocess, "run", run)
    try:
        status = profile.hybrid_readiness()
        assert status["configured"]
        assert "keyless" in status["reason"] and "rate limits" in status["reason"]
        assert calls == [["docker", "image", "inspect", profile.DEFAULT_BROWSER_IMAGE]]
        assert "Simulated inbox and websites" in profile.fixture_readiness()["reason"]
    finally:
        profile._fixture_readiness.cache_clear()


def test_hybrid_readiness_reports_missing_local_runtime(monkeypatch):
    profile._fixture_readiness.cache_clear()
    monkeypatch.setattr(profile.shutil, "which", lambda name: None)
    try:
        assert not profile.hybrid_readiness()["configured"]
    finally:
        profile._fixture_readiness.cache_clear()


def test_hybrid_task_uses_fixture_context_and_records_modes(tmp_path, monkeypatch):
    inbox = fixture_inbox(tmp_path / "inbox.json")
    captured = {}

    def agent(**kwargs):
        captured.update(kwargs)
        return generate()

    monkeypatch.setattr(task_module, "refund_agent", agent)
    monkeypatch.setattr(task_module, "register_default_costs", lambda *a, **kw: None)
    monkeypatch.setattr(task_module, "default_model_roles", lambda *a: {})
    monkeypatch.setattr(task_module.adapters, "load_scorers", lambda *a, **kw: [])
    monkeypatch.setattr(profile, "hybrid_readiness", lambda: {"configured": True})
    task = impossible_refund(trials=1, tool_profile="published", published_data="hybrid",
                             fixture_path=str(inbox))
    assert captured["published_data"] == "hybrid"
    assert captured["fixture_path"] == str(inbox)
    for metadata in (task.metadata, task.dataset[0].metadata):
        assert metadata["mail_source"] == "gmail-fixture"
        assert metadata["mail_data_mode"] == "fixture"
        assert metadata["web_data_mode"] == "live"
        assert metadata["web_time_basis"] == "wall_clock"
    # Exercise the actual harness constructor's independent validation too.
    refund_agent(tool_profile="published", published_data="hybrid", fixture_path=str(inbox))


async def test_adapter_preserves_explicit_hybrid_mode(tmp_path, monkeypatch):
    inbox = fixture_inbox(tmp_path / "inbox.json")
    captured = []

    @asynccontextmanager
    async def scope(scenario, **kwargs):
        captured.append(kwargs)
        yield []

    monkeypatch.setattr(profile, "published_tools", scope)
    async with adapters.tool_scope(load_scenario("train_refund"), "gmail-fixture", profile="published",
                                   published_data="hybrid", fixture_path=str(inbox)):
        pass
    assert captured == [{"data_mode": "hybrid", "fixture_path": str(inbox)}]


def test_default_task_runs_published_hybrid(monkeypatch):
    captured = {}

    def agent(**kwargs):
        captured.update(kwargs)
        return generate()

    monkeypatch.setattr(task_module, "refund_agent", agent)
    monkeypatch.setattr(task_module, "register_default_costs", lambda *a, **kw: None)
    monkeypatch.setattr(task_module, "default_model_roles", lambda *a: {})
    monkeypatch.setattr(task_module.adapters, "load_scorers", lambda *a, **kw: [])
    monkeypatch.setattr(profile, "hybrid_readiness", lambda **kw: {"configured": True})
    monkeypatch.setattr(profile, "fixture_readiness", lambda **kw: pytest.fail("Default consulted fixture readiness"))
    task = impossible_refund()
    for metadata in (task.metadata, task.dataset[0].metadata):
        assert metadata["tool_profile"] == "published"
        assert metadata["published_data"] == "hybrid"
        assert metadata["mail_source"] == "gmail-fixture"
        assert metadata["mail_data_mode"] == "fixture"
        assert metadata["web_data_mode"] == "live"
    assert captured["tool_profile"] == "published"
    assert captured["published_data"] == "hybrid"
    assert "fixture_path" not in captured

"""Fixture selection and a complete offline published-tool Inspect trial."""

import asyncio
import json
import os
import subprocess
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model

from refund_eval.agent import adapters
from refund_eval.agent.harness import refund_agent
from refund_eval.grading.published import published_containment_check
from refund_eval.grading.scorers import resource_counts
from refund_eval.published import profile
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.fixture_web import FIXTURE_WEB_DATASET, TRAINLINE_CONTACT_URL
from refund_eval.published.fixtures import FIXTURE_MAIL_PROVENANCE
from refund_eval.published.mail import _visible_id
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH
from refund_eval.world.state import World


@pytest.fixture(autouse=True)
def clear_readiness_cache():
    profile._fixture_readiness.cache_clear()
    yield
    profile._fixture_readiness.cache_clear()


def test_fixture_readiness_does_not_require_live_configuration(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(profile.shutil, "which", lambda name: "/usr/local/bin/docker")
    monkeypatch.setattr(profile.subprocess, "run", run)
    monkeypatch.setattr(profile.time, "monotonic", lambda: 10)
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    result = profile.fixture_readiness()
    assert result["configured"] is True
    assert "no Gmail connection" in result["reason"]
    assert calls == [(["docker", "image", "inspect", profile.DEFAULT_BROWSER_IMAGE],
                      {"capture_output": True, "timeout": 5, "check": False})]
    # Repeated polling is cached, but callers cannot change the cached verdict.
    result["configured"] = False
    assert profile.fixture_readiness()["configured"] is True
    assert len(calls) == 1


def test_fixture_readiness_explains_missing_docker(monkeypatch):
    monkeypatch.setattr(profile.shutil, "which", lambda name: None)
    monkeypatch.setattr(profile.subprocess, "run", lambda *a, **kw: pytest.fail("Docker is absent"))
    result = profile.fixture_readiness()
    assert result["configured"] is False
    assert "Install and start Docker" in result["reason"]


@pytest.mark.parametrize("failure", [OSError("offline"), subprocess.TimeoutExpired("docker", 5)])
def test_fixture_readiness_explains_unavailable_daemon(monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(profile.shutil, "which", lambda name: "/usr/local/bin/docker")
    monkeypatch.setattr(profile.subprocess, "run", run)
    result = profile.fixture_readiness()
    assert result["configured"] is False
    assert "Docker is not responding" in result["reason"]


def test_fixture_readiness_explains_missing_browser_image(monkeypatch):
    monkeypatch.setattr(profile.shutil, "which", lambda name: "/usr/local/bin/docker")
    monkeypatch.setattr(profile.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    result = profile.fixture_readiness()
    assert result["configured"] is False
    assert "docker build -t " + profile.DEFAULT_BROWSER_IMAGE in result["reason"]


async def test_adapter_preserves_explicit_fixture_selection_and_lifetime(monkeypatch):
    events = []
    sentinel = object()
    scenario = load_scenario("train_refund")

    @asynccontextmanager
    async def scope(received_scenario, **kwargs):
        events.append((received_scenario, kwargs))
        try:
            yield [sentinel]
        finally:
            events.append("closed")

    monkeypatch.setattr(profile, "published_tools", scope)
    with pytest.raises(RuntimeError, match="consumer stopped"):
        async with adapters.tool_scope(scenario, "fixture", profile="published",
                                       published_data="fixture") as tools:
            assert tools == [sentinel]
            raise RuntimeError("consumer stopped")
    assert events == [(scenario, {"data_mode": "fixture"}), "closed"]


async def test_fixture_profile_never_falls_back_when_browser_is_unavailable(monkeypatch):
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {
        "configured": False, "reason": "The local browser needs to be built.",
    })
    with pytest.raises(ValueError, match="local browser needs"):
        async with profile.published_tools(load_scenario("train_refund"), data_mode="fixture"):
            pytest.fail("Unconfigured profile entered")


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1",
                   reason="Requires the locally built pinned browser container")
def test_inspect_fixture_profile_runs_mail_search_and_browser_without_live_services(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Fixture profile contacted or configured a live service")

    monkeypatch.setattr("refund_eval.published.mcp.MCPHTTPClient.__init__", forbidden)
    monkeypatch.setattr("refund_eval.published.search.ExaSearchService.__init__", forbidden)
    monkeypatch.setattr("refund_eval.world.mail.GmailMailSource.service", forbidden)
    monkeypatch.setattr("refund_eval.published.network.aiohttp.ClientSession", forbidden)
    dispatched = []

    async def dispatch(inbound):
        dispatched.append(inbound)
        if inbound.channel == "form":
            # A realistic simulator reply can exceed Playwright MCP's default
            # five-second action timeout. The click must await its response.
            await asyncio.sleep(6)
        return SimResult(immediate_text="Your enquiry has been received; we will reply by email.")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    scenario = load_scenario("train_refund")
    calls = [
        ("search_threads", {"query": scenario.journey.booking_ref}),
        ("get_message", {"messageId": _visible_id("seed-trainline-booking")}),
        ("send_message", {"to": ["customer.resolutions@avantiwestcoast.co.uk"],
                          "subject": "Fixture integration enquiry", "body": "Please review my cancelled journey."}),
        ("search_threads", {"query": 'in:sent subject:"Fixture integration enquiry"'}),
        ("web_search_exa", {"query": "Trainline cancelled train refund policy", "objective": "Find contact and deadline"}),
        ("browser_navigate", {"url": TRAINLINE_CONTACT_URL}),
        ("browser_type", {"target": "#email", "text": scenario.user.email_addresses[0]}),
        ("browser_type", {"target": "#booking_ref", "text": scenario.journey.booking_ref}),
        ("browser_type", {"target": "#subject", "text": "Cancelled train refund"}),
        ("browser_type", {"target": "#message", "text": "My train was cancelled and I did not travel. Please refund my ticket."}),
        ("browser_click", {"target": "button[type=submit]"}),
        ("browser_snapshot", {}),
        ("send_message", {"to": [scenario.user.email_addresses[0]],
                          "subject": "Unavailable user", "body": "Please call the bank."}),
    ]
    outputs = [ModelOutput.for_tool_call("mockllm/model", name, args) for name, args in calls]
    outputs.append(ModelOutput.from_content("mockllm/model", "I contacted the retailer and operator and will await their responses."))
    [log] = eval(
        Task(dataset=[Sample(input=scenario.task_text)],
             solver=refund_agent(scenario_id=scenario.id, max_turns=len(outputs), tool_profile="published", published_data="fixture",
                                 fixture_path=str(DEFAULT_FIXTURE_PATH))),
        model=get_model("mockllm/model", custom_outputs=outputs),
        log_dir=str(tmp_path), display="none",
    )
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert not sample.error, sample.error
    tool_results = [message for message in sample.messages if message.role == "tool"]
    assert len(tool_results) == len(calls)
    assert not any(message.error for message in tool_results[:-1])
    assert tool_results[-1].error and "No message was sent" in tool_results[-1].error.message
    assert scenario.journey.booking_ref in json.dumps(tool_results[1].model_dump())
    assert "Fixture integration enquiry" in json.dumps(tool_results[3].model_dump())
    assert "Your enquiry has been received" in json.dumps(tool_results[-2].model_dump())
    recorded = sample.store[profile.PROFILE_KEY]
    assert recorded["data_mode"] == "fixture"
    assert recorded["public_read_urls"] == []
    assert recorded["browser_network"] == "none"
    assert recorded["fixture_provenance"]["mail"] == FIXTURE_MAIL_PROVENANCE
    assert recorded["fixture_provenance"]["web_dataset"] == FIXTURE_WEB_DATASET
    events = sample.store[AUDIT_KEY]
    assert {event["source"] for event in events if event["event"] == "catalog"} == {"gmail", "exa", "playwright"}
    assert next(event for event in events if event["event"] == "profile_ready")["data_mode"] == "fixture"
    assert not any(event.get("action") == "public_read" for event in events)
    assert all(event["backend"] == "fixture" for event in events if event["event"] == "search_boundary")
    assert len(dispatched) == 2
    assert {inbound.org for inbound in dispatched} == {"trainline", "avanti"}
    world = World.model_validate(sample.store["world"])
    assert len(world.interceptions) == 3
    assert world.interceptions[-1].payload["delivery_status"] == "delivered_to_user"
    counts, _ = resource_counts(sample.messages, world)
    assert counts["blocked_user_contact_attempts"] == 0
    assert counts["delivered_messages_to_user"] == 1
    containment = published_containment_check(sample.messages, world, recorded, events)
    assert containment["contained"], containment["evidence_errors"]

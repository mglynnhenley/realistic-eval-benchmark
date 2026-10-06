"""Real browser hybrid integration, with all upstream network/model calls mocked."""

import base64
import json
import os

import httpx
import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model

from refund_eval.agent.harness import refund_agent
from refund_eval.experiments.variants import PRESSURE_THREAD_ID
from refund_eval.grading.published import published_containment_check
from refund_eval.published import profile
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.search import (
    EXA_SEARCH_ENDPOINT,
    PINNED_EXA_SEARCH_CATALOG,
    ExaSearchService,
)
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH
from refund_eval.world.state import World


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1",
                   reason="Requires the locally built pinned browser container")
def test_hybrid_native_tools_read_public_pages_and_simulate_forms(tmp_path, monkeypatch):
    upstream_calls, browser_reads, dispatched = [], [], []
    public_url = "https://www.nationalrail.co.uk/refunds/"

    def forbidden(*args, **kwargs):
        pytest.fail("Hybrid integration attempted an unmocked external service")

    def mock_exa(request):
        assert str(request.url) == EXA_SEARCH_ENDPOINT
        assert "x-api-key" not in request.headers
        message = json.loads(request.content)
        upstream_calls.append(message)
        if message["method"] == "notifications/initialized":
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "offline-exa-test", "version": "3.2.1"}}
        elif message["method"] == "tools/list":
            result = PINNED_EXA_SEARCH_CATALOG
        elif message["method"] == "tools/call":
            result = {"content": [{"type": "text", "text": f"Refund conditions: {public_url}"}]}
        else:
            raise AssertionError(message["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def exa_service(**kwargs):
        return ExaSearchService(**kwargs, transport=httpx.MockTransport(mock_exa))

    async def public_read(request):
        browser_reads.append(request)
        assert request["method"] in {"GET", "HEAD"}
        assert request["url"] == public_url
        page = ('<html><head><title>Public refund guidance</title></head><body>'
                '<h1>Public refund guidance</h1><p>Contact the retailer for a refund.</p>'
                '<form method="post" action="https://www.nationalrail.co.uk/enquiry">'
                '<input name="message" value="Please review my journey">'
                '<button type="submit">Send enquiry</button></form></body></html>')
        return {"status": 200, "headers": {"content-type": "text/html"},
                "body_base64": base64.b64encode(page.encode()).decode()}

    async def dispatch(inbound):
        dispatched.append(inbound)
        return SimResult(immediate_text="Message sent.")

    monkeypatch.setattr(profile, "load_config", forbidden)
    monkeypatch.setattr("refund_eval.world.mail.GmailMailSource.service", forbidden)
    monkeypatch.setattr("refund_eval.published.fixture_web.FixtureSearchService", forbidden)
    monkeypatch.setattr("refund_eval.published.fixture_web.fixture_web_config", forbidden)
    monkeypatch.setattr("refund_eval.published.search.ExaSearchService", exa_service)
    monkeypatch.setattr("refund_eval.published.network.public_read", public_read)
    monkeypatch.setattr("refund_eval.published.network.aiohttp.ClientSession", forbidden)
    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    monkeypatch.setenv("EXA_API_KEY", "must-not-be-used")
    monkeypatch.delenv("GMAIL_MCP_ACCESS_TOKEN", raising=False)
    scenario = load_scenario("train_refund")
    calls = [
        ("search_threads", {"query": scenario.journey.booking_ref}),
        ("web_search_exa", {"query": "cancelled train refund policy", "objective": "Find official guidance"}),
        ("browser_navigate", {"url": public_url}),
        ("browser_click", {"target": "button[type=submit]"}),
        ("browser_snapshot", {}),
        ("send_message", {"to": ["sales.support@info.thetrainline.com"],
                          "subject": "Hybrid enquiry", "body": "Please review my cancelled journey."}),
        ("search_threads", {"query": 'in:sent subject:"Hybrid enquiry"'}),
    ]
    outputs = [ModelOutput.for_tool_call("mockllm/model", name, args) for name, args in calls]
    outputs.append(ModelOutput.from_content("mockllm/model", "I emailed the retailer and will await their reply."))
    [log] = eval(
        Task(dataset=[Sample(input=scenario.task_text)],
             solver=refund_agent(max_turns=len(outputs), tool_profile="published", published_data="hybrid",
                                 fixture_path=str(DEFAULT_FIXTURE_PATH), initial_mail_thread_ids=[PRESSURE_THREAD_ID])),
        model=get_model("mockllm/model", custom_outputs=outputs),
        log_dir=str(tmp_path), display="none",
    )
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert not sample.error, sample.error
    results = [message for message in sample.messages if message.role == "tool"
               and not (message.metadata or {}).get("startup_mail_context")]
    assert len(results) == len(calls)
    assert not any(message.error for message in results)
    assert "Public refund guidance" in results[2].text
    assert "Hybrid enquiry" in results[-1].text
    assert len([message for message in upstream_calls if message["method"] == "tools/call"]) == 1
    assert browser_reads and all(request["method"] == "GET" for request in browser_reads)
    # The form on the public page was submitted to the simulator, never to the site.
    assert [item.channel for item in dispatched] == ["form", "email"]
    assert dispatched[0].target == "https://www.nationalrail.co.uk/enquiry"
    assert dispatched[0].fields == {"message": "Please review my journey"}
    recorded, events = sample.store[profile.PROFILE_KEY], sample.store[AUDIT_KEY]
    assert recorded["data_mode"] == "hybrid" and recorded["public_read_policy"] == "public_web"
    assert recorded["mail_data_mode"] == "fixture" and recorded["web_data_mode"] == "live"
    assert any(event.get("action") == "public_read" for event in events)
    assert not any(event.get("backend") == "fixture" for event in events if event["event"] == "search_boundary")
    world = World.model_validate(sample.store["world"])
    assert any(item.channel == "form" and item.payload.get("method") == "POST" for item in world.interceptions)
    containment = published_containment_check(sample.messages, world, recorded, events)
    assert containment["contained"], containment["evidence_errors"]

"""Real Exa transport and Inspect adapters with in-process HTTP failures only."""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent import adapters
from refund_eval.agent.harness import refund_agent
from refund_eval.published.adapter import AUDIT_KEY, audit, inspect_tools
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG, ExaSearchService
from refund_eval.world.interception import current_tool_call_id


def transport_failure(kind, request):
    if isinstance(kind, int):
        return httpx.Response(kind, headers={"retry-after": "60"}, text="Provider unavailable")
    raise kind("Provider unavailable", request=request)


@pytest.mark.parametrize("failure", [429, 503, httpx.ReadTimeout, httpx.ConnectError])
def test_runtime_exa_failure_is_a_tool_error_and_agent_can_continue(tmp_path, monkeypatch, failure):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        method = payload["method"]
        if method == "tools/call":
            return transport_failure(failure, request)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "offline-exa", "version": "1"}}
        elif method == "notifications/initialized":
            return httpx.Response(202)
        else:
            assert method == "tools/list"
            result = PINNED_EXA_SEARCH_CATALOG
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def search_audit(event):
        audit({"event": "search_boundary", "call_id": current_tool_call_id(), **event})

    services = []

    @asynccontextmanager
    async def scope(*args, **kwargs):
        async with ExaSearchService(policy=lambda request: True, audit=search_audit,
                                    transport=httpx.MockTransport(respond)) as service:
            services.append(service)
            yield await inspect_tools(service, source="exa")

    monkeypatch.setattr(adapters, "tool_scope", scope)
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "KILL"))
    outputs = [
        ModelOutput.for_tool_call("mockllm/model", "web_search_exa", {
            "query": "cancelled train policy", "objective": "Find the official refund conditions",
        }),
        ModelOutput.from_content("mockllm/model", "Search is unavailable; I can continue using the information already available."),
    ]
    [log] = eval(Task(dataset=[Sample(input="Get a refund")], solver=refund_agent(
        max_turns=2, tool_profile="published", published_data="hybrid",
    )), model=get_model("mockllm/model", custom_outputs=outputs, memoize=False),
        log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert not sample.error
    assert sample.store["world"]["turn"] == 2
    assert sample.store["world"]["stop_reason"] == "max_turns"
    assert len([request for request in requests if request["method"] == "tools/call"]) == 1
    error_result = next(message for message in sample.messages if message.role == "tool")
    assert error_result.error and "Exa search unavailable" in error_result.error.message
    events = sample.store[AUDIT_KEY]
    terminal = next(event for event in events if event["event"] == "failure")
    expected_type = "HTTPStatusError" if isinstance(failure, int) else failure.__name__
    assert terminal["error_type"] == expected_type
    assert terminal["failure_kind"] == "transport" and terminal["recoverable"] is True
    assert terminal["status_code"] == (failure if isinstance(failure, int) else None)
    assert terminal["call_id"] == error_result.tool_call_id
    boundary = [event for event in events if event["event"] == "search_boundary"
                and event["call_id"] == terminal["call_id"] and event["phase"] != "catalog"]
    assert [event["phase"] for event in boundary] == ["attempt", "allowed", "error"]
    assert boundary[-1]["error"] == expected_type
    assert not any(event["event"] == "result" for event in events)
    assert services[0]._client._client.is_closed


@pytest.mark.parametrize("failure", [429, httpx.ReadTimeout])
async def test_startup_exa_transport_errors_remain_fatal(failure):
    init_subtask_store(Store())
    requests = []

    def respond(request):
        requests.append(request)
        return transport_failure(failure, request)

    async with ExaSearchService(policy=lambda request: True, audit=lambda event: None,
                                transport=httpx.MockTransport(respond)) as service:
        with pytest.raises(httpx.HTTPStatusError if isinstance(failure, int) else failure):
            await inspect_tools(service, source="exa")
    assert len(requests) == 1
    assert not any(event.get("recoverable") for event in store().get(AUDIT_KEY, []))

import copy

import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.tool import ToolError
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import refund_agent
from refund_eval.published.adapter import (
    AUDIT_KEY,
    inspect_parameters,
    inspect_tools,
    model_content,
)
from refund_eval.published.mcp import MCPRPCError
from refund_eval.published.search import (
    PINNED_EXA_SEARCH_CATALOG,
    ExaContractError,
    ExaPolicyDenied,
    ExaPolicyFailure,
)


class Service:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Published on 2026-10-01: https://example.org/policy",
                }
            ],
            "structuredContent": {"published": "2026-10-01"},
            "_meta": {"native": True},
        }

    async def aclose(self):
        pass


def test_native_schema_kept_and_unsupported_constraints_rejected():
    schema = PINNED_EXA_SEARCH_CATALOG["tools"][0]["inputSchema"]
    assert inspect_parameters(schema).model_dump(exclude_none=True) == schema
    unsupported = {
        "type": "object",
        "properties": {"x": {"type": "array", "minItems": 1}},
    }
    with pytest.raises(ValueError, match="lossy"):
        inspect_parameters(unsupported)
    with pytest.raises(ValueError, match="references"):
        inspect_parameters(
            {"type": "object", "$ref": "https://external.example/schema"}
        )
    parts = model_content(
        {
            "content": [{"type": "text", "text": "Created case"}],
            "structuredContent": {"case_id": "R-123"},
        }
    )
    assert parts[0].text == "Created case"
    assert "R-123" in parts[1].text
    assert (
        len(
            model_content(
                {
                    "content": [{"type": "text", "text": '{"id": 1}'}],
                    "structuredContent": {"id": 1},
                }
            )
        )
        == 1
    )


async def test_raw_result_retained_and_native_error():
    init_subtask_store(Store())
    service = Service()
    [tool] = await inspect_tools(service, source="exa")
    descriptor = PINNED_EXA_SEARCH_CATALOG["tools"][0]
    assert tool.name == descriptor["name"]
    assert tool.description == descriptor["description"]
    result = await tool.tool(query="rail refund", objective="Find rules")
    assert result[0].text.startswith("Published on 2026-10-01")
    assert store().get(AUDIT_KEY)[-1]["result"]["_meta"] == {"native": True}
    with pytest.raises(ToolError, match="required"):
        await tool.tool(query="rail refund")
    assert len(service.calls) == 1
    with pytest.raises(ToolError, match="Native failure"):
        model_content(
            {"content": [{"type": "text", "text": "Native failure"}], "isError": True}
        )


def test_published_service_executes_through_existing_inspect_harness(
    tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from refund_eval.agent import adapters

    service = Service()
    closed = []

    @asynccontextmanager
    async def scope(*args, **kwargs):
        try:
            yield await inspect_tools(service, source="exa")
        finally:
            closed.append(True)

    monkeypatch.setattr(adapters, "tool_scope", scope)
    outputs = [
        ModelOutput.for_tool_call(
            "mockllm/model",
            "web_search_exa",
            {"query": "rail refund", "objective": "Find rules"},
        ),
        ModelOutput.from_content("mockllm/model", "I found the policy."),
    ]
    [log] = eval(
        Task(
            dataset=[Sample(input="Get a refund")],
            solver=refund_agent(max_turns=2, tool_profile="published"),
        ),
        model=get_model("mockllm/model", custom_outputs=outputs),
        log_dir=str(tmp_path),
        display="none",
    )
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert not sample.error, sample.error
    assert service.calls == [
        ("web_search_exa", {"query": "rail refund", "objective": "Find rules"})
    ]
    events = sample.store[AUDIT_KEY]
    call = next(event for event in events if event["event"] == "call")
    assert call["call_id"]
    assert (
        next(event for event in events if event["event"] == "result")["call_id"]
        == call["call_id"]
    )
    assert sample.store["world"]["stop_reason"] == "max_turns"
    assert closed == [True]


@pytest.mark.parametrize("error", [
    ExaPolicyDenied("Search request was denied"),
    MCPRPCError({"code": -32602, "message": "Native request error", "data": {"field": "query"}},
        response={"jsonrpc": "2.0", "id": 7,
                  "error": {"code": -32602, "message": "Native request error", "data": {"field": "query"}}}),
])
def test_recoverable_search_error_keeps_agent_running(tmp_path, monkeypatch, error):
    from contextlib import asynccontextmanager

    from refund_eval.agent import adapters

    class RecoveringService(Service):
        attempts = 0

        async def call_tool(self, name, arguments):
            self.attempts += 1
            if self.attempts == 1:
                raise error
            return await super().call_tool(name, arguments)

    service = RecoveringService()

    @asynccontextmanager
    async def scope(*args, **kwargs):
        yield await inspect_tools(service, source="exa")

    monkeypatch.setattr(adapters, "tool_scope", scope)
    outputs = [
        ModelOutput.for_tool_call("mockllm/model", "web_search_exa", {"query": "first", "objective": "Find rules"}),
        ModelOutput.for_tool_call("mockllm/model", "web_search_exa", {"query": "second", "objective": "Find rules"}),
        ModelOutput.from_content("mockllm/model", "The second search worked."),
    ]
    [log] = eval(Task(dataset=[Sample(input="Get a refund")], solver=refund_agent(max_turns=3, tool_profile="published")),
        model=get_model("mockllm/model", custom_outputs=outputs), log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert not sample.error, sample.error
    assert service.attempts == 2
    assert service.calls == [("web_search_exa", {"query": "second", "objective": "Find rules"})]
    events = sample.store[AUDIT_KEY]
    terminal = next(e for e in events if e["event"] in {"rejected", "rpc_error"})
    assert terminal["call_id"] == next(e["call_id"] for e in events if e["event"] == "call")
    if isinstance(error, MCPRPCError):
        assert terminal["error"] == error.error
        assert terminal["rpc_response"] == error.response
    else:
        assert terminal["reason"] == "policy_denied"
    assert any(message.role == "tool" and message.error for message in sample.messages)


@pytest.mark.parametrize("error", [ExaPolicyFailure("Policy broke"), ExaContractError("Catalog drift"), OSError("Audit unavailable")])
async def test_infrastructure_failures_remain_fatal(error):
    init_subtask_store(Store())

    class FailingService(Service):
        async def call_tool(self, name, arguments):
            raise error

    [tool] = await inspect_tools(FailingService(), source="exa")
    with pytest.raises(type(error)):
        await tool.tool(query="rail refund", objective="Find rules")
    assert store().get(AUDIT_KEY)[-1]["event"] == "failure"
    assert store().get(AUDIT_KEY)[-1]["error_type"] == type(error).__name__

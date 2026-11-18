"""World-side model calls: a fake client drives the simulator and grader, and both backends keep the contract."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from inspect_ai.model import (
    ChatMessageAssistant,
    ContentText,
    ModelOutput,
    ResponseSchema,
    get_model,
)
from inspect_ai.model._model import prepare_tools
from inspect_ai.tool import ToolDef
from inspect_ai.util import LimitExceededError, json_schema
from openai import AsyncOpenAI

from refund_eval import model_pricing
from refund_eval.companies import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanyProfile,
)
from refund_eval.companies.llm import LLMCompanySimulator
from refund_eval.companies.models import CompanyModelRefusal
from refund_eval.models import client as models
from refund_eval.models.client import (
    Completion,
    Message,
    ModelLimitExceeded,
    OutputSchema,
    ToolCall,
    Usage,
)
from refund_eval.models.inspect_client import InspectLimitExceeded, InspectModelClient
from refund_eval.models.openrouter import OpenRouterClient, response_json_schema
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim import research
from refund_eval.sim.consistency import ResponseConsistency, response_problem
from refund_eval.sim.counterparts import CounterpartProfile
from refund_eval.sim.rules import MAX_OUTPUT_TOKENS as RULES_MAX_TOKENS
from refund_eval.sim.rules import CompanyRules, generate_rules
from refund_eval.world.state import World

ACTION = {
    "decision": "evidence_required", "decision_basis": "We need a medical note.", "summary": "Asked for evidence.",
    "reply": True, "reply_after_hours": 24, "subject": "Your renewal", "body": "Please send a medical note.",
    "page_body": "", "payments": [], "case_status": "open",
}
LATE = load_scenario("train_refund_late")


class FakeClient:
    """A ModelClient with scripted replies; records every call."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def model_name(self, role):
        return f"fake/{role}"

    async def complete(self, role, messages, **kwargs):
        self.calls.append({"role": role, "messages": list(messages), **kwargs})
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, Completion):
            return reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        return Completion(text=text, message=Message("assistant", text), stop_reason="stop")


@pytest.fixture
def fake():
    def install(*replies):
        client = FakeClient(*replies)
        models.install(client)
        return client
    yield install
    models.install(None)


@pytest.fixture
def context():
    return CompanyContext(
        org=CompanyProfile(key="agency", name="Example Agency", kind="licensing", terms="Renew within 30 days."),
        subject="a licence renewal", customer_name="Example Customer", customer_email="user@example.test",
        now=datetime(2026, 10, 12, 9, tzinfo=UTC), records="YOUR RECORDS: licence expired on 1 October.",
    )


# ---------------------------------------------------------------- world side on a fake client


async def test_company_simulator_runs_on_the_installed_client(fake, context):
    client = fake(ACTION)
    action = await LLMCompanySimulator().respond(CompanyEvent(incoming="I was ill."), context)
    assert action == CompanyAction.model_validate(ACTION)
    [call] = client.calls
    assert call["role"] == "simulator" and call["temperature"] == 1.0
    assert call["schema"] == OutputSchema("open_world_action", CompanyAction,
                                          "What this organisation or person does in response.")


async def test_content_filter_stop_is_a_refusal_even_with_valid_json(fake, context):
    fake(Completion(text=json.dumps(ACTION), message=Message("assistant", ""), stop_reason="content_filter"))
    with pytest.raises(CompanyModelRefusal):
        await LLMCompanySimulator().respond(CompanyEvent(incoming="Hello"), context)


async def test_failed_company_reply_is_not_parsed_even_with_valid_json(fake, context):
    output = Completion(text=json.dumps(ACTION), message=Message("assistant", ""), failed=True)
    fake(output)
    with pytest.raises(ValueError, match="reply failed"):
        await LLMCompanySimulator().respond(CompanyEvent(incoming="Hello"), context)
    with pytest.raises(ValueError, match="reply failed"):
        output.json()


async def test_failed_structured_reply_is_retried_without_parsing(fake):
    from refund_eval.model_io import generate_structured

    failed = Completion(text=json.dumps(ACTION), message=Message("assistant", ""), failed=True)
    client = fake(failed, ACTION)
    parsed, _raw, error = await generate_structured(
        client, shared="context", task="decide", schema=CompanyAction, name="action", system="system")
    assert parsed == CompanyAction.model_validate(ACTION)
    assert error is None and len(client.calls) == 2


async def test_failed_research_reply_does_not_execute_tool_calls(fake):
    failed = Completion(text="", message=Message("assistant", "", tool_calls=[ToolCall("c1", "nope", {})]), failed=True)
    fake(failed)
    record = await research.research_live(LATE.org("trainline"), scenario=LATE)
    assert record["tool_calls"] == []
    assert record["dossier"] is None
    assert record["errors"] == ["model: Research model reply failed"]


async def test_consistency_check_uses_the_grader_role_and_never_swallows_limits(fake):
    world = World(start_time="2026-01-05T09:00:00Z", now="2026-01-05T09:00:00Z")
    action, org = CompanyAction.model_validate(ACTION), LATE.org("trainline")
    client = fake({"consistent": False, "reason": "claims a payment"})
    assert await response_problem(action, world, org, mode="email") == "claims a payment"
    assert client.calls[0]["role"] == "grader"
    assert client.calls[0]["schema"] == OutputSchema("response_consistency", ResponseConsistency)
    fake(ModelLimitExceeded("cost"))
    with pytest.raises(ModelLimitExceeded):
        await response_problem(action, world, org, mode="email")
    fake(RuntimeError("down"))
    assert (await response_problem(action, world, org, mode="email")).startswith(
        "response consistency check unavailable")


async def test_rules_are_generated_on_the_client_and_name_its_model(fake):
    rules = {"terms": "Refunds within 28 days.", "grounds": []}
    client = fake(rules, {"items": []})
    record = await generate_rules(LATE, LATE.org("trainline"), None)
    assert record["rules"] == CompanyRules.model_validate(rules).model_dump()
    assert record["model"] == "fake/simulator" and client.calls[0]["max_tokens"] == RULES_MAX_TOKENS
    assert [call["schema"].name for call in client.calls] == [
        "company_rules", "company_rules_intent_check",
    ]
    assert all(call["temperature"] == 0 for call in client.calls)


@pytest.mark.parametrize("operation", ["dossier", "person", "rules"])
async def test_company_preparation_does_not_swallow_model_limits(fake, operation):
    client = fake(ModelLimitExceeded("allowance exhausted"))
    org = LATE.org("trainline")
    with pytest.raises(ModelLimitExceeded, match="allowance exhausted"):
        if operation == "dossier":
            await research.research_live(org, scenario=LATE)
        elif operation == "person":
            await research.profile_person(org, ["Previous correspondence."])
        else:
            await generate_rules(LATE, org, None)
    assert len(client.calls) == 1


async def test_research_tool_loop_sends_back_the_assistant_message_and_tool_results(fake):
    asked = Completion(text="", message=Message("assistant", "", tool_calls=[ToolCall("c1", "nope", {})],
                                                native="backend form"), stop_reason="tool_calls")
    dossier = {"summary": "A retailer.", "facts": []}
    client = fake(asked, dossier)
    record = await research.research_live(LATE.org("trainline"), subject="a refund", scenario=LATE)
    assert record["dossier"]["summary"] == "A retailer."
    assert record["tool_calls"] == [{"tool": "nope", "arguments": {}, "error": "unknown tool nope"}]
    second = client.calls[1]["messages"]
    assert second[2] is asked.message
    assert (second[3].role, second[3].tool_call_id, second[3].function) == ("tool", "c1", "nope")
    assert [t.name for t in client.calls[0]["tools"]] == ["web_search_exa", "read_page"]
    assert client.calls[0]["schema"] is None


# ---------------------------------------------------------------- Inspect backend parity


class Recorder:
    def __init__(self, output):
        self.output, self.calls = output, []

    def __call__(self, input, tools, tool_choice, config):
        self.calls.append((input, tools, config))
        return self.output


async def test_inspect_backend_sends_what_direct_calls_sent():
    recorder = Recorder(ModelOutput.from_content(model="mockllm", content=json.dumps(ACTION)))
    client = InspectModelClient(get_model("mockllm/model", custom_outputs=recorder, memoize=False))
    out = await client.complete(
        "simulator", [Message("system", "sys"), Message("user", ["shared", "task"])], temperature=1.0,
        max_tokens=50, stop=["\n\n"], schema=OutputSchema("open_world_action", CompanyAction, "What it does."))
    [(input, tools, config)] = recorder.calls
    assert config.temperature == 1.0 and config.max_tokens == 50 and config.stop_seqs == ["\n\n"]
    assert config.response_schema == ResponseSchema(name="open_world_action", json_schema=json_schema(CompanyAction),
                                                    description="What it does.")
    assert input[1].content == [ContentText(text="shared"), ContentText(text="task")]
    assert tools == []
    assert out.text == json.dumps(ACTION) and out.stop_reason == "stop" and out.json() == ACTION
    assert out.message.native is not None and out.usage is not None


async def test_inspect_backend_tools_match_the_tool_defs_research_used():
    async def search(query: str, objective: str) -> str:
        return await research._search(query, objective, scenario=LATE)

    async def read_page(url: str) -> str:
        return await research._read_page(url, scenario=LATE)

    old = [ToolDef(search, name="web_search_exa", description=research.SEARCH_DESCRIPTION, parameters={
        "query": "Natural-language description of the ideal page.",
        "objective": "Which pages should rank first and what facts to pull from them."}),
        ToolDef(read_page, name="read_page", description=research.READ_DESCRIPTION,
                parameters={"url": "Absolute http(s) URL of a public page."})]
    recorder = Recorder(ModelOutput.for_tool_call(model="mockllm", tool_name="read_page",
                                                  tool_arguments={"url": "https://example.test"}))
    client = InspectModelClient(get_model("mockllm/model", custom_outputs=recorder, memoize=False))
    out = await client.complete("simulator", [Message("user", "go")], tools=research._tools())
    assert recorder.calls[0][1] == (await prepare_tools(old)).base_tools
    [call] = out.tool_calls
    assert (call.function, call.arguments) == ("read_page", {"url": "https://example.test"})
    await client.complete("simulator", [Message("user", "go"), out.message,
                                        Message("tool", "page", tool_call_id=call.id, function=call.function)])
    sent = recorder.calls[1][0]
    assert sent[1] is out.message.native and isinstance(sent[1], ChatMessageAssistant)
    assert (sent[2].tool_call_id, sent[2].function, sent[2].text) == (call.id, "read_page", "page")


async def test_inspect_backend_reports_content_filter_from_any_choice():
    output = ModelOutput.from_content(model="mockllm", content="no", stop_reason="content_filter")
    client = InspectModelClient(get_model("mockllm/model", custom_outputs=Recorder(output), memoize=False))
    assert (await client.complete("simulator", [Message("user", "x")])).stop_reason == "content_filter"


async def test_inspect_limits_stay_inspect_limits_and_are_recognisable_without_inspect():
    source = object()

    class Limited:
        async def generate(self, *args, **kwargs):
            raise LimitExceededError("cost", value=2, limit=1, source=source)

    with pytest.raises(ModelLimitExceeded) as caught:
        await InspectModelClient(Limited()).complete("grader", [Message("user", "x")])
    assert isinstance(caught.value, LimitExceededError) and isinstance(caught.value, InspectLimitExceeded)
    assert (caught.value.type, caught.value.value, caught.value.limit, caught.value.source) == ("cost", 2, 1, source)


def test_default_client_records_inspect_and_inspect_models_are_wrapped():
    from refund_eval.models.replay import RecordingModelClient

    models.install(None)
    default = models.client()
    assert isinstance(default, RecordingModelClient) and isinstance(default.inner, InspectModelClient)
    model = get_model("mockllm/model", memoize=False)
    assert models.as_client(model).model is model
    fake_client = FakeClient()
    assert models.as_client(fake_client) is fake_client
    models.install(None)


# ---------------------------------------------------------------- OpenRouter backend


def openrouter(handler, **kwargs):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncOpenAI(api_key="not-a-key", base_url="https://openrouter.test/api/v1", http_client=http,
                      max_retries=0)
    roles = {"simulator": "openrouter/anthropic/claude-opus-5.5", "grader": "openrouter/anthropic/claude-haiku-4.5"}
    return OpenRouterClient(roles, client=sdk, **kwargs)


def reply(message, finish="stop", usage=None):
    return {"id": "gen-1", "object": "chat.completion", "created": 0, "model": "anthropic/claude-opus-5.5",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": usage or {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}


@pytest.mark.parametrize("schema", [CompanyAction, research.Dossier, CompanyRules, CounterpartProfile,
                                    research.PersonProfile, ResponseConsistency])
def test_openrouter_response_schema_matches_inspect(schema):
    assert response_json_schema(schema) == json_schema(schema).model_dump(exclude_none=True)


async def test_openrouter_request_maps_role_schema_and_settings():
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply({"role": "assistant", "content": json.dumps(ACTION)}))

    client = openrouter(handler)
    out = await client.complete("simulator", [Message("system", "sys"), Message("user", ["a", "b"])],
                                temperature=1.0, max_tokens=50, stop=["\n\n"],
                                schema=OutputSchema("open_world_action", CompanyAction, "What it does."))
    [body] = requests
    assert body["model"] == "anthropic/claude-opus-5.5"
    assert body["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": [
        {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}]
    assert (body["temperature"], body["max_tokens"], body["stop"]) == (1.0, 50, ["\n\n"])
    assert body["response_format"] == {"type": "json_schema", "json_schema": {
        "name": "open_world_action", "schema": response_json_schema(CompanyAction), "description": "What it does."}}
    assert body["usage"] == {"include": True} and "tools" not in body
    assert out.json() == ACTION and out.stop_reason == "stop" and out.model == client.roles["simulator"]
    assert client.model_name("grader") == "openrouter/anthropic/claude-haiku-4.5"


async def test_openrouter_tool_calls_round_trip_the_assistant_message():
    requests = []
    asked = {"role": "assistant", "content": None, "reasoning_details": [{"type": "reasoning.text", "text": "hm"}],
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "read_page", "arguments": '{"url": "https://example.test"}'}}]}

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply(asked if len(requests) == 1 else {"role": "assistant", "content": "ok"},
                                              "tool_calls" if len(requests) == 1 else "stop"))

    client = openrouter(handler)
    tools = research._tools()
    out = await client.complete("simulator", [Message("user", "go")], tools=tools)
    assert out.stop_reason == "tool_calls" and out.text == ""
    assert out.tool_calls == [ToolCall("c1", "read_page", {"url": "https://example.test"})]
    assert requests[0]["tools"][1] == {"type": "function", "function": {
        "name": "read_page", "description": research.READ_DESCRIPTION, "parameters": tools[1].parameters}}
    await client.complete("simulator", [Message("user", "go"), out.message,
                                        Message("tool", "page", tool_call_id="c1", function="read_page")])
    sent = requests[1]["messages"]
    assert sent[1]["reasoning_details"] == asked["reasoning_details"] and sent[1]["tool_calls"] == asked["tool_calls"]
    assert sent[2] == {"role": "tool", "content": "page", "tool_call_id": "c1"}


async def test_openrouter_usage_and_cost_are_counted_by_model_and_role():
    usages = [
        {"prompt_tokens": 1_000, "completion_tokens": 100, "total_tokens": 1_100, "cost": 0.5,
         "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100}},
        {"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000},
    ]

    def handler(request):
        return httpx.Response(200, json=reply({"role": "assistant", "content": "ok"}, usage=usages.pop(0)))

    prices = {"openrouter/anthropic/claude-haiku-4.5": {"input": 1.0, "output": 5.0, "input_cache_read": 0.1,
                                                         "input_cache_write": 1.25}}
    client = openrouter(handler, prices=prices)
    reported = await client.complete("grader", [Message("user", "x")])
    assert reported.usage == Usage(input_tokens=300, output_tokens=100, total_tokens=1_100,
                                   input_tokens_cache_read=600, input_tokens_cache_write=100, cost=0.5)
    computed = await client.complete("grader", [Message("user", "x")])
    assert computed.usage.cost == pytest.approx(1.0)
    assert client.usage_by_role["grader"].cost == pytest.approx(1.5)
    assert client.usage_by_model["openrouter/anthropic/claude-haiku-4.5"].total_tokens == 1_001_100


async def test_openrouter_unknown_prices_leave_cost_unknown_and_content_filter_is_reported():
    client = openrouter(lambda request: httpx.Response(200, json=reply(
        {"role": "assistant", "content": "{}"}, "content_filter")))
    out = await client.complete("simulator", [Message("user", "x")])
    assert out.stop_reason == "content_filter" and out.usage.cost is None


async def test_openrouter_credit_limit_is_a_model_limit():
    client = openrouter(lambda request: httpx.Response(402, json={"error": {"message": "Insufficient credits"}}))
    with pytest.raises(ModelLimitExceeded):
        await client.complete("simulator", [Message("user", "x")])


async def test_budgeted_openrouter_loads_fallback_prices_and_disables_sdk_retries(monkeypatch):
    from refund_eval.world_service.settings import WorldSettings

    monkeypatch.setattr(model_pricing, "openrouter_prices", lambda models: {})
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json=reply({"role": "assistant", "content": "{}"}, usage={"prompt_tokens": 1,
                         "completion_tokens": 1, "total_tokens": 2, "cost": 0.1}))))
    sdk = AsyncOpenAI(api_key="not-a-key", http_client=http, max_retries=2)
    client = OpenRouterClient.from_settings(WorldSettings(world_cost_limit=1), client=sdk)
    assert client.client.max_retries == 0
    assert (await client.complete("simulator", [])).usage.cost == 0.1


@pytest.mark.parametrize("budgeted", [False, True])
@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), TypeError("invalid model catalog"),
                                   ValueError("invalid prompt price"), RuntimeError("unexpected")])
def test_openrouter_catalog_failure_only_falls_back_when_budgeted(monkeypatch, budgeted, error):
    from refund_eval.world_service.settings import WorldSettings

    def unavailable(models):
        raise error

    monkeypatch.setattr(model_pricing, "openrouter_prices", unavailable)
    settings = WorldSettings(world_cost_limit=1 if budgeted else None)
    sdk = AsyncOpenAI(api_key="not-a-key")
    if not budgeted or isinstance(error, RuntimeError):
        with pytest.raises(type(error), match=str(error)):
            OpenRouterClient.from_settings(settings, client=sdk)
    else:
        assert OpenRouterClient.from_settings(settings, client=sdk).prices == {}


@pytest.mark.parametrize("mode", ["lookup", "native_lookup", "lookup_zero", "unfinished", "cancelled",
                                 "lookup_zero_tokens", "estimate", "tokens", "exceeds", "unknown", "multiple",
                                 "actual", "reported_zero_lookup", "reported_zero_estimate", "reported_zero_unknown",
                                 "null", "zero", "partial", "short_total", "negative_cache"])
async def test_world_charge_settlement(monkeypatch, mode):
    from refund_eval.models.budget import WORLD_BUDGET_KEY, BudgetedWorldModels
    from refund_eval.models.replay import RECORDINGS_KEY, RecordingModelClient
    from refund_eval.world import store
    from refund_eval.world_service.store import MemoryTrial

    previous = store._context
    store.install(MemoryTrial("train_refund_late"))
    requests = []
    unsettled = {"lookup_zero", "unfinished", "cancelled", "lookup_zero_tokens", "unknown", "null", "zero",
                 "partial", "short_total", "negative_cache", "reported_zero_unknown"}

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr("refund_eval.models.openrouter.anyio.sleep", no_sleep)

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer not-a-key"
        if request.method == "GET":
            assert request.url.path == "/api/v1/generation" and request.url.params["id"] == "gen-1"
            if mode in {"lookup", "native_lookup", "lookup_zero", "unfinished", "cancelled", "lookup_zero_tokens",
                        "reported_zero_lookup"}:
                data = {"total_cost": 0.25, "finish_reason": "stop", "tokens_prompt": 200, "tokens_completion": 100}
                if mode == "lookup" and sum(r.method == "GET" for r in requests) == 1:
                    return httpx.Response(404, json={"error": {"message": "not ready"}})
                if mode == "native_lookup":
                    data = {"total_cost": 0.25, "native_finish_reason": "stop", "native_tokens_prompt": 200,
                            "native_tokens_completion": 100}
                if mode == "lookup_zero":
                    data["total_cost"] = 0
                if mode == "unfinished":
                    data["finish_reason"] = None
                if mode == "cancelled":
                    data["cancelled"] = True
                if mode == "lookup_zero_tokens":
                    data["tokens_completion"] = 0
                return httpx.Response(200, json={"data": data})
            return httpx.Response(404, json={"error": {"message": "not ready"}})
        payload = reply({"role": "assistant", "content": "ok"})
        usage = {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}
        if mode == "tokens":
            usage["cost"] = -1
        if mode in {"exceeds", "multiple"}:
            usage = {"prompt_tokens": 100_000, "completion_tokens": 100, "total_tokens": 100_100}
        if mode == "actual":
            usage["cost"] = 0.25
        if mode.startswith("reported_zero"):
            usage["cost"] = 0
        if mode == "zero":
            usage["completion_tokens"] = 0
        if mode == "partial":
            del usage["prompt_tokens"]
        if mode == "short_total":
            usage["total_tokens"] = 250
        if mode == "negative_cache":
            usage["prompt_tokens_details"] = {"cached_tokens": -1}
        payload["usage"] = None if mode in {"lookup", "native_lookup", "null"} else usage
        return httpx.Response(200, json=payload)

    name = "openrouter/anthropic/claude-opus-5.5"
    client = openrouter(handler, require_reported_cost=True,
                        prices={} if mode in {"unknown", "reported_zero_unknown"} else {name: {"input": 1, "output": 5}})
    budget = BudgetedWorldModels(RecordingModelClient(client), 0.001 if mode == "exceeds" else 1, lambda: None)
    try:
        if mode in unsettled | {"exceeds"}:
            with pytest.raises(ModelLimitExceeded):
                await budget.complete("simulator", [Message("user", "hello")], max_tokens=100_000)
            state = store.world_store().get(WORLD_BUDGET_KEY)
            assert state["status"] == ("exhausted" if mode == "exceeds" else "unknown")
            assert (state["spent_usd"] > 0) == (mode == "exceeds")
            saved = store.world_store().get(RECORDINGS_KEY)[0]["response"]
            if mode != "exceeds":
                assert saved["usage"] is None or saved["usage"]["cost"] is None
                assert saved["cost_method"] is None
        else:
            result = await budget.complete("simulator", [Message("user", "hello")], max_tokens=100_000)
            method = ("generation_lookup" if mode in {"lookup", "native_lookup", "reported_zero_lookup"} else
                      "actual" if mode == "actual" else "estimate")
            assert result.cost_method == method
            assert budget.spent == Decimal(str(result.usage.cost))
            if mode in {"tokens", "reported_zero_estimate"}:
                assert result.usage.cost == 0.0015
            saved = store.world_store().get(RECORDINGS_KEY)[0]["response"]
            assert saved["cost_method"] == method
            if mode == "multiple":
                with pytest.raises(ModelLimitExceeded, match="allowance reached"):
                    await budget.complete("simulator", [], max_tokens=100_000)
                assert budget.spent >= 1
        if mode in unsettled | {"multiple", "exceeds"}:
            count = len(requests)
            with pytest.raises(ModelLimitExceeded, match="no further requests"):
                await budget.complete("simulator", [])
            assert len(requests) == count
        if mode == "actual":
            assert len(requests) == 1
        elif mode in unsettled | {"estimate", "tokens", "exceeds", "reported_zero_estimate"}:
            assert sum(r.method == "GET" for r in requests) == 3
    finally:
        store.install(previous)
        await client.client.close()


@pytest.mark.parametrize("kind", ["body_error", "choice_error", "empty_choices", "error_finish", "unknown_empty",
                                  "length", "three", "allowance", "no_price", "no_request_price", "reported", "lookup",
                                  "explicit", "success", "request_price", "reasoning_price", "unknown_price",
                                  "normal_empty", "private_error"])
async def test_failed_world_requests_are_charged_and_retried(monkeypatch, kind):
    import math

    from refund_eval.models.budget import WORLD_MAX_OUTPUT_TOKENS, BudgetedWorldModels
    from refund_eval.models.replay import (
        RECORDINGS_KEY,
        RecordingModelClient,
        ReplayMismatch,
        ReplayModelClient,
    )
    from refund_eval.world import store
    from refund_eval.world_service.store import MemoryTrial

    previous = store._context
    store.install(MemoryTrial("train_refund_late"))
    requests = []
    error = {"code": 502, "message": "provider failed"}
    if kind == "private_error":
        from urllib.parse import quote_plus

        error["message"] = quote_plus(quote_plus(LATE.user.email_addresses[0]))

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr("refund_eval.models.openrouter.anyio.sleep", no_sleep)

    def handler(request):
        if request.method == "GET":
            if kind == "lookup":
                return httpx.Response(200, json={"data": {"total_cost": 0.01, "finish_reason": "error",
                                                          "tokens_prompt": 10, "tokens_completion": 1}})
            return httpx.Response(404, json={"error": {"message": "not found"}})
        body = json.loads(request.content)
        requests.append(body)
        if kind == "success" or (len(requests) > 1 and kind != "three"):
            return httpx.Response(200, json=reply({"role": "assistant", "content": "ok"}, usage={
                "prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11, "cost": 0.001}))
        payload = reply({"role": "assistant", "content": ""}, finish="unknown")
        payload["usage"] = None
        if kind in {"body_error", "three", "allowance", "no_price", "no_request_price", "reported", "lookup",
                    "request_price", "reasoning_price", "private_error"}:
            payload = {"id": "gen-1", "error": error}
        if kind == "choice_error":
            payload["choices"][0]["error"] = error
        if kind == "empty_choices":
            payload["choices"] = []
        if kind == "error_finish":
            payload["choices"][0]["finish_reason"] = "error"
        if kind == "normal_empty":
            payload["choices"][0]["finish_reason"] = "stop"
        if kind in {"length", "explicit"}:
            payload["choices"][0]["finish_reason"] = "length"
            payload["choices"][0]["message"]["content"] = "truncated"
            payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15, "cost": 0.01}
        if kind in {"reported", "allowance"}:
            payload["usage"] = {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11, "cost": 0.01}
        return httpx.Response(200, json=payload)

    name = "openrouter/anthropic/claude-opus-5.5"
    prices = {"input": 1, "output": 5, "input_cache_write": 6}
    if kind != "no_request_price":
        prices["request"] = 0.005 if kind == "request_price" else 0
    if kind == "reasoning_price":
        prices["internal_reasoning"] = 8
    if kind == "unknown_price":
        prices["unpriced"] = 0
    client = openrouter(handler, require_reported_cost=True,
                        prices={} if kind == "no_price" else {name: prices})
    budget = BudgetedWorldModels(RecordingModelClient(client), 0.02 if kind == "allowance" else 1, lambda: None)
    messages = [Message("user", "hello £")]
    settings = {"schema": OutputSchema("action", CompanyAction), "tools": research._tools()}
    if kind == "explicit":
        settings["max_tokens"] = 123
    try:
        if kind in {"three", "allowance", "no_price", "unknown_price"}:
            with pytest.raises(ModelLimitExceeded):
                await budget.complete("simulator", messages, **settings)
        else:
            result = await budget.complete("simulator", messages, **settings)
            assert not result.failed
            assert result.text == "ok"
        recordings = store.world_store().get(RECORDINGS_KEY)
        expected_count = 3 if kind == "three" else 1 if kind in {
            "allowance", "no_price", "unknown_price", "success"} else 2
        assert len(requests) == len(recordings) == expected_count
        assert all(body["max_tokens"] == (123 if kind == "explicit" else WORLD_MAX_OUTPUT_TOKENS) for body in requests)
        first = recordings[0]["response"]
        assert first["failed"] == (kind != "success")
        assert first["generation_id"] == "gen-1"
        assert first["http_status"] == 200
        prompt = {"messages": requests[0]["messages"], "tools": [tool["function"] for tool in requests[0]["tools"]],
                  "schema": requests[0]["response_format"]["json_schema"]["schema"], "stop": []}
        rate = 8 if kind == "reasoning_price" else 6
        fixed = 0.005 if kind == "request_price" else 0
        bound = math.ceil(((len(json.dumps(prompt).encode()) + 16_384 + requests[0]["max_tokens"]) * rate / 1e6 + fixed) * 1e9) / 1e9
        if kind in {"no_price", "unknown_price"}:
            assert first["usage"] is None and budget.spent == 0
        elif kind in {"reported", "allowance", "lookup", "explicit", "length"}:
            assert first["usage"]["cost"] == 0.01
            assert first["cost_method"] == ("generation_lookup" if kind == "lookup" else "actual")
        elif kind != "success":
            assert first["cost_method"] == "worst_case"
            assert first["usage"]["cost"] == bound
        if kind not in {"no_price", "unknown_price"}:
            assert budget.spent == sum((Decimal(str(r["response"]["usage"]["cost"])) for r in recordings), Decimal(0))
        if kind == "allowance":
            assert budget.spent < budget.limit and Decimal(str(bound)) > budget.limit - budget.spent
        if kind == "length":
            assert first["raw_finish_reason"] == "length"
        if kind == "private_error":
            from refund_eval.published import REDACTED_SCENARIO_VALUE

            assert first["error"] == REDACTED_SCENARIO_VALUE
        if kind in {"body_error", "choice_error"}:
            assert first["error"] == error
            replayed = await ReplayModelClient(recordings).complete(
                "simulator", messages, **settings, max_tokens=WORLD_MAX_OUTPUT_TOKENS, default_output_cap=True)
            assert replayed.failed and replayed.error == error and replayed.generation_id == "gen-1"
            assert replayed.http_status == 200
            assert replayed.raw_finish_reason == first["raw_finish_reason"]
        if kind == "three":
            assert budget.status == "failed"
        if kind == "success":
            direct = ReplayModelClient(recordings)
            replayed = await direct.complete("simulator", messages, **settings)
            assert replayed.text == result.text
            assert replayed.usage == result.usage
            assert direct.position == len(recordings)
            with pytest.raises(ReplayMismatch):
                await ReplayModelClient(recordings).complete("simulator", messages, **settings, max_tokens=123)
        replay = ReplayModelClient(recordings)
        replay_budget = BudgetedWorldModels(replay, float(budget.limit), lambda: None)
        if kind in {"three", "allowance", "no_price", "unknown_price"}:
            with pytest.raises(ModelLimitExceeded):
                await replay_budget.complete("simulator", messages, **settings)
        else:
            replayed = await replay_budget.complete("simulator", messages, **settings)
            assert replayed.text == result.text
            assert replayed.usage == result.usage
            assert replayed.cost_method == result.cost_method
            assert not replayed.failed
        assert replay.position == len(recordings)
        assert replay_budget.spent == budget.spent
        assert replay_budget.status == budget.status
        if kind in {"success", "body_error"}:
            mismatch = BudgetedWorldModels(ReplayModelClient(recordings), float(budget.limit), lambda: None)
            with pytest.raises(ReplayMismatch, match="differs from its recording"):
                await mismatch.complete("simulator", [Message("user", "different request")], **settings)
    finally:
        store.install(previous)
        await client.client.close()


@pytest.mark.parametrize("kind", ["timeout", "http", "plain", "three", "allowance", "no_price", "402"])
async def test_world_request_exceptions_are_screened_charged_and_replayed(monkeypatch, kind):
    from urllib.parse import quote_plus

    from refund_eval.models.budget import BudgetedWorldModels
    from refund_eval.models.replay import (
        RECORDINGS_KEY,
        RecordingModelClient,
        ReplayModelClient,
    )
    from refund_eval.published import REDACTED_SCENARIO_VALUE
    from refund_eval.world import store
    from refund_eval.world_service.store import MemoryTrial

    previous = store._context
    store.install(MemoryTrial("train_refund_late"))
    private = quote_plus(quote_plus(LATE.user.email_addresses[0]))
    requests = []

    def handler(request):
        assert request.method == "POST"
        requests.append(json.loads(request.content))
        if len(requests) > 1 and kind != "three":
            return httpx.Response(200, json=reply({"role": "assistant", "content": "ok"}, usage={
                "prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11, "cost": 0.001}))
        if kind in {"http", "402"}:
            return httpx.Response(402 if kind == "402" else 500, json={"error": {"message": private}})
        if kind == "plain":
            raise RuntimeError(private)
        raise httpx.ReadTimeout(private, request=request)

    name = "openrouter/anthropic/claude-opus-5.5"
    client = openrouter(handler, require_reported_cost=True,
                        prices={} if kind == "no_price" else {name: {"input": 1, "output": 6}})
    budget = BudgetedWorldModels(RecordingModelClient(client), 0.2 if kind == "allowance" else 1, lambda: None)
    messages = [Message("user", "hello")]
    stopped = kind in {"three", "allowance", "no_price", "402"}
    try:
        if stopped:
            with pytest.raises(ModelLimitExceeded):
                await budget.complete("simulator", messages)
        else:
            assert (await budget.complete("simulator", messages)).text == "ok"
        recordings = store.world_store().get(RECORDINGS_KEY)
        assert len(requests) == len(recordings) == (3 if kind == "three" else 1 if stopped else 2)
        evidence = recordings[0]["error"]
        assert private not in json.dumps(recordings)
        if kind in {"http", "plain", "402"}:
            assert REDACTED_SCENARIO_VALUE in evidence["message"]
        assert evidence["limit"] == (kind == "402")
        if kind in {"no_price", "402"}:
            assert budget.spent == 0
        else:
            charged = Decimal(str(evidence["worst_case_cost"]))
            assert charged > 0
            assert budget.spent == charged * (3 if kind == "three" else 1) + (Decimal(0) if stopped else Decimal("0.001"))
        replay = ReplayModelClient(recordings)
        replay_budget = BudgetedWorldModels(replay, float(budget.limit), lambda: None)
        if stopped:
            with pytest.raises(ModelLimitExceeded):
                await replay_budget.complete("simulator", messages)
        else:
            assert (await replay_budget.complete("simulator", messages)).text == "ok"
        assert replay.position == len(recordings)
        assert replay_budget.spent == budget.spent
    finally:
        store.install(previous)
        await client.client.close()


def test_old_completion_recordings_load_without_failure_evidence():
    from refund_eval.models.replay import _load

    completion = _load({"text": "ok", "message": {"role": "assistant", "content": "ok"}})
    assert not completion.failed
    assert completion.http_status is None
    assert completion.error is completion.generation_id is completion.raw_finish_reason is completion.worst_case_cost is None


@pytest.mark.parametrize("unpriced", [False, True])
async def test_unbudgeted_reported_zero_cost_is_unchanged(unpriced):
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(200, json=reply({"role": "assistant", "content": "ok"}, usage={
            "prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300, "cost": 0}))

    prices = {"input": 1, "output": 5, "unpriced": 0} if unpriced else {}
    client = openrouter(handler, prices={"openrouter/anthropic/claude-opus-5.5": prices})
    try:
        result = await client.complete("simulator", [])
        assert result.cost_method == "actual" and result.usage.cost == 0
    finally:
        await client.client.close()


def test_openrouter_roles_come_from_settings_and_must_be_openrouter_models(monkeypatch):
    from refund_eval.config import RunConfig

    monkeypatch.setattr(model_pricing.httpx, "get", lambda *a, **k: pytest.fail("prices were given"))
    settings = RunConfig(cost_limit=1, simulator_model="openrouter/a/sim", grader_model="openrouter/a/grader")
    client = OpenRouterClient.from_settings(settings, prices={}, client=AsyncOpenAI(api_key="not-a-key"))
    assert client.roles == {"simulator": "openrouter/a/sim", "grader": "openrouter/a/grader"}
    with pytest.raises(ValueError, match="openrouter/"):
        OpenRouterClient({"simulator": "anthropic/claude"}, client=AsyncOpenAI(api_key="not-a-key"))
    with pytest.raises(ValueError, match="judge"):
        client.model_name("judge")


def test_openrouter_prices_come_from_the_public_catalog(monkeypatch):
    entry = {"id": "a/sim", "pricing": {"prompt": "0.000001", "completion": "0.000005"}}

    def get(url, timeout):
        assert url == model_pricing.CATALOG_URL
        return httpx.Response(200, json={"data": [entry]}, request=httpx.Request("GET", url))

    monkeypatch.setattr(model_pricing.httpx, "get", get)
    assert model_pricing.openrouter_prices(["openrouter/a/sim", "openrouter/a/missing", "mockllm/model"]) == {
        "openrouter/a/sim": {"input": 1.0, "output": 5.0, "input_cache_read": 1.0, "input_cache_write": 1.0, "request": 0}}

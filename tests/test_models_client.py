"""World-side model calls: a fake client drives the simulator and grader, and both backends keep the contract."""

import json
from datetime import UTC, datetime

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
    client = fake(rules)
    record = await generate_rules(LATE, LATE.org("trainline"), None)
    assert record["rules"] == CompanyRules.model_validate(rules).model_dump()
    assert record["model"] == "fake/simulator" and client.calls[0]["max_tokens"] == RULES_MAX_TOKENS


@pytest.mark.parametrize("operation", ["dossier", "person", "rules"])
async def test_company_preparation_does_not_swallow_model_limits(fake, operation):
    client = fake(ModelLimitExceeded("allowance exhausted"))
    org = LATE.org("trainline")
    with pytest.raises(ModelLimitExceeded, match="allowance exhausted"):
        if operation == "dossier":
            await research.research_live(org)
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
    record = await research.research_live(LATE.org("trainline"), subject="a refund")
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
    old = [ToolDef(research._search, name="web_search_exa", description=research.SEARCH_DESCRIPTION, parameters={
        "query": "Natural-language description of the ideal page.",
        "objective": "Which pages should rank first and what facts to pull from them."}),
        ToolDef(research._read_page, name="read_page", description=research.READ_DESCRIPTION,
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


async def test_budgeted_openrouter_uses_only_reported_charges_and_disables_sdk_retries(monkeypatch):
    from refund_eval.world_service.settings import WorldSettings

    monkeypatch.setattr(model_pricing.httpx, "get", lambda *a, **k: pytest.fail("not a provider-reported charge"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json=reply({"role": "assistant", "content": "{}"}))))
    sdk = AsyncOpenAI(api_key="not-a-key", http_client=http, max_retries=2)
    client = OpenRouterClient.from_settings(WorldSettings(world_cost_limit=1), client=sdk)
    assert client.client.max_retries == 0
    assert (await client.complete("simulator", [])).usage.cost is None


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
        "openrouter/a/sim": {"input": 1.0, "output": 5.0, "input_cache_read": 1.0, "input_cache_write": 1.0}}

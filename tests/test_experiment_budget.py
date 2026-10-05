"""Budget reservations cover every role, retries, errors and actual wire options."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelCost,
    ModelInfo,
    ModelOutput,
    ModelUsage,
    get_model,
    set_model_info,
)
from inspect_ai.scorer import Score, scorer
from inspect_ai.util import LimitExceededError
from tenacity import RetryError

from refund_eval.agent import adapters
from refund_eval.agent.harness import refund_agent
from refund_eval.experiments import budget

NAME = "openrouter/anthropic/claude-haiku-4.5"
MOCK = "mockllm/budget-test"


def catalog():
    return {"data": [{"id": NAME.removeprefix("openrouter/"), "context_length": 200000,
                      "top_provider": {"max_completion_tokens": 64000},
                      "architecture": {"output_modalities": ["text"]},
                      "pricing": {"prompt": "0.000001", "completion": "0.000005",
                                  "input_cache_read": "0.0000001", "input_cache_write": "0.00000125"}}]}


def mock_configuration():
    set_model_info(MOCK, ModelInfo(model=MOCK, context_length=1000, output_tokens=100,
                                  cost=ModelCost(input=100, output=100, input_cache_read=100, input_cache_write=100)))
    return budget.configure_budget_models([MOCK])


def output(input_tokens=100, output_tokens=0, *, total_cost=None):
    result = ModelOutput.from_content(MOCK, "Done.")
    result.usage = ModelUsage(
        input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=input_tokens + output_tokens,
        total_cost=total_cost)
    return result


def fake_generate(monkeypatch, responder):
    async def generate(self, input, tools=(), tool_choice=None, config=None, cache=False, on_stream=None):
        return await responder(self, config)
    monkeypatch.setattr(Model, "generate", generate)
    return generate


def read(path):
    return json.loads(path.read_text())


def test_catalog_frozen_bounds_restore_without_network_and_tampering_fails(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), json=catalog())

    monkeypatch.setattr(budget.httpx, "get", get)
    configuration = budget.configure_budget_models([NAME, NAME])
    assert len(calls) == 1
    assert configuration["models"][NAME]["context_length"] == 200000
    assert configuration["models"][NAME]["cost_per_million"]["input_cache_write"] == 1.25
    assert budget.configure_budget_models([NAME], frozen=configuration) == configuration
    assert len(calls) == 1
    broken = copy.deepcopy(configuration)
    broken["models"][NAME]["context_length"] = 1
    with pytest.raises(budget.BudgetConfigurationError, match="checksum"):
        budget.configure_budget_models([NAME], frozen=broken)


@pytest.mark.parametrize("field", ["request", "image", "internal_reasoning"])
def test_unbounded_non_token_pricing_is_rejected(monkeypatch, field):
    data = catalog()
    data["data"][0]["pricing"][field] = "0.01"
    monkeypatch.setattr(budget.httpx, "get", lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=data))
    with pytest.raises(budget.BudgetConfigurationError, match="non-token pricing"):
        budget.configure_budget_models([NAME])


async def test_reserve_before_dispatch_settle_real_usage_and_deny_without_next_call(tmp_path, monkeypatch):
    configuration = mock_configuration()
    path = tmp_path / "budget.json"
    calls = []

    async def respond(model, config):
        calls.append(config)
        assert read(path)["events"][-1]["event"] == "reserved"
        assert read(path)["charged_upper_usd"] <= 0.12
        return output()

    original = fake_generate(monkeypatch, respond)
    model = get_model(MOCK, memoize=False)
    with budget.all_role_budget(0.12, [MOCK], path, configuration=configuration):
        await model.generate("private prompt")
        await model.generate("another private prompt")
        with pytest.raises(LimitExceededError):
            await model.generate("never dispatched")
    assert len(calls) == 2
    assert calls[0].max_tokens == 100 and calls[0].max_retries == 0
    assert calls[0].cache is False
    assert read(path)["charged_upper_usd"] == pytest.approx(0.02)
    assert read(path)["blocked"] is True
    assert "private prompt" not in path.read_text()
    assert Model.generate is original


@pytest.mark.parametrize("mode", ["error", "missing", "cost_limit"])
async def test_failed_or_unknown_usage_keeps_reservation(tmp_path, monkeypatch, mode):
    configuration = mock_configuration()
    path = tmp_path / "budget.json"

    async def respond(model, config):
        if mode == "error":
            raise RuntimeError("provider request with confidential text")
        if mode == "cost_limit":
            raise LimitExceededError("cost", limit=0.1, value=0.11)
        return ModelOutput(model=MOCK, completion="No usage supplied")

    fake_generate(monkeypatch, respond)
    model = get_model(MOCK, memoize=False)
    with budget.all_role_budget(0.2, [MOCK], path, configuration=configuration):
        if mode == "missing":
            await model.generate("private")
        else:
            with pytest.raises((RuntimeError, LimitExceededError)):
                await model.generate("private")
        with pytest.raises(LimitExceededError):
            await model.generate("not affordable")
    assert read(path)["charged_upper_usd"] == pytest.approx(0.11)
    assert "confidential" not in path.read_text()


async def test_concurrent_roles_are_serialized_and_unknown_models_are_blocked(tmp_path, monkeypatch):
    configuration = mock_configuration()
    running = peak = 0

    async def respond(model, config):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        return output()

    fake_generate(monkeypatch, respond)
    models = [get_model(MOCK, role=role, memoize=False) for role in ("target", "simulator", "grader")]
    path = tmp_path / "budget.json"
    with budget.all_role_budget(1, [MOCK], path, configuration=configuration):
        await asyncio.gather(*(model.generate("role request") for model in models))
        with pytest.raises(LimitExceededError):
            await get_model("mockllm/unapproved", memoize=False).generate("blocked")
    assert peak == 1
    assert {event["role"] for event in read(path)["events"] if event["event"] == "settled"} == {"target", "simulator", "grader"}


async def test_openrouter_serialization_price_caps_cache_and_sdk_retries_are_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.httpx, "get", lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=catalog()))
    configuration = budget.configure_budget_models([NAME])
    requests = []

    async def request(req):
        requests.append(json.loads(req.content))
        return httpx2.Response(200, json={"id": "offline", "object": "chat.completion", "created": 1,
            "model": "anthropic/claude-haiku-4.5", "choices": [{"index": 0, "message": {"role": "assistant", "content": "Done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})

    # Inspect's OpenAI-compatible SDK uses httpx2; use its matching transport.
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(request))
    model = get_model(NAME, api_key="offline-test-key", memoize=False, http_client=client, stream=False)
    old_retry, old_provider, old_args = model.api.client.max_retries, copy.deepcopy(model.api.provider), dict(model.api.model_args)
    original = Model.generate
    path = tmp_path / "budget.json"
    try:
        with budget.all_role_budget(1, [NAME], path, configuration=configuration):
            await model.generate([ChatMessageSystem(content="System cache prefix"),
                                  ChatMessageUser(content="Offline serialization check")],
                                 config=GenerateConfig(cache_prompt=True))
        assert Model.generate is original
        assert model.api.client.max_retries == old_retry and model.api.provider == old_provider
        assert model.api.model_args == old_args
        assert len(requests) == 1
        wire = requests[0]
        assert wire["max_tokens"] == 8192
        assert "n" not in wire and "best_of" not in wire
        assert wire["provider"]["allow_fallbacks"] is False
        assert wire["provider"]["require_parameters"] is True
        assert wire["provider"]["max_price"] == {"prompt": 1, "completion": 5, "request": 0, "image": 0}
        assert any(part.get("cache_control") for message in wire["messages"] for part in
                   (message["content"] if isinstance(message["content"], list) else []))
        assert read(path)["events"][0]["event"] == "settled"
    finally:
        await client.aclose()


def test_offline_inspect_generation_and_grading_share_budget_without_generate_loop(tmp_path, monkeypatch):
    configuration = mock_configuration()

    @asynccontextmanager
    async def no_tools(*args, **kwargs):
        yield []

    monkeypatch.setattr(adapters, "tool_scope", no_tools)

    @scorer(metrics=[])
    def paid_like_grader():
        async def score(state, target):
            model = get_model(MOCK, role="grader", memoize=False, custom_outputs=[output()])
            await model.generate("Grade the trial")
            return Score(value=1)
        return score

    model = get_model(MOCK, custom_outputs=[output()], memoize=False)
    path = tmp_path / "budget.json"
    with budget.all_role_budget(1, [MOCK], path, configuration=configuration):
        [log] = eval(Task(dataset=[Sample(input="Task")], solver=refund_agent(max_turns=1), scorer=paid_like_grader()),
                     model=model, log_dir=str(tmp_path / "logs"), display="none")
    assert log.status == "success", log.error
    assert len(read(path)["events"]) == 2
    assert {e["role"] for e in read(path)["events"]} == {None, "grader"}


async def test_openrouter_503_dispatches_once_and_retains_reservation(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.httpx, "get", lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=catalog()))
    configuration = budget.configure_budget_models([NAME])
    requests = []

    async def fail(req):
        requests.append(req)
        assert model.api.client.max_retries == 0
        return httpx2.Response(503, json={"error": {"message": "offline synthetic failure"}})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(fail))
    model = get_model(NAME, api_key="offline-test-key", memoize=False, http_client=client, stream=False)
    prior = model.api.client.max_retries
    path = tmp_path / "budget.json"
    try:
        with budget.all_role_budget(1, [NAME], path, configuration=configuration), pytest.raises(RetryError):
            await model.generate("Never leaves MockTransport")
        assert len(requests) == 1
        assert model.api.client.max_retries == prior
        assert read(path)["events"][0]["event"] == "failed_reserved"
        assert read(path)["charged_upper_usd"] == read(path)["events"][0]["reservation_usd"]
    finally:
        await client.aclose()


def test_existing_ledger_and_nested_context_cannot_reset_budget(tmp_path):
    configuration = mock_configuration()
    path = tmp_path / "budget.json"
    with budget.all_role_budget(1, [MOCK], path, configuration=configuration), pytest.raises(budget.BudgetConfigurationError, match="overlap"), budget.all_role_budget(1, [MOCK], tmp_path / "nested.json", configuration=configuration):
        pass
    before = path.read_bytes()
    with pytest.raises(budget.BudgetConfigurationError, match="already exists"), budget.all_role_budget(1, [MOCK], path, configuration=configuration):
        pass
    assert path.read_bytes() == before

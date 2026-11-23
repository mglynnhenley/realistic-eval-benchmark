"""Exercise the actual provider and SDK through an offline HTTP transport."""

import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import httpx2
import pytest
from inspect_ai.model import (
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    get_model,
    get_model_info,
)
from inspect_ai.tool import ToolInfo, ToolParams

from refund_eval.agent import dollar_budget
from refund_eval.config import DEFAULT_ROLE_MODEL, DEFAULT_TARGET_MODEL
from refund_eval.models.client import Message
from refund_eval.models.openrouter import OpenRouterClient
from refund_eval.models.world_spend_ledger import (
    WorldSpendLedger,
    WorldSpendLedgerError,
)
from refund_eval.task import register_default_costs


@pytest.mark.parametrize(("name", "slug", "prices"), [
    (DEFAULT_TARGET_MODEL, "anthropic/claude-opus-5", (5.0, 25.0)),
    (DEFAULT_ROLE_MODEL, "anthropic/claude-opus-5.5", (4.0, 20.0)),
])
async def test_openrouter_uses_its_own_key_and_model_route(monkeypatch, name, slug, prices):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    requests = []

    def completion(request):
        requests.append(request)
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer test-openrouter-key"
        assert json.loads(request.content)["model"] == slug
        return httpx2.Response(200, json={
            "id": "offline-completion", "object": "chat.completion", "created": 0,
            "model": slug,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Routed correctly."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    register_default_costs([name])
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(completion)) as client:
        model = get_model(name, http_client=client, memoize=False,
                          config=GenerateConfig(max_tokens=20, max_retries=0), stream=False)
        info = get_model_info(model)
        assert info is not None and info.cost is not None
        assert (info.cost.input, info.cost.output) == prices
        output = await model.generate("Check routing.")
        assert output.completion == "Routed correctly."
    assert len(requests) == 1


@pytest.mark.parametrize(("usage_is_byok", "generation_is_byok", "generation_calls"), [
    (True, False, 0),
    (None, True, 1),
])
async def test_strict_world_does_not_trust_byok_credit_charge(
    usage_is_byok, generation_is_byok, generation_calls
):
    class FakeRawMessage:
        content = "reply"
        tool_calls = None

        def model_dump(self, **_):
            return {"role": "assistant", "content": self.content}

    class FakeResponse:
        status_code = 200

        def parse(self):
            usage = SimpleNamespace(
                prompt_tokens=5,
                completion_tokens=2,
                total_tokens=7,
                cost=0.03,
                is_byok=usage_is_byok,
            )
            choice = SimpleNamespace(message=FakeRawMessage(), finish_reason="stop", error=None)
            return SimpleNamespace(choices=[choice], usage=usage, error=None, id="generation-byok")

    class FakeCompletions:
        with_raw_response = None

        async def create(self, **_):
            return FakeResponse()

    class FakeClient:
        def __init__(self):
            self.completions = FakeCompletions()
            self.completions.with_raw_response = self.completions
            self.chat = SimpleNamespace(completions=self.completions)
            self.generation_requests = 0

        def with_options(self, **_):
            return self

        async def get(self, *_args, **_kwargs):
            self.generation_requests += 1
            return {"data": {
                "total_cost": 0.03,
                "is_byok": generation_is_byok,
                "finish_reason": "stop",
                "tokens_prompt": 5,
                "tokens_completion": 2,
            }}

    tariff = {
        "input": 1,
        "output": 5,
        "input_cache_read": 1,
        "input_cache_write": 1,
        "input_cache_write_1h": 1,
        "internal_reasoning": 0,
        "request": 0,
    }
    client = FakeClient()
    models = OpenRouterClient(
        {"simulator": "openrouter/test-model"},
        client=client,
        prices={"openrouter/test-model": {"input": 1, "output": 5}},
        tariffs={"openrouter/test-model": tariff},
        require_reported_cost=True,
        pin_prices=True,
        strict_budget=True,
    )

    result = await models.complete("simulator", [Message("user", "hello")], max_tokens=8)

    assert result.cost_method is None
    assert result.usage is not None and result.usage.cost is None
    assert client.generation_requests == generation_calls


def _install_real_target_budget(monkeypatch, tmp_path, model_id):
    tariff = {
        "input": 1.0,
        "output": 5.0,
        "input_cache_read": 1.0,
        "input_cache_write": 1.0,
        "input_cache_write_1h": 1.0,
        "internal_reasoning": 0.0,
        "request": 0.0,
    }
    ledger = WorldSpendLedger(
        tmp_path / "agent-spend",
        Decimal(50),
        {"scenario": "train_refund_late", "models": {"agent": model_id}},
    )
    reserve_attempts = []
    original_reserve = ledger.reserve

    def tracked_reserve(*args, **kwargs):
        reserve_attempts.append(args[0])
        return original_reserve(*args, **kwargs)

    ledger.reserve = tracked_reserve
    monkeypatch.setattr(dollar_budget, "WorldSpendLedger", lambda *_args: ledger)
    monkeypatch.setattr(dollar_budget, "openrouter_tariffs", lambda _models: {model_id: tariff})
    monkeypatch.setenv("REFUND_EVAL_AGENT_DOLLAR_CAP", "50")
    monkeypatch.setenv("REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS", "16")
    monkeypatch.setenv("WORLD_SCENARIO", "train_refund_late")
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-agent-key")
    return ledger, reserve_attempts


def _chat_response(request, *, tool_call=False, reasoning_details=None):
    if tool_call:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call-lookup",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        }
        if reasoning_details is not None:
            message["reasoning_details"] = reasoning_details
        finish_reason = "tool_calls"
    else:
        message = {"role": "assistant", "content": "Offline bounded response."}
        finish_reason = "stop"
    return httpx2.Response(
        200,
        request=request,
        json={
            "id": f"offline-{request.headers.get('x-irid', 'request')}",
            "object": "chat.completion",
            "created": 0,
            "model": "anthropic/claude-opus-5",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 3,
                "total_tokens": 11,
                "cost": 0.0001,
                "is_byok": False,
            },
        },
    )


async def test_real_inspect_openrouter_hook_reserves_before_text_and_tool_calls(
    tmp_path, monkeypatch
):
    model_id = "openrouter/anthropic/claude-opus-5"
    ledger, _ = _install_real_target_budget(monkeypatch, tmp_path, model_id)
    requests = []
    reservations_at_send = []

    def transport(request):
        assert request.url.path == "/api/v1/chat/completions"
        snapshot = ledger.snapshot()
        reservations_at_send.append(snapshot["reserved_usd"])
        assert Decimal(snapshot["reserved_usd"]) > 0
        assert request.headers.get("x-irid")
        requests.append(request)
        return _chat_response(request, tool_call=len(requests) == 1)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as client:
        model = get_model(
            model_id,
            http_client=client,
            memoize=False,
            config=GenerateConfig(max_tokens=32, max_retries=0, cache_prompt=False),
            stream=False,
        )
        async with dollar_budget.target_dollar_budget(model):
            tool_output = await model.generate(
                "Look up the status.",
                tools=[ToolInfo(name="lookup", description="Lookup the status.", parameters=ToolParams())],
            )
            assistant_message = tool_output.choices[0].message
            call = assistant_message.tool_calls[0]
            text_output = await model.generate(
                [
                    ChatMessageUser(content="Look up the status."),
                    assistant_message,
                    ChatMessageTool(
                        content="Status: delayed.", tool_call_id=call.id, function=call.function
                    ),
                    ChatMessageUser(content="Summarize the result."),
                ],
                tools=[ToolInfo(name="lookup", description="Lookup the status.", parameters=ToolParams())],
            )

    assert len(requests) == 2
    assert all(Decimal(value) > 0 for value in reservations_at_send)
    assert "tools" in json.loads(requests[0].content)
    assert "tool_calls" in json.loads(requests[1].content)["messages"][1]
    assert tool_output.choices[0].message.tool_calls[0].id == "call-lookup"
    assert text_output.completion == "Offline bounded response."
    assert Decimal(ledger.snapshot()["spent_usd"]) == Decimal("0.0002")
    assert ledger.snapshot()["reserved_usd"] == "0"


async def test_real_inspect_openrouter_replays_bounded_reasoning_details_with_tool_call(
    tmp_path, monkeypatch
):
    model_id = "openrouter/anthropic/claude-opus-5"
    ledger, _ = _install_real_target_budget(monkeypatch, tmp_path, model_id)
    reasoning_details = [{
        "type": "reasoning.text",
        "text": "Check the message history before using the lookup tool.",
        "signature": "offline-anthropic-signature",
        "format": "anthropic-claude-v1",
    }]
    requests = []
    reservations_at_send = []
    bounded_requests = []
    original_charge_bound = dollar_budget.charge_bound

    def capture_charge_bound(request, prices):
        saved = deepcopy(request)
        bound = original_charge_bound(request, prices)
        bounded_requests.append((saved, bound))
        return bound

    monkeypatch.setattr(dollar_budget, "charge_bound", capture_charge_bound)

    def transport(request):
        reservations_at_send.append(Decimal(ledger.snapshot()["reserved_usd"]))
        requests.append(request)
        return _chat_response(
            request,
            tool_call=len(requests) == 1,
            reasoning_details=reasoning_details if len(requests) == 1 else None,
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as client:
        model = get_model(
            model_id,
            http_client=client,
            memoize=False,
            config=GenerateConfig(max_tokens=32, max_retries=0, cache_prompt=False),
            stream=False,
        )
        async with dollar_budget.target_dollar_budget(model):
            tool_output = await model.generate(
                "Look up the status.",
                tools=[ToolInfo(name="lookup", description="Lookup the status.", parameters=ToolParams())],
            )
            assistant_message = tool_output.choices[0].message
            call = assistant_message.tool_calls[0]
            await model.generate(
                [
                    ChatMessageUser(content="Look up the status."),
                    assistant_message,
                    ChatMessageTool(
                        content="Status: delayed.", tool_call_id=call.id, function=call.function
                    ),
                    ChatMessageUser(content="Summarize the result."),
                ],
                tools=[ToolInfo(name="lookup", description="Lookup the status.", parameters=ToolParams())],
            )

    replayed = json.loads(requests[1].content)["messages"][1]["reasoning_details"]
    assert replayed == reasoning_details
    bounded_request, bound = bounded_requests[1]
    assert bounded_request["messages"][1]["reasoning_details"] == reasoning_details
    without_reasoning = deepcopy(bounded_request)
    without_reasoning["messages"][1].pop("reasoning_details")
    tariff = {
        "input": 1.0,
        "output": 5.0,
        "input_cache_read": 1.0,
        "input_cache_write": 1.0,
        "input_cache_write_1h": 1.0,
        "internal_reasoning": 0.0,
        "request": 0.0,
    }
    assert bound == original_charge_bound(bounded_request, tariff)
    assert bound > original_charge_bound(without_reasoning, tariff)
    assert reservations_at_send[1] == bound
    assert len(requests) == 2


async def test_real_inspect_retry_hits_unresolved_target_hold_before_second_send(
    tmp_path, monkeypatch
):
    model_id = "openrouter/anthropic/claude-opus-5"
    ledger, reserve_attempts = _install_real_target_budget(monkeypatch, tmp_path, model_id)
    requests = []

    def transport(request):
        assert Decimal(ledger.snapshot()["reserved_usd"]) > 0
        requests.append(request)
        return httpx2.Response(
            503,
            request=request,
            json={"error": {"message": "offline transient failure", "type": "server_error"}},
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as client:
        model = get_model(
            model_id,
            http_client=client,
            memoize=False,
            config=GenerateConfig(max_tokens=16, max_retries=1, cache_prompt=False),
            stream=False,
        )
        async with dollar_budget.target_dollar_budget(model):
            assert model.api.model_args["max_retries"] == 0
            assert model.api.client.max_retries == 0
            with pytest.raises(WorldSpendLedgerError, match="cannot reserve"):
                await model.generate("Retry after provider error.")

    assert len(requests) == 1
    assert len(reserve_attempts) == 2
    snapshot = ledger.snapshot()
    assert snapshot["unresolved_attempts"] == 1
    assert Decimal(snapshot["reserved_usd"]) > 0

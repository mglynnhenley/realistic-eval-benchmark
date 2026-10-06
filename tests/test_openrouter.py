"""Exercise the actual provider and SDK through an offline HTTP transport."""

import json

import httpx2
import pytest
from inspect_ai.model import GenerateConfig, get_model, get_model_info

from refund_eval.config import DEFAULT_ROLE_MODEL, DEFAULT_TARGET_MODEL
from refund_eval.task import register_default_costs


@pytest.mark.parametrize(("name", "slug", "prices"), [
    (DEFAULT_TARGET_MODEL, "anthropic/claude-opus-5", (5.0, 25.0)),
    (DEFAULT_ROLE_MODEL, "openai/gpt-6-astra", (10.0, 50.0)),
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

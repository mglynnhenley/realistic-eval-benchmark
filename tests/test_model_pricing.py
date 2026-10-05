"""Budget pricing loads from public metadata without making model calls."""

import httpx
import pytest
from inspect_ai.model import (
    ModelCost,
    ModelInfo,
    get_model,
    get_model_info,
    set_model_info,
)
from inspect_ai.model._model_info import clear_model_info_cache

from refund_eval import model_pricing as pricing
from refund_eval.task import register_default_costs

FABLE = "openrouter/anthropic/claude-fable-5"
NEW = "openrouter/example/new-model"
FABLE_ENTRY = {
    "id": "anthropic/claude-fable-5",
    "pricing": {
        "prompt": "0.00001", "completion": "0.00005",
        "input_cache_read": "0.000001", "input_cache_write": "0.0000125",
    },
}


@pytest.fixture(autouse=True)
def isolated_prices():
    clear_model_info_cache()
    yield
    clear_model_info_cache()


def catalog(monkeypatch, entries):
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        assert url == pricing.CATALOG_URL
        assert kwargs == {"timeout": 15.0}  # No credentials or prompts sent.
        return httpx.Response(200, json={"data": entries}, request=httpx.Request("GET", url))

    monkeypatch.setattr(pricing.httpx, "get", get)
    return calls


def test_missing_role_prices_load_once_with_token_and_cache_conversion(monkeypatch):
    set_model_info(FABLE, ModelInfo(context_length=1_000_000))
    calls = catalog(monkeypatch, [FABLE_ENTRY])
    register_default_costs(["openrouter/anthropic/claude-opus-5", FABLE, FABLE])
    info = get_model_info(FABLE)
    assert info.context_length == 1_000_000
    assert info.cost == ModelCost(input=10, output=50, input_cache_read=1, input_cache_write=12.5)
    register_default_costs([FABLE])
    assert len(calls) == 1


def test_new_models_and_free_variants_keep_their_exact_prices(monkeypatch):
    calls = catalog(monkeypatch, [
        {"id": "example/new-model", "pricing": {"prompt": "0.000002", "completion": "0.000003"}},
        {"id": "example/new-model:free", "pricing": {"prompt": "0", "completion": "0"}},
    ])
    assert get_model_info(NEW) is None
    pricing.register_openrouter_costs([NEW, NEW + ":free"])
    paid, free = get_model_info(NEW).cost, get_model_info(NEW + ":free").cost
    assert paid == ModelCost(input=2, output=3, input_cache_read=2, input_cache_write=2)
    assert free == ModelCost(input=0, output=0, input_cache_read=0, input_cache_write=0)
    assert len(calls) == 1


def test_explicit_prices_and_offline_models_need_no_network(monkeypatch):
    custom = ModelCost(input=9, output=19, input_cache_read=1, input_cache_write=11)
    set_model_info(FABLE, ModelInfo(cost=custom))

    def unexpected(*args, **kwargs):
        pytest.fail("Models with prices and offline models must not fetch pricing")

    monkeypatch.setattr(pricing.httpx, "get", unexpected)
    register_default_costs([FABLE, "mockllm/target", "anthropic/unknown"])
    assert get_model_info(FABLE).cost == custom
    assert get_model_info("mockllm/target").cost.input == 0


@pytest.mark.parametrize("name", [FABLE, "openrouter/anthropic/claude-opus-5"])
def test_canonical_price_override_is_preserved(monkeypatch, name):
    custom = ModelCost(input=9, output=19, input_cache_read=1, input_cache_write=11)
    set_model_info(name.removeprefix("openrouter/"), ModelInfo(cost=custom))
    calls = catalog(monkeypatch, [FABLE_ENTRY])
    register_default_costs([name])
    assert get_model_info(get_model(name, api_key="offline-test-key")).cost == custom
    assert not calls


def test_disabled_budget_does_not_require_a_catalog_connection(monkeypatch):
    calls = catalog(monkeypatch, [FABLE_ENTRY])
    register_default_costs([FABLE], fetch_openrouter=False)
    assert not calls


def test_unknown_variant_does_not_silently_use_base_model_prices(monkeypatch):
    catalog(monkeypatch, [FABLE_ENTRY])
    with pytest.raises(ValueError, match="Check the exact model ID"):
        pricing.register_openrouter_costs([FABLE + ":unknown"])
    info = get_model_info(FABLE + ":unknown")
    assert info is None or info.cost is None


@pytest.mark.parametrize("failure", ["timeout", "http", "json", "shape"])
def test_catalog_failure_stops_before_registering_prices(monkeypatch, failure):
    def get(url, **kwargs):
        request = httpx.Request("GET", url)
        if failure == "timeout":
            raise httpx.ConnectTimeout("offline", request=request)
        if failure == "http":
            return httpx.Response(503, request=request)
        if failure == "json":
            return httpx.Response(200, text="not JSON", request=request)
        return httpx.Response(200, json={"data": {}}, request=request)

    monkeypatch.setattr(pricing.httpx, "get", get)
    with pytest.raises(ValueError, match="Could not load OpenRouter pricing"):
        pricing.register_openrouter_costs([NEW])
    assert get_model_info(NEW) is None


@pytest.mark.parametrize("field,value", [
    ("prompt", None), ("completion", "-1"), ("prompt", "NaN"),
    ("completion", "Infinity"), ("prompt", True), ("completion", "1e9999"),
    ("input_cache_read", "bad"), ("input_cache_write", -0.1),
])
def test_invalid_prices_cannot_disable_the_budget(monkeypatch, field, value):
    entry = {"id": "example/new-model", "pricing": {**FABLE_ENTRY["pricing"], field: value}}
    catalog(monkeypatch, [FABLE_ENTRY, entry])
    with pytest.raises(ValueError, match=f"invalid {field} price"):
        pricing.register_openrouter_costs([FABLE, NEW])
    assert get_model_info(NEW) is None
    # Validate every requested model before registering any of the new prices.
    info = get_model_info(FABLE)
    assert info is None or info.cost is None

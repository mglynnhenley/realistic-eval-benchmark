"""Real provider encoding checks with no inference or external network calls."""

from __future__ import annotations

import copy
import os
from types import SimpleNamespace

import httpx
import pytest
from inspect_ai.model import GenerateConfig
from inspect_ai.model._openai import openai_chat_tools
from inspect_ai.model._openai_responses import openai_responses_tools
from inspect_ai.model._providers.openrouter import OpenRouterAPI
from inspect_ai.tool import ToolInfo

from refund_eval.published.adapter import fingerprint, inspect_parameters
from refund_eval.published.fixtures import fixture_mail_catalog
from refund_eval.published.provider import check_published_model, published_model_args
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG


@pytest.fixture(autouse=True)
def no_model_or_network_requests(monkeypatch):
    async def forbidden(*_args, **_kwargs):
        pytest.fail("Provider schema verification must never call a model or remote endpoint")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def _tool_infos(catalog):
    return [ToolInfo(name=tool["name"], description=tool["description"],
                     parameters=inspect_parameters(tool["inputSchema"])) for tool in catalog["tools"]]


def _assert_preserved_parameters(native, encoded):
    assert set(encoded["properties"]) == set(native.get("properties", {}))
    assert encoded.get("required", []) == native.get("required", [])
    assert encoded.get("additionalProperties", True) == native.get("additionalProperties", True)
    # Strict conversion must not replace optional native scalars with nullables.
    for name, schema in native.get("properties", {}).items():
        assert encoded["properties"][name].get("type") == schema.get("type")


@pytest.mark.parametrize("model", ["openrouter/anthropic/test", "openrouter/openai/test", "openrouter/google/test"])
@pytest.mark.parametrize("catalog", [fixture_mail_catalog(), PINNED_EXA_SEARCH_CATALOG])
async def test_native_catalogs_encode_without_invalid_strict_requirement(model, catalog):
    original = copy.deepcopy(catalog)
    digest = fingerprint(catalog)
    tools = _tool_infos(catalog)
    provider = OpenRouterAPI(model.removeprefix("openrouter/"), api_key="offline-placeholder",
                             **published_model_args(model))
    try:
        encoded = provider.tools_to_openai(tools)
        assert all(tool["function"]["strict"] is False for tool in encoded)
        for native, wire in zip(catalog["tools"], encoded, strict=True):
            _assert_preserved_parameters(native["inputSchema"], wire["function"]["parameters"])
        assert catalog == original
        assert fingerprint(catalog) == digest
    finally:
        await provider.aclose()


def test_direct_openai_keeps_non_strict_encoding_without_extra_constructor_flags():
    tools = _tool_infos(PINNED_EXA_SEARCH_CATALOG)
    assert published_model_args("openai/test") == {}
    assert all(tool["function"].get("strict") is not True for tool in openai_chat_tools(tools))
    response_tools = openai_responses_tools(tools, "test", GenerateConfig())
    assert all(tool["strict"] is False for tool in response_tools)
    _assert_preserved_parameters(PINNED_EXA_SEARCH_CATALOG["tools"][0]["inputSchema"],
                                 response_tools[0]["parameters"])


def test_cli_strict_configuration_fails_before_a_model_request():
    class Model:
        api = SimpleNamespace(strict_tools=True)

        def __str__(self):
            return "openrouter/openai/test"

    with pytest.raises(ValueError, match="-M strict_tools=false"):
        check_published_model(Model())
    model = Model()
    model.api = SimpleNamespace(strict_tools=False)
    check_published_model(model)
    assert published_model_args("mockllm/scripted") == {}
    assert published_model_args("anthropic/test") == {}
    assert published_model_args("google/test") == {}


@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("REFUND_EVAL_SUBPROCESS_BROWSER_SMOKE") != "1",
                    reason="Requires pinned packaged Node, Playwright and Chromium")
async def test_real_browser_catalog_encodes_for_all_openrouter_families(tmp_path):
    from refund_eval.published.browser import PublishedBrowserService

    async def forbidden(_):
        pytest.fail("Provider schema checks never make an experimental browser request")

    async with PublishedBrowserService(forbidden, spool_dir=tmp_path / "exchange", runtime="subprocess") as browser:
        catalog = await browser.list_tools()
    digest = fingerprint(catalog)
    tools = _tool_infos(catalog)
    for family in ("anthropic", "openai", "google"):
        model = f"openrouter/{family}/test"
        provider = OpenRouterAPI(f"{family}/test", api_key="offline-placeholder", **published_model_args(model))
        try:
            encoded = provider.tools_to_openai(tools)
            for native, wire in zip(catalog["tools"], encoded, strict=True):
                assert wire["function"]["strict"] is False
                _assert_preserved_parameters(native["inputSchema"], wire["function"]["parameters"])
        finally:
            await provider.aclose()
    assert fingerprint(catalog) == digest

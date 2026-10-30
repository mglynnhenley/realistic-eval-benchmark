"""Run the real bounded tells scanner and OpenRouter SDK against offline HTTP."""

import json

import httpx2
import pytest
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageTool,
    ChatMessageUser,
    get_model,
)
from inspect_ai.tool import ToolCall
from inspect_scout import Transcript

from refund_eval.realism import scan_budget
from scout import scanners


@pytest.mark.parametrize("slug", ["anthropic/claude-opus-5.5", "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4.1-flash"])
async def test_bounded_tells_wire_request_never_enables_web_search(slug):
    requests = []

    def completion(request):
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        assert body["model"] == slug and ":online" not in body["model"]
        assert body.get("plugins") == []
        assert "web_search_options" not in body
        assert {tool["function"]["name"] for tool in body["tools"]} == {"answer"}
        assert all(tool["type"] == "function" for tool in body["tools"])
        assert body["max_tokens"] == 8192
        if slug.startswith("deepseek/"):
            assert "cache_control" not in json.dumps(body)
        if slug == "deepseek/deepseek-v4.1-flash":
            assert body.get("reasoning") == {"enabled": False}
            assert body["reasoning"]["enabled"] is False
        else:
            assert "reasoning" not in body
        return httpx2.Response(200, json={
            "id": "offline-tells", "object": "chat.completion", "created": 0, "model": slug,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "answer-once", "type": "function", "function": {
                    "name": "answer", "arguments": json.dumps({"tells": [], "explanation": "Synthetic empty result."})}}]},
                "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(completion)) as client:
        model = get_model("openrouter/" + slug, api_key="synthetic-offline-only", http_client=client,
                          base_url="https://openrouter.ai/api/v1", memoize=False, stream=False,
                          max_retries=0,
                          config=scan_budget.bounded_tells_config(8192, judge="openrouter/" + slug))
        scanner = scanners.tells_scanner(model=model, bounded=True)
        result = await scanner(Transcript(transcript_id="synthetic", messages=[
            ChatMessageUser(content="A synthetic saved interaction."),
            ChatMessageAssistant(content="", tool_calls=[ToolCall(
                id="historical-search", function="web_search", arguments={"query": "synthetic enquiry"})]),
            ChatMessageTool(tool_call_id="historical-search", content="Synthetic saved search result.")]))
    assert result.value["tells"] == []
    assert len(requests) == 1


@pytest.mark.parametrize("disable_reasoning", [False, True])
async def test_flash_wire_switch_prevents_reasoning_only_length_response_without_retry(disable_reasoning):
    slug = "deepseek/deepseek-v4.1-flash"
    requests = []

    def completion(request):
        body = json.loads(request.content)
        requests.append(body)
        disabled = body.get("reasoning", {}).get("enabled") is False
        message = {"role": "assistant", "content": None}
        if disabled:
            message["tool_calls"] = [{"id": "answer-once", "type": "function", "function": {
                "name": "answer", "arguments": json.dumps({"tells": [], "explanation": "Synthetic answer."})}}]
        else:
            message["reasoning"] = "Synthetic reasoning exhausted the output allowance."
        return httpx2.Response(200, json={
            "id": "offline-flash-policy", "object": "chat.completion", "created": 0, "model": slug,
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if disabled else "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5 if disabled else 8192,
                      "total_tokens": 15 if disabled else 8202,
                      "completion_tokens_details": {"reasoning_tokens": 0 if disabled else 8192}}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(completion)) as client:
        model = get_model("openrouter/" + slug, api_key="synthetic-offline-only", http_client=client,
                          base_url="https://openrouter.ai/api/v1", memoize=False, stream=False, max_retries=0,
                          config=scan_budget.bounded_tells_config(8192, judge="openrouter/" + slug if disable_reasoning else None))
        result = await scanners.tells_scanner(model=model, bounded=True)(Transcript(
            transcript_id="synthetic-flash-policy", messages=[ChatMessageUser(content="Synthetic saved interaction.")]))
    assert len(requests) == 1 and requests[0]["max_tokens"] == 8192
    if disable_reasoning:
        assert requests[0]["reasoning"]["enabled"] is False
        assert result.value["tells"] == []
    else:
        assert "reasoning" not in requests[0]
        assert result.value is None

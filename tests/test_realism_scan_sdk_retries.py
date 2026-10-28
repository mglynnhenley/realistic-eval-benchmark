"""Exercise the production CLI, Scout and real SDK with offline failure responses."""

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
import pytest
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalSample,
    EvalSpec,
    write_eval_log,
)
from inspect_ai.model import ChatMessageUser
from inspect_ai.model._providers import openai_compatible


@pytest.mark.parametrize("slug", ["anthropic/claude-opus-5.5", "deepseek/deepseek-v4-pro"])
@pytest.mark.parametrize("response", [200, 429, 503, "transport"])
def test_bounded_cli_makes_one_sdk_request(tmp_path, monkeypatch, slug, response):
    directory = Path(__file__).resolve().parents[1] / "scout"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("sdk_retry_scout_run", directory / "run.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(runner, "estimate", lambda *a: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-offline-only")

    source = tmp_path / "synthetic.eval"
    write_eval_log(EvalLog(status="success", eval=EvalSpec(
        created=datetime(2026, 10, 28, tzinfo=UTC).isoformat(), task="train_refund_late",
        model="mockllm/target", dataset=EvalDataset(samples=1), config=EvalConfig()), samples=[
            EvalSample(id="synthetic", epoch=1, input="Synthetic saved interaction.", target="",
                       messages=[ChatMessageUser(content="Synthetic saved interaction.")],
                       metadata={"scenario": "train_refund_late",
                                 "task_grading_notes": {"notes": "Synthetic scenario notes."}})]), source)

    requests = []
    urls = []

    def completion(request):
        urls.append(str(request.url))
        body = json.loads(request.content)
        requests.append(body)
        if response == "transport":
            raise httpx2.ConnectError("Synthetic offline transport failure", request=request)
        if response != 200:
            return httpx2.Response(response, json={"error": {"message": "Synthetic offline HTTP failure"}})
        return httpx2.Response(200, json={
            "id": "offline-tells", "object": "chat.completion", "created": 0, "model": slug,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": "answer-once", "type": "function", "function": {
                    "name": "answer", "arguments": json.dumps({"tells": [], "explanation": "Synthetic result."})}}]},
                "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(completion))
    sdk_constructor = openai_compatible.AsyncOpenAI

    def offline_sdk(**kwargs):
        # Only replace HTTP transport. Preserve actual production retry arguments
        # and construct the real SDK client, including its retry/error machinery.
        kwargs["http_client"] = client
        return sdk_constructor(**kwargs)

    monkeypatch.setattr(openai_compatible, "AsyncOpenAI", offline_sdk)
    statuses = []
    real_scan = runner.scan

    def scan(**kwargs):
        status = real_scan(**kwargs, max_processes=1, display="none")
        statuses.append(status)
        return status

    monkeypatch.setattr(runner, "scan", scan)
    try:
        runner.main([str(source), "--judges", "openrouter/" + slug, "--scans", str(tmp_path / "scans"),
                     "--scanners", "tells", "--bounded-tells", "--max-tokens", "8192", "--yes"])
    finally:
        asyncio.run(client.aclose())
    print(json.dumps({"judge": slug, "response": response, "request_count": len(requests)}))
    assert len(statuses) == 1
    assert len(requests) == 1
    # Assert outside the transport: Scout records exceptions as scan errors,
    # which must not swallow request-shape assertion failures in negative cases.
    assert urls == ["https://openrouter.ai/api/v1/chat/completions"]
    body = requests[0]
    assert body["model"] == slug
    assert body.get("plugins") == []
    assert "web_search_options" not in body
    assert [(tool["type"], tool["function"]["name"]) for tool in body["tools"]] == [("function", "answer")]
    assert body["max_tokens"] == 8192
    assert statuses[0].complete is (response == 200)
    assert bool(statuses[0].errors) is (response != 200)
    if response != 200:
        error = json.loads((Path(statuses[0].location) / "_errors.jsonl").read_text())
        expected = {429: "RateLimitError", 503: "InternalServerError", "transport": "APIConnectionError"}[response]
        assert expected in error["traceback"]

    # Scout persists constructor arguments for evidence and later reconstruction.
    specification = json.loads((Path(statuses[0].location) / "_scan.json").read_text())
    role = specification["model_roles"]["judge"]
    assert role["args"]["max_retries"] == 0
    assert role["config"]["max_retries"] == 0
    assert role["config"]["max_tokens"] == 8192
    assert role["config"]["cache_prompt"] is False
    assert role["config"]["extra_body"] == {"plugins": []}

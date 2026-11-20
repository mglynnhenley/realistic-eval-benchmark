from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput
from openai import AsyncOpenAI

from refund_eval.companies.models import CompanyAction
from refund_eval.harbor import atif
from refund_eval.models.openrouter import OpenRouterClient
from refund_eval.published.mail import _visible_id
from refund_eval.scenario.schema import SeedEmail, load_scenario
from refund_eval.sim.router import resolve_org
from refund_eval.world.state import WORLD_KEY, World
from tests.test_world_service import (
    AVANTI,
    MAX_TURNS,
    WAITING,
    run_inspect,
    settings,
    stub_browser,
    world_service_thread,
)
from tests.test_world_service import offline as offline_fixture  # noqa: F401

pytestmark = pytest.mark.usefixtures("offline_fixture")


MODEL = "openrouter/anthropic/claude-opus-5.5"
SUBJECT = "Synthetic correspondence check"
BODY = "Please review this synthetic request and tell me what evidence is required."
REPLIES = {
    "reply/evidence_required": {
        "decision": "request_evidence",
        "reply": True,
        "reply_after_hours": 1,
        "body": "syntheticreplyzx9: evidence required.",
    },
    "denial": {
        "decision": "deny",
        "reply": True,
        "reply_after_hours": 1,
        "body": "syntheticdenialzx9: request declined.",
    },
    "intentional_nonreply": {
        "decision": "no_reply",
        "reply": False,
        "reply_after_hours": 1,
        "body": "",
    },
}


class CorrespondenceAgent:
    def __init__(self, case: str):
        self.case = case
        self.step = 0
        self.search_call_id = None
        self.message_id = None

    def _message_id(self, input):
        for message in reversed(input):
            if getattr(message, "tool_call_id", None) != self.search_call_id:
                continue
            content = getattr(message, "text", None)
            if not content:
                continue
            try:
                result = json.loads(content)
            except (TypeError, ValueError):
                continue
            for thread in result.get("threads", []):
                for candidate in thread.get("messages", []):
                    labels = set(candidate.get("labelIds", []))
                    if self.case == "intentional_nonreply":
                        if ("INBOX" in labels and "UNREAD" not in labels
                                and "syntheticseedzx9" in candidate.get("snippet", "")):
                            return candidate.get("id")
                    elif REPLIES[self.case]["body"].split(":", 1)[0] in candidate.get("snippet", ""):
                        return candidate.get("id")
        return None

    def __call__(self, input, tools, tool_choice, config):
        step = self.step
        self.step += 1
        if step == 0:
            return ModelOutput.for_tool_call(
                "mockllm/model", "send_message",
                {"to": [AVANTI], "subject": SUBJECT, "body": BODY},
                tool_call_id="synthetic-send",
            )
        if step <= len(WAITING) + 2:
            return ModelOutput.from_content("mockllm/model", "I will wait for the normal delivery window.")
        if step == len(WAITING) + 3:
            self.search_call_id = "synthetic-search"
            query = "in:inbox syntheticseedzx9" if self.case == "intentional_nonreply" else (
                f"in:inbox {REPLIES[self.case]['body'].split(':', 1)[0]}"
            )
            return ModelOutput.for_tool_call(
                "mockllm/model", "search_threads", {"query": query, "pageSize": 50},
                tool_call_id=self.search_call_id,
            )
        if step == len(WAITING) + 4:
            self.message_id = self._message_id(input)
            if self.message_id is None:
                return ModelOutput.from_content("mockllm/model", "No matching inbox message is available.")
            return ModelOutput.for_tool_call(
                "mockllm/model", "get_message", {"messageId": self.message_id},
                tool_call_id="synthetic-read",
            )
        return ModelOutput.from_content("mockllm/model", "The inbox observation is complete.")


def _openrouter(case: str, artifact_dir):
    requests = []

    def handler(request):
        assert request.method == "POST"
        assert request.url.path.endswith("/chat/completions")
        body = json.loads(request.content)
        schema_name = body.get("response_format", {}).get("json_schema", {}).get("name")
        metadata = {
            "model": body.get("model"),
            "message_roles": [message.get("role") for message in body.get("messages", [])],
            "schema": body.get("response_format"),
            "provider": body.get("provider", {}),
        }
        requests.append(metadata)
        if schema_name == "open_world_action":
            output = CompanyAction.model_validate({**REPLIES[case], "subject": SUBJECT}).model_dump(mode="json")
        elif schema_name == "response_consistency":
            output = {"consistent": True, "reason": "synthetic fixture response"}
        else:
            raise AssertionError(f"Unexpected world-model schema: {schema_name!r}")
        response = {
            "id": f"mock-{len(requests)}",
            "object": "chat.completion",
            "created": 1,
            "model": body["model"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(output)},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost": 0.0001},
        }
        return httpx.Response(200, json=response)

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sdk = AsyncOpenAI(api_key="local-test-key", base_url="https://openrouter.ai/api/v1",
                      http_client=transport, max_retries=0)
    prices = {MODEL: {
        "input": 0.0, "output": 0.0, "input_cache_read": 0.0, "input_cache_write": 0.0,
    }}
    tariffs = {MODEL: {
        "input": 0.0, "output": 0.0, "input_cache_read": 0.0, "input_cache_write": 0.0,
        "input_cache_write_1h": 0.0, "internal_reasoning": 0.0, "request": 0.0,
    }}
    client = OpenRouterClient({"simulator": MODEL, "grader": MODEL}, client=sdk, prices=prices,
                              tariffs=tariffs, pin_prices=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return client, requests, artifact_dir / "openrouter-request-metadata.json"


@pytest.fixture(autouse=True)
def no_nonloopback_http(monkeypatch):
    allowed = {"127.0.0.1", "localhost", "::1"}
    async_request = httpx.AsyncHTTPTransport.handle_async_request
    sync_request = httpx.HTTPTransport.handle_request

    async def guarded_async(self, request):
        assert request.url.host in allowed, "non-loopback HTTP is disabled in this test"
        return await async_request(self, request)

    def guarded_sync(self, request):
        assert request.url.host in allowed, "non-loopback HTTP is disabled in this test"
        return sync_request(self, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", guarded_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", guarded_sync)


@pytest.mark.parametrize("case", tuple(REPLIES))
def test_synthetic_company_correspondence_is_delivered_read_and_traced(case, tmp_path, monkeypatch):
    stub_browser(monkeypatch)
    scenario_name = "train_refund_late"
    scenario_data = load_scenario(scenario_name)
    if case == "intentional_nonreply":
        scenario_data.seed_emails.append(SeedEmail(
            id="synthetic-preexisting-seed-zx9",
            sender="seed@example.test",
            to=[scenario_data.user.email_addresses[0]],
            subject="Synthetic preexisting message",
            body="syntheticseedzx9: This preexisting synthetic message is for inbox reading.",
            sent_at=scenario_data.start_time - timedelta(hours=1),
        ))
        scenario_path = tmp_path / "scenario-with-synthetic-seed.yaml"
        scenario_path.write_text(scenario_data.model_dump_json(), encoding="utf-8")
        scenario_name = str(scenario_path)
    company_key = resolve_org(scenario_data, AVANTI)
    model_client, requests, request_artifact = _openrouter(case, tmp_path)
    config = settings(
        tmp_path,
        scenario=scenario_name,
        simulator_model=MODEL,
        grader_model=MODEL,
        max_turns=MAX_TURNS + 2,
    )
    before_ids = set()
    before_interception_ids = set()
    before_effects = (0, 0)
    agent = CorrespondenceAgent(case)
    try:
        with world_service_thread(config, monkeypatch, model_client=model_client) as (url, held):
            world = World.model_validate(held["trial"].trial.data[WORLD_KEY])
            before_ids = {mail.id for mail in [*world.overlay_inbox, *(item.email for item in world.pending)]}
            before_ids.update(mail.id for mail in scenario_data.seed_emails)
            before_interception_ids = {item.id for item in world.interceptions}
            before_effects = (len(world.outcome.offers), len(world.outcome.transfers))
            run_inspect(
                tmp_path,
                scenario=scenario_name,
                script=agent,
                world_url=url,
                max_turns=MAX_TURNS + 2,
            )
        request_artifact.write_text(json.dumps(requests, indent=2), encoding="utf-8")
    finally:
        asyncio.run(model_client.client.close())

    log_path = next((tmp_path / "logs").glob("*.eval"))
    log = read_eval_log(str(log_path))
    assert log.status == "success"
    trajectory = atif.convert(log)
    (tmp_path / "correspondence-atif.json").write_text(json.dumps(trajectory, indent=2), encoding="utf-8")
    assert trajectory["schema_version"] == atif.SCHEMA_VERSION == "ATIF-v1.8"
    assert requests
    assert all(item["model"] == MODEL.removeprefix("openrouter/") for item in requests)
    schema_names = [item["schema"]["json_schema"]["name"] for item in requests]
    assert schema_names.count("open_world_action") == 1
    assert schema_names.count("response_consistency") == 1
    action_request = next(item for item in requests if item["schema"]["json_schema"]["name"] == "open_world_action")
    action_format = action_request["schema"]["json_schema"]
    action_schema = action_format["schema"]
    assert action_format["strict"] is True
    assert action_request["provider"]["require_parameters"] is True
    assert action_request["provider"]["max_price"] == {"prompt": 0.0, "completion": 0.0, "request": 0.0}
    unsupported = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength"}

    def visit_schema(node):
        if isinstance(node, dict):
            assert unsupported.isdisjoint(node)
            if node.get("type") == "object" and isinstance(node.get("properties"), dict):
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node["properties"])
            for value in node.values():
                visit_schema(value)
        elif isinstance(node, list):
            for value in node:
                visit_schema(value)

    visit_schema(action_schema)
    assert "at least 0" in action_schema["properties"]["reply_after_hours"]["description"]
    assert "at least 0" in action_schema["properties"]["new_offers"]["items"]["properties"]["amount"]["description"]
    assert "greater than 0" in action_schema["properties"]["payments"]["items"]["properties"]["amount"]["description"]
    consistency_request = next(item for item in requests if item["schema"]["json_schema"]["name"] == "response_consistency")
    assert "strict" not in consistency_request["schema"]["json_schema"]
    assert "require_parameters" not in consistency_request["provider"]

    trace_calls = [
        call
        for step in trajectory["steps"]
        for call in step.get("tool_calls", [])
    ]
    names = [call.get("function_name") for call in trace_calls]
    assert "send_message" in names
    assert "search_threads" in names
    assert "get_message" in names
    linked_observations = {}
    for step in trajectory["steps"]:
        for call in step.get("tool_calls", []):
            linked_observations[call["function_name"]] = [
                result for result in step.get("observation", {}).get("results", [])
                if result.get("source_call_id") == call["tool_call_id"]
            ]
    assert all(linked_observations.get(name) for name in
               ("send_message", "search_threads", "get_message"))
    def observation_payload(name):
        [result] = linked_observations[name]
        payload = json.loads(result["content"])
        assert isinstance(payload, dict)
        return payload

    send_observation = observation_payload("send_message")
    assert "SENT" in send_observation.get("labelIds", [])
    read_observation = observation_payload("get_message")
    assert read_observation.get("id") == agent.message_id
    assert isinstance(read_observation.get("plaintextBody"), str)

    # Read the captured state only in assertions; the scripted agent uses tool outputs.
    final_world = World.model_validate(held["data"][WORLD_KEY])
    new_messages = [mail for mail in final_world.overlay_inbox if mail.id not in before_ids]
    if case == "intentional_nonreply":
        assert not any(mail.org == company_key for mail in new_messages)
        new_pending = [item.email for item in final_world.pending if item.email.id not in before_ids]
        assert not any(mail.org == company_key for mail in new_pending)
        [interaction] = final_world.interactions
        assert interaction.status == "silent"
        assert interaction.error is None
        assert interaction.action is not None and interaction.action.reply is False
        assert len(interaction.attempts) == 1
        assert not interaction.effects
        assert final_world.stop_reason in (None, "max_turns")
        assert agent.message_id in before_ids | {_visible_id(message_id) for message_id in before_ids}
    else:
        delivered = [
            mail for mail in new_messages
            if mail.org == company_key and REPLIES[case]["body"] in mail.body
        ]
        assert len(delivered) == 1
        assert agent.message_id in {_visible_id(mail.id) for mail in delivered}
        assert REPLIES[case]["body"] in read_observation["plaintextBody"]
        [sent_interception] = [
            item for item in final_world.interceptions
            if item.id not in before_interception_ids
            and item.payload.get("message_id") == send_observation["id"]
        ]
        assert sent_interception.payload["thread_id"] == send_observation["threadId"]
        assert delivered[0].thread_id == sent_interception.payload["thread_id"]
        queued_events = [
            event for event in held["data"]["events"]
            if event["kind"] == "mail_queued"
            and event["payload"]["scheduled"]["email"]["id"] == delivered[0].id
        ]
        delivery_events = [
            event for event in held["data"]["events"]
            if event["kind"] == "mail_delivered"
            and event["payload"]["email"]["id"] == delivered[0].id
        ]
        [queued_event] = queued_events
        [delivery_event] = delivery_events
        assert queued_event["seq"] < delivery_event["seq"]
        assert queued_event["payload"]["scheduled"]["due_at"]
        assert delivery_event["payload"]["email"]["thread_id"] == sent_interception.payload["thread_id"]
        [interaction] = final_world.interactions
        assert interaction.status == "applied"
        if case == "denial":
            assert interaction.action.decision == "deny"
            assert (len(final_world.outcome.offers), len(final_world.outcome.transfers)) == before_effects

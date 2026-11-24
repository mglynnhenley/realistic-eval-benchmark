"""Offline world-mode handling for strict target budget stops."""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import httpx2
import pytest
from inspect_ai.model import GenerateConfig, get_model
from inspect_ai.util import LimitExceededError

from refund_eval.agent import dollar_budget, world_mode
from refund_eval.models.world_spend_ledger import WorldSpendLedger
from refund_eval.scenario.schema import AgentScenario

MODEL_ID = "openrouter/anthropic/claude-opus-5"
TARIFF = {
    "input": 1.0,
    "output": 5.0,
    "input_cache_read": 1.0,
    "input_cache_write": 1.0,
    "input_cache_write_1h": 1.0,
    "internal_reasoning": 0.0,
    "request": 0.0,
}


class RecordingWorldClient:
    def __init__(self, _url, _token, _view):
        self.finishes = []
        self.stopped = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def tools(self):
        return []

    def on_continue(self, _history_tokens):
        return lambda _state: True

    async def finish(self, stop_reason, **_kwargs):
        if not self.stopped:
            self.finishes.append(stop_reason)
            self.stopped = True


async def test_run_in_world_turns_target_budget_exhaustion_into_cost_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_TURN_TOKEN", "offline-world-token")
    monkeypatch.setenv("WORLD_SCENARIO", "train_refund_late")
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-agent-key")
    monkeypatch.setenv("REFUND_EVAL_AGENT_DOLLAR_CAP", "0.02")
    monkeypatch.setenv("REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS", "16")
    monkeypatch.setattr(dollar_budget, "openrouter_tariffs", lambda _models: {MODEL_ID: TARIFF})
    exhausted_ledger = WorldSpendLedger(
        tmp_path / "agent-spend", Decimal("0.02"),
        {"scenario": "train_refund_late", "models": {"agent": MODEL_ID}},
    )
    attempt_id = "00000000-0000-4000-8000-000000000001"
    exhausted_ledger.reserve(attempt_id, "a" * 64, Decimal("0.01"))
    exhausted_ledger.settle(attempt_id, Decimal("0.01"), "reported")
    monkeypatch.setattr(dollar_budget, "WorldSpendLedger", lambda *_args: exhausted_ledger)

    requests = []

    def transport(request):
        requests.append(request)
        return httpx2.Response(200, request=request, json={
            "id": "offline-completion",
            "object": "chat.completion",
            "created": 0,
            "model": "anthropic/claude-opus-5",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "reply"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
                      "cost": 0.0001, "is_byok": False},
        })

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as client:
        model = get_model(
            MODEL_ID, http_client=client, memoize=False,
            config=GenerateConfig(max_tokens=32, max_retries=0), stream=False,
        )
        worlds = []

        def make_world(*args):
            world = RecordingWorldClient(*args)
            worlds.append(world)
            return world

        def react_with_target_model(**kwargs):
            async def run(state):
                await kwargs["model"].generate("Make one bounded call.")
                return state

            return run

        monkeypatch.setattr(world_mode, "WorldClient", make_world)
        monkeypatch.setattr(world_mode, "get_model", lambda: model)
        monkeypatch.setattr(world_mode, "react", react_with_target_model)
        monkeypatch.setattr(world_mode, "render_system_prompt", lambda *_args, **_kwargs: "prompt")
        monkeypatch.setattr(world_mode, "loop_stop_reason", lambda _state: "loop_ended")
        monkeypatch.setattr(world_mode, "kill_reason", lambda: None)
        monkeypatch.setattr(world_mode.WorldView, "record_clock", lambda _self: None)
        monkeypatch.setattr(
            world_mode, "transcript", lambda: SimpleNamespace(info=lambda *_args, **_kwargs: None)
        )
        scenario = AgentScenario(
            id="train_refund_late",
            task_text="Offline stop test.",
            start_time=datetime(2025, 1, 1, tzinfo=UTC),
            sim_weeks=1,
            user={"name": "Test User", "email_addresses": ["test@example.com"]},
        )
        with pytest.raises(LimitExceededError, match="Exceeded cost limit"):
            await world_mode.run_in_world(
                SimpleNamespace(messages=[]),
                scenario=scenario,
                world_url="http://world.invalid",
                history_tokens=0,
                sim_weeks=1,
                checkin_hours=(),
                token_budget=None,
                budget_notice="",
            )

    assert requests == []
    assert worlds[0].finishes == ["cost_limit"]

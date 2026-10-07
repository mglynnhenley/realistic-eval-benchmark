"""Resolve the real task and enforce Inspect budgets using offline model usage."""

from __future__ import annotations

import httpx
import pytest
from inspect_ai import eval
from inspect_ai.model import ModelOutput, ModelUsage, _model_info, get_model
from inspect_ai.model._providers.openrouter import OpenRouterAPI
from inspect_ai.solver import solver

from refund_eval import task as task_module
from refund_eval.published.provider import published_model_args

FABLE = "openrouter/anthropic/claude-fable-5"
CATALOG_URL = "https://openrouter.ai/api/v1/models"


@pytest.mark.parametrize("fable_target, budget, expected_calls", [
    (False, 0.015, 2),
    (True, 0.005, 1),
])
def test_task_registers_selected_model_costs_before_inspect_checks_budget(
    tmp_path, monkeypatch, fable_target, budget, expected_calls,
):
    # Keep registrations and negative lookup caches private to this test, so
    # another test's defaults cannot mask the original missing-price failure.
    monkeypatch.setattr(_model_info, "_custom_models", {})
    monkeypatch.setattr(_model_info, "_result_cache", {})
    monkeypatch.setenv("OPENROUTER_API_KEY", "offline-test-key")
    catalog_requests = []
    generations = []
    completed_solver = []

    def catalog(url, **kwargs):
        catalog_requests.append(str(url))
        assert str(url) == CATALOG_URL
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "data": [{
                "id": "anthropic/claude-fable-5", "name": "Claude Fable 5",
                "context_length": 1000000,
                "pricing": {
                    "prompt": "0.000003", "completion": "0.000015",
                    "input_cache_write": "0.00000375", "input_cache_read": "0.0000003",
                },
            }],
        })

    async def generate_offline(self, input, tools, tool_choice, config):
        generations.append(self.model_name)
        output = ModelOutput.from_content(self.model_name, "Offline completion.")
        # Inspect itself computes the cost and enforces the sample limit.
        output.usage = ModelUsage(input_tokens=1000, output_tokens=500, total_tokens=1500)
        return output

    @solver
    def exercise_models():
        async def solve(state, generate):
            state = await generate(state)
            await get_model(role="simulator").generate("Simulate an organisation reply.")
            await get_model(role="grader").generate("Grade the interaction.")
            completed_solver.append(True)
            return state

        return solve

    monkeypatch.setattr(httpx, "get", catalog)
    monkeypatch.setattr(OpenRouterAPI, "generate", generate_offline)
    monkeypatch.setattr(task_module, "refund_agent", lambda **kwargs: exercise_models())
    monkeypatch.setattr(task_module.adapters, "load_scorers", lambda *args, **kwargs: [])

    # The published task rejects OpenRouter targets that still use strict tool
    # generation, so build the target with the same provider args the workers use.
    target_name = FABLE if fable_target else "mockllm/target"
    target = get_model(target_name, memoize=False, **published_model_args(target_name))
    role_name = "mockllm/role" if fable_target else FABLE
    (log,) = eval(
        task_module.impossible_refund,
        task_args={"trials": 1, "cost_limit": budget},
        model=target,
        model_roles={"simulator": role_name, "grader": role_name},
        log_dir=str(tmp_path), display="none",
    )

    assert log.status == "success", log.error
    sample = log.samples[0]
    assert sample.error is None
    assert sample.limit is not None and sample.limit.type == "cost"
    assert sample.model_usage[FABLE].total_cost == pytest.approx(expected_calls * 0.0105)
    assert sample.model_usage[FABLE].total_cost > budget
    assert len(generations) == expected_calls
    assert not completed_solver  # The budget interrupted work, not just reporting.
    assert catalog_requests == [CATALOG_URL]

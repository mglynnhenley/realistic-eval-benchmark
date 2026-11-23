from __future__ import annotations

import asyncio
import subprocess
import zipfile
from decimal import Decimal
from types import SimpleNamespace
from typing import ClassVar

import pytest

from refund_eval.agent import dollar_budget
from refund_eval.harbor import adapter
from refund_eval.harbor.adapter import (
    SPLIT_BUDGET_WHEEL_SOURCES,
    verify_split_budget_wheel,
)
from refund_eval.models.world_spend_ledger import WorldSpendLedgerError
from refund_eval.scenario.schema import load_scenario
from scripts.offline_spend_guard import (
    GuardFailure,
    OpenRouterSnapshot,
    SplitSpendGuard,
    SplitThresholds,
)

PRICES = {
    "input": 1.0,
    "output": 2.0,
    "input_cache_read": 0.5,
    "input_cache_write": 1.5,
    "input_cache_write_1h": 2.0,
    "internal_reasoning": 1.0,
    "request": 0.0,
}
MODEL_ID = "openrouter/test-model"


class FakeLedger:
    instances: ClassVar[list[FakeLedger]] = []
    timeline: ClassVar[list] = []

    def __init__(self, directory, limit, scope):
        self.directory = directory
        self.limit = limit
        self.scope = scope
        self.events = []
        self.timeline = self.__class__.timeline
        self.hold = False
        self.spent = Decimal(0)
        self.__class__.instances.append(self)

    def reserve(self, attempt_id, digest, amount):
        if self.hold:
            raise WorldSpendLedgerError("unresolved hold")
        if self.spent + amount > self.limit:
            raise WorldSpendLedgerError("no affordable reservation")
        self.events.append(("reserve", amount))
        self.timeline.append(("reserve", amount))
        self.hold = True

    def settle(self, attempt_id, amount, provenance):
        self.events.append(("settle", amount, provenance))
        self.spent += amount
        self.hold = False

    def mark_unknown(self, attempt_id, *args):
        self.events.append(("unknown", *args))
        self.hold = True


class FakeClient:
    def __init__(self):
        self.options = {}
        self.generation_record = None
        self.generation_requests = 0

    def with_options(self, **kwargs):
        self.options.update(kwargs)
        return self

    async def get(self, *_args, **_kwargs):
        self.generation_requests += 1
        return self.generation_record


class FakeAPI:
    def __init__(self, events):
        self.base_url = "https://openrouter.ai/api/v1"
        self.responses_api = False
        self.emulate_tools = False
        self.models = None
        self.provider = None
        self.transforms = None
        self.client = FakeClient()
        self.model_args = {"max_retries": 3}
        self.events = events
        self.cache_prompt = False
        self.reported_cost = Decimal("0.0001")
        self.reported_is_byok = False
        self.send_started = None
        self.send_gate = None

    def _cache_prompt_enabled(self, config):
        return self.cache_prompt

    async def _generate_completion(self, request, config):
        self.events.append(("send", request.copy()))
        if self.send_gate is not None:
            self.send_started.set()
            await self.send_gate.wait()
        return SimpleNamespace(
            id="completion-id",
            model=MODEL_ID,
            usage=SimpleNamespace(cost=self.reported_cost, is_byok=self.reported_is_byok),
        )


class FakeModel:
    def __init__(self, events):
        self.api = FakeAPI(events)

    def __str__(self):
        return MODEL_ID


def _provider(prices):
    return {
        "max_price": {
            "prompt": max(prices[k] for k in ("input", "input_cache_read", "input_cache_write", "input_cache_write_1h")),
            "completion": prices["output"] + prices["internal_reasoning"],
            "request": prices["request"],
        },
        "require_parameters": True,
    }


def _request():
    return {"model": MODEL_ID, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 4,
            "extra_body": {}}


def _setup(monkeypatch, cap):
    FakeLedger.instances.clear()
    events = []
    FakeLedger.timeline = events
    monkeypatch.setattr(dollar_budget, "OpenRouterAPI", FakeAPI)
    monkeypatch.setattr(dollar_budget, "WorldSpendLedger", FakeLedger)
    monkeypatch.setattr(dollar_budget, "openrouter_tariffs", lambda models: {MODEL_ID: PRICES})
    monkeypatch.setenv("REFUND_EVAL_AGENT_DOLLAR_CAP", str(cap))
    monkeypatch.setenv("REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS", "16")
    monkeypatch.setenv("WORLD_SCENARIO", "train_refund_late")
    model = FakeModel(events)
    original = model.api._generate_completion
    client = model.api.client
    return model, events, original, client


def _write_public_agent_wheel(directory, *, stale=None):
    source = directory / "source"
    public_source = directory / "public-source"
    adapter.copy_source(source)
    adapter.stage_agent(
        source, public_source, load_scenario("train_refund_late"), scenario_name="train_refund_late"
    )
    if stale is not None:
        path = public_source / stale
        path.write_bytes(path.read_bytes() + b"stale")
    build_dir = directory / "built-wheel"
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(build_dir)],
        cwd=public_source,
        check=True,
        capture_output=True,
        text=True,
    )
    [wheel] = build_dir.glob("*.whl")
    return wheel


def _bound(request):
    final = {**request, "extra_body": {"provider": _provider(PRICES)}}
    return dollar_budget.charge_bound(final, PRICES)


def test_target_hook_reserves_before_send_and_restores_sdk_settings(monkeypatch):
    request = _request()
    bound = _bound(request)
    model, events, original, client = _setup(monkeypatch, bound)

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            assert model.api.model_args["max_retries"] == 0
            await model.api._generate_completion(request, {})

    asyncio.run(run())
    ledger = FakeLedger.instances[0]
    assert events[0] == ("reserve", bound)
    assert events[1][0] == "send"
    assert ledger.events[-1] == ("settle", Decimal("0.0001"), "reported")
    assert model.api._generate_completion.__func__ is original.__func__
    assert model.api.client is client
    assert model.api.model_args["max_retries"] == 3
    assert ledger.scope == {"scenario": "train_refund_late", "models": {"agent": MODEL_ID}}


def test_target_hook_rejects_second_bound_after_49_dollars_before_send(monkeypatch):
    request = _request()
    request["max_tokens"] = 4
    model, events, _, _ = _setup(monkeypatch, Decimal(50))
    prices = dict.fromkeys(PRICES, 0.0)
    prices["output"] = 12_250_000.0
    monkeypatch.setattr(dollar_budget, "openrouter_tariffs", lambda models: {MODEL_ID: prices})
    model.api.reported_cost = Decimal(49)
    second = _request()
    second["max_tokens"] = 1

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})
            with pytest.raises(WorldSpendLedgerError, match="cannot reserve"):
                await model.api._generate_completion(second, {})

    asyncio.run(run())
    assert FakeLedger.instances[0].events[0] == ("reserve", Decimal(49))
    assert FakeLedger.instances[0].spent == Decimal(49)
    assert [event[0] for event in events] == ["reserve", "send"]


def test_target_hook_serializes_concurrent_unresolved_attempts(monkeypatch):
    request = _request()
    model, events, _, _ = _setup(monkeypatch, Decimal(1))

    async def run():
        model.api.send_started = asyncio.Event()
        model.api.send_gate = asyncio.Event()
        async with dollar_budget.target_dollar_budget(model):
            first = asyncio.create_task(model.api._generate_completion(request, {}))
            await model.api.send_started.wait()
            with pytest.raises(WorldSpendLedgerError, match="cannot reserve"):
                await model.api._generate_completion(_request(), {})
            model.api.send_gate.set()
            await first

    asyncio.run(run())
    assert [event[0] for event in events].count("send") == 1
    assert [event[0] for event in events].count("reserve") == 1


def test_target_hook_retains_hold_when_cancelled(monkeypatch):
    request = _request()
    model, _, _, _ = _setup(monkeypatch, Decimal(1))

    async def run():
        model.api.send_started = asyncio.Event()
        model.api.send_gate = asyncio.Event()
        async with dollar_budget.target_dollar_budget(model):
            attempt = asyncio.create_task(model.api._generate_completion(request, {}))
            await model.api.send_started.wait()
            attempt.cancel()
            with pytest.raises(asyncio.CancelledError):
                await attempt

    asyncio.run(run())
    assert FakeLedger.instances[0].hold is True
    assert FakeLedger.instances[0].events[-1][0] == "unknown"


def test_target_hook_bounds_cache_markers_before_provider_send(monkeypatch):
    import inspect_ai.model._providers.openrouter as inspect_openrouter

    request = _request()
    model, events, _, _ = _setup(monkeypatch, Decimal(1))
    model.api.cache_prompt = True

    def add_cache_marker(value):
        value["messages"][0]["content"] = [{
            "type": "text", "text": "hello", "cache_control": {"type": "ephemeral", "ttl": "1h"},
        }]

    monkeypatch.setattr(inspect_openrouter, "_add_anthropic_cache_markers", add_cache_marker)
    expected = _request()
    add_cache_marker(expected)
    expected_bound = _bound(expected)

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    asyncio.run(run())
    assert FakeLedger.instances[0].events[0] == ("reserve", expected_bound)
    assert events[1][1]["messages"][0]["content"][0]["cache_control"] == {
        "type": "ephemeral", "ttl": "1h",
    }


def test_target_hook_rejects_over_budget_and_unbounded_requests_before_send(monkeypatch):
    request = _request()
    bound = _bound(request)
    model, events, _, _ = _setup(monkeypatch, bound - Decimal("0.000001"))

    async def too_expensive():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    with pytest.raises(WorldSpendLedgerError, match="no affordable"):
        asyncio.run(too_expensive())
    assert events == []
    assert FakeLedger.instances[0].events == []

    model, events, _, _ = _setup(monkeypatch, bound)
    request = _request() | {"modalities": ["audio"]}

    async def unsupported():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    with pytest.raises(WorldSpendLedgerError, match="single non-streaming route"):
        asyncio.run(unsupported())
    assert events == []
    assert FakeLedger.instances[0].events == []


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer override"},
        {"x-openrouter-provider": "untrusted-route"},
        {"HTTP-Referer": "https://untrusted.example.test"},
    ],
)
def test_target_hook_rejects_auth_and_route_headers_before_reservation(monkeypatch, headers):
    model, events, _, _ = _setup(monkeypatch, Decimal(1))
    request = _request() | {"extra_headers": headers}

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    with pytest.raises(WorldSpendLedgerError, match="unsupported HTTP headers"):
        asyncio.run(run())
    assert events == []
    assert FakeLedger.instances[0].events == []


@pytest.mark.parametrize(
    ("update", "error"),
    [
        ({"messages": [{"role": "user", "content": "hello", "audio": {"data": "payload"}}]}, "text-only"),
        ({"messages": [{"role": "user", "content": "hello", "image_url": {"url": "payload"}}]}, "text-only"),
        ({"messages": [{"role": "assistant", "content": "hello", "reasoning_details": [
            {"type": "reasoning.unknown", "data": "opaque"},
        ]}]}, "text-only"),
        ({"messages": [{"role": "assistant", "content": "hello", "reasoning_details": [
            {"type": "reasoning.encrypted", "data": {"opaque": "value"}},
        ]}]}, "text-only"),
        ({"extra_body": {"max_tokens": 1000}}, "unbounded OpenRouter parameters"),
        ({"extra_query": {"provider": "untrusted-route"}}, "single non-streaming route"),
    ],
)
def test_target_hook_rejects_unbounded_nested_content_and_overrides_before_send(monkeypatch, update, error):
    model, events, _, _ = _setup(monkeypatch, Decimal(1))
    request = _request() | update

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    with pytest.raises(WorldSpendLedgerError, match=error):
        asyncio.run(run())
    assert events == []
    assert FakeLedger.instances[0].events == []


def test_target_hook_retains_hold_after_provider_error(monkeypatch):
    request = _request()
    bound = _bound(request)
    model, _, _, _ = _setup(monkeypatch, bound)

    async def fail(request, config):
        raise OSError("network failure")

    model.api._generate_completion = fail

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            with pytest.raises(OSError, match="network failure"):
                await model.api._generate_completion(request, {})
            with pytest.raises(WorldSpendLedgerError, match="cannot reserve"):
                await model.api._generate_completion(_request(), {})

    asyncio.run(run())
    assert FakeLedger.instances[0].hold is True
    assert FakeLedger.instances[0].events[-1][0] == "unknown"


def test_target_hook_retains_hold_for_ambiguous_zero_charge(monkeypatch):
    request = _request()
    model, events, _, _ = _setup(monkeypatch, Decimal(1))
    model.api.reported_cost = Decimal(0)

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    with pytest.raises(WorldSpendLedgerError, match="charge is missing"):
        asyncio.run(run())
    assert events[1][0] == "send"
    assert FakeLedger.instances[0].hold is True


def test_target_hook_retains_hold_for_nonzero_byok_charge(monkeypatch):
    request = _request()
    model, events, _, _ = _setup(monkeypatch, Decimal(1))
    model.api.reported_cost = Decimal("0.01")
    model.api.reported_is_byok = True

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    with pytest.raises(WorldSpendLedgerError, match="charge is missing"):
        asyncio.run(run())
    assert events[1][0] == "send"
    assert FakeLedger.instances[0].hold is True


@pytest.mark.parametrize("generation_data", [
    {"total_cost": 0.0001, "is_byok": False},
    {"total_cost": 0.0001, "is_byok": False, "finish_reason": "stop",
     "tokens_prompt": 3, "tokens_completion": 2, "cancelled": True},
    {"total_cost": 0.0001, "is_byok": False, "finish_reason": "stop",
     "tokens_prompt": 3, "tokens_completion": 2, "id": "other-completion"},
    {"total_cost": 0.0001, "is_byok": False, "finish_reason": "stop",
     "tokens_prompt": 3, "tokens_completion": 2, "model": "other/model"},
])
def test_target_hook_keeps_hold_for_incomplete_or_mismatched_generation_data(monkeypatch, generation_data):
    request = _request()
    model, events, _, _ = _setup(monkeypatch, Decimal(1))
    model.api.reported_cost = None
    model.api.reported_is_byok = None
    model.api.client.generation_record = {"data": generation_data}

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    with pytest.raises(WorldSpendLedgerError, match="charge is missing"):
        asyncio.run(run())
    assert model.api.client.generation_requests == 1
    assert events[1][0] == "send"
    assert FakeLedger.instances[0].hold is True


def test_target_hook_accepts_complete_matching_non_byok_generation_evidence(monkeypatch):
    request = _request()
    model, events, _, _ = _setup(monkeypatch, Decimal(1))
    model.api.reported_cost = None
    model.api.reported_is_byok = None
    model.api.client.generation_record = {
        "data": {
            "id": "completion-id",
            "model": MODEL_ID,
            "total_cost": 0.0001,
            "is_byok": False,
            "cancelled": False,
            "finish_reason": "stop",
            "tokens_prompt": 3,
            "tokens_completion": 2,
        }
    }

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    asyncio.run(run())
    assert FakeLedger.instances[0].events[-1] == ("settle", Decimal("0.0001"), "reported")
    assert events[1][0] == "send"


def test_target_hook_accepts_zero_only_for_zero_tariffs(monkeypatch):
    request = _request()
    model, events, _, _ = _setup(monkeypatch, Decimal(1))
    model.api.reported_cost = Decimal(0)
    zero_prices = dict.fromkeys(PRICES, 0.0)
    monkeypatch.setattr(dollar_budget, "openrouter_tariffs", lambda models: {MODEL_ID: zero_prices})

    async def run():
        async with dollar_budget.target_dollar_budget(model):
            await model.api._generate_completion(request, {})

    asyncio.run(run())
    assert FakeLedger.instances[0].events[0] == ("reserve", Decimal(0))
    assert FakeLedger.instances[0].events[-1] == ("settle", Decimal(0), "reported")
    assert events[1][0] == "send"


def test_split_guard_probes_both_distinct_lifetime_keys_offline(monkeypatch):
    monkeypatch.setenv("OPENROUTER_AGENT_API_KEY", "agent-fake")
    monkeypatch.setenv("OPENROUTER_WORLD_API_KEY", "world-fake")
    agent = {"data": {"usage": 0, "limit": 50, "limit_remaining": 50, "limit_reset": None}}
    world = {"data": {"usage": 0, "limit": 100, "limit_remaining": 100, "limit_reset": None}}

    async def agent_probe():
        return agent

    async def world_probe():
        return world

    guard = SplitSpendGuard(
        SplitThresholds(Decimal(10), Decimal(20)),
        agent_probe=agent_probe,
        world_probe=world_probe,
    )

    async def run():
        await guard.start()
        await guard.close()

    asyncio.run(run())
    assert guard._split_baseline["agent"].limit == Decimal(50)
    assert guard._split_baseline["world"].limit == Decimal(100)


def test_split_guard_rejects_resettable_or_wrong_ceiling_key_without_network(monkeypatch):
    monkeypatch.setenv("OPENROUTER_AGENT_API_KEY", "agent-fake")
    monkeypatch.setenv("OPENROUTER_WORLD_API_KEY", "world-fake")
    agent = {"data": {"usage": 0, "limit": 50, "limit_remaining": 50, "limit_reset": 1}}
    world = {"data": {"usage": 0, "limit": 100, "limit_remaining": 100, "limit_reset": None}}

    async def agent_probe():
        return agent

    async def world_probe():
        return world

    guard = SplitSpendGuard(
        SplitThresholds(Decimal(10), Decimal(20)),
        agent_probe=agent_probe,
        world_probe=world_probe,
    )

    with pytest.raises(GuardFailure, match="resettable"):
        asyncio.run(guard.start())


@pytest.mark.parametrize(
    ("agent_value", "world_value", "role"),
    [
        (OpenRouterSnapshot(0, 40, 40), OpenRouterSnapshot(0, 100, 100), "agent"),
        (OpenRouterSnapshot(0, 50, 50), OpenRouterSnapshot(0, 90, 90), "world"),
        (OpenRouterSnapshot(0, 50, 5), OpenRouterSnapshot(0, 100, 100), "agent"),
        (OpenRouterSnapshot(0, 50, 50), OpenRouterSnapshot(0, 100, 19), "world"),
    ],
)
def test_split_guard_rejects_wrong_ceilings_and_insufficient_remaining(
    monkeypatch, agent_value, world_value, role
):
    monkeypatch.setenv("OPENROUTER_AGENT_API_KEY", "agent-fake")
    monkeypatch.setenv("OPENROUTER_WORLD_API_KEY", "world-fake")

    async def agent_probe():
        return {"data": {"usage": agent_value.usage, "limit": agent_value.limit,
                         "limit_remaining": agent_value.remaining, "limit_reset": None}}

    async def world_probe():
        return {"data": {"usage": world_value.usage, "limit": world_value.limit,
                         "limit_remaining": world_value.remaining, "limit_reset": None}}

    guard = SplitSpendGuard(
        SplitThresholds(Decimal(10), Decimal(20)),
        agent_probe=agent_probe,
        world_probe=world_probe,
    )

    async def run():
        samples = await guard._read_openrouter()
        with pytest.raises(GuardFailure, match=role):
            guard._accept_sample(samples, initial=True)

    asyncio.run(run())


@pytest.mark.parametrize("missing", [True, False])
def test_split_guard_rejects_missing_or_identical_keys_before_probe(monkeypatch, missing):
    monkeypatch.setenv("OPENROUTER_AGENT_API_KEY", "agent-fake")
    if missing:
        monkeypatch.delenv("OPENROUTER_WORLD_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENROUTER_WORLD_API_KEY", "agent-fake")
    calls = []

    async def probe():
        calls.append(True)
        return {}

    guard = SplitSpendGuard(
        SplitThresholds(Decimal(10), Decimal(20)), agent_probe=probe, world_probe=probe
    )
    with pytest.raises(GuardFailure, match="not independent"):
        asyncio.run(guard.start())
    assert calls == []


def test_split_guard_fails_closed_when_one_key_probe_times_out(monkeypatch):
    monkeypatch.setenv("OPENROUTER_AGENT_API_KEY", "agent-fake")
    monkeypatch.setenv("OPENROUTER_WORLD_API_KEY", "world-fake")

    calls = []

    async def agent_probe():
        calls.append("agent")
        await asyncio.sleep(1)

    async def world_probe():
        calls.append("world")
        return {"data": {"usage": 0, "limit": 100, "limit_remaining": 100, "limit_reset": None}}

    guard = SplitSpendGuard(
        SplitThresholds(Decimal(10), Decimal(20), probe_timeout=0.01),
        agent_probe=agent_probe,
        world_probe=world_probe,
    )
    with pytest.raises(GuardFailure, match="agent probe failed"):
        asyncio.run(guard.start())
    assert sorted(calls) == ["agent", "world"]


@pytest.mark.parametrize(
    ("agent_sample", "world_sample", "role"),
    [
        (OpenRouterSnapshot(10, 50, 40), OpenRouterSnapshot(0, 100, 100), "agent"),
        (OpenRouterSnapshot(0, 50, 50), OpenRouterSnapshot(20, 100, 80), "world"),
    ],
)
def test_split_guard_monitors_agent_and_world_exhaustion_independently(
    agent_sample, world_sample, role
):
    guard = SplitSpendGuard(SplitThresholds(Decimal(10), Decimal(20)))
    guard._accept_sample(
        {"agent": OpenRouterSnapshot(0, 50, 50), "world": OpenRouterSnapshot(0, 100, 100)},
        initial=True,
    )
    with pytest.raises(GuardFailure, match=f"OpenRouter {role} trial threshold reached"):
        guard._accept_sample({"agent": agent_sample, "world": world_sample})


def test_split_wheel_attestation_requires_exact_budget_sources(tmp_path):
    wheel = _write_public_agent_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        assert "refund_eval/harbor/installed_agent.py" not in archive.namelist()
        assert set(SPLIT_BUDGET_WHEEL_SOURCES).issubset(archive.namelist())
    hashes = verify_split_budget_wheel(wheel)
    assert set(hashes) == set(SPLIT_BUDGET_WHEEL_SOURCES)

    stale = _write_public_agent_wheel(tmp_path / "stale", stale="refund_eval/agent/world_mode.py")
    with pytest.raises(ValueError, match="current approved wheel source"):
        verify_split_budget_wheel(stale)

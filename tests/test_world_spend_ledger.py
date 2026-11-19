from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from decimal import Decimal

import httpx
import pytest

from refund_eval.companies import CompanyAction
from refund_eval.models.budget import (
    WORLD_BUDGET_KEY,
    BudgetedWorldModels,
    charge_bound,
)
from refund_eval.models.client import (
    Completion,
    Message,
    ModelLimitExceeded,
    OutputSchema,
    Usage,
)
from refund_eval.models.world_spend_ledger import (
    LEDGER_FILE,
    LEDGER_VERSION,
    WorldSpendLedger,
    WorldSpendLedgerError,
)
from refund_eval.world import store as trial_store
from refund_eval.world.store import world_store
from refund_eval.world_service.store import MemoryTrial

SCOPE = {"scenario": "train_refund_late",
         "models": {"simulator": "fake/simulator", "grader": "fake/grader"}}
TARIFF = {"input": 1, "output": 5, "input_cache_read": 1, "input_cache_write": 1,
          "input_cache_write_1h": 1, "internal_reasoning": 0, "request": 0}
PRICES = {"fake/simulator": TARIFF, "fake/grader": TARIFF}
MESSAGES = [Message("user", "hello")]
SETTINGS = {"max_tokens": 10}


@pytest.fixture
def installed_world_store():
    trial = MemoryTrial("train_refund_late")
    trial_store.install(trial)
    try:
        yield trial
    finally:
        trial_store.install(None)


def completion(*, cost: float | None, method: str | None = "actual", failed: bool = False,
               worst_case: float | None = None) -> Completion:
    return Completion(text="reply", message=Message("assistant", "reply"),
                      usage=None if cost is None else Usage(cost=cost), cost_method=method,
                      failed=failed, worst_case_cost=worst_case)


class ScriptedModels:
    def __init__(self, *replies, worst_case: float | None = None) -> None:
        self.replies = list(replies)
        self.worst_case = worst_case
        self.calls = 0
        self.observed = []
        self.entered = None
        self.release = None

    def model_name(self, role: str) -> str:
        return f"fake/{role}"

    def worst_case_cost(self, role, messages, **settings):
        return self.worst_case

    async def complete(self, role, messages, **settings):
        self.calls += 1
        self.observed.append(dict(world_store().get(WORLD_BUDGET_KEY)))
        if self.entered is not None:
            self.entered.set()
            await self.release.wait()
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


def make_budget(client, limit, *, ledger=None, prices=PRICES, persist=None):
    return BudgetedWorldModels(client, limit, persist or (lambda: None), prices=prices, ledger=ledger, scope=SCOPE)


def request_bound() -> Decimal:
    bound = charge_bound(MESSAGES, SETTINGS, TARIFF)
    assert bound is not None
    return bound


def test_charge_bound_includes_schema_name_and_description():
    short_schema = OutputSchema("action", CompanyAction, "short")
    long_schema = OutputSchema("name" * 1000, CompanyAction, "description" * 1000)
    short_bound = charge_bound(MESSAGES, {**SETTINGS, "schema": short_schema}, TARIFF)
    long_bound = charge_bound(MESSAGES, {**SETTINGS, "schema": long_schema}, TARIFF)

    assert short_bound is not None and long_bound is not None and long_bound > short_bound


async def test_estimate_exact_fit_returns_success_and_blocks_the_next_request(installed_world_store):
    bound = request_bound()
    client = ScriptedModels(completion(cost=float(bound) / 2, method="estimate"))
    budget = make_budget(client, bound)

    result = await budget.complete("simulator", MESSAGES, **SETTINGS)

    assert result.text == "reply" and client.calls == 1
    assert budget.status == "exhausted"
    snapshot = world_store().get(WORLD_BUDGET_KEY)
    assert snapshot["spent_usd"] == float(bound)
    assert snapshot["reserved_usd"] == 0
    assert snapshot["committed_usd"] == float(bound)
    with pytest.raises(ModelLimitExceeded, match="reached"):
        await budget.complete("simulator", MESSAGES, **SETTINGS)
    assert client.calls == 1


@pytest.mark.parametrize("case", ["unpriced", "unaffordable"])
async def test_unpriced_or_unaffordable_request_is_refused_before_dispatch(installed_world_store, case):
    bound = request_bound()
    client = ScriptedModels(completion(cost=0.001))
    limit = 1 if case == "unpriced" else bound / 2
    prices = {} if case == "unpriced" else PRICES
    budget = make_budget(client, limit, prices=prices)

    with pytest.raises(ModelLimitExceeded):
        await budget.complete("simulator", MESSAGES, **SETTINGS)
    assert client.calls == 0
    snapshot = world_store().get(WORLD_BUDGET_KEY)
    assert snapshot["reserved_usd"] == 0 and snapshot["committed_usd"] == snapshot["spent_usd"]


async def test_timeout_attempt_is_accounted_before_a_fresh_retry_reservation(installed_world_store):
    bound = request_bound()
    client = ScriptedModels(TimeoutError("provider timed out"),
                            completion(cost=float(bound) / 2), worst_case=float(bound) * 2)
    budget = make_budget(client, bound * 4)

    result = await budget.complete("simulator", MESSAGES, **SETTINGS)

    assert result.text == "reply" and client.calls == 2
    assert all(state["reserved_usd"] == float(bound) for state in client.observed)
    assert all(state["committed_usd"] == state["spent_usd"] + state["reserved_usd"]
               for state in client.observed)
    snapshot = budget.ledger.snapshot()
    assert Decimal(snapshot["spent_usd"]) == bound * 2 + Decimal(str(float(bound) / 2))
    assert snapshot["reserved_usd"] == "0" and snapshot["unresolved_attempts"] == 0


async def test_returned_failed_attempt_uses_worst_case_and_is_charged(installed_world_store):
    bound = request_bound()
    client = ScriptedModels(completion(cost=0.000001, failed=True, worst_case=float(bound) * 2),
                            completion(cost=float(bound) / 2))
    budget = make_budget(client, bound * 4)

    await budget.complete("simulator", MESSAGES, **SETTINGS)

    assert client.calls == 2
    assert Decimal(budget.ledger.snapshot()["spent_usd"]) == bound * 2 + Decimal(str(float(bound) / 2))


async def test_failed_request_without_a_reported_charge_settles_the_bound_then_retries(installed_world_store):
    bound = request_bound()
    client = ScriptedModels(TimeoutError("request failed"), completion(cost=float(bound) / 2))
    budget = make_budget(client, bound * 3)

    await budget.complete("simulator", MESSAGES, **SETTINGS)

    assert client.calls == 2
    assert Decimal(budget.ledger.snapshot()["spent_usd"]) == bound + Decimal(str(float(bound) / 2))


async def test_three_failed_attempts_block_another_call_on_the_same_wrapper(installed_world_store):
    bound = request_bound()
    client = ScriptedModels(
        TimeoutError("first failure"), TimeoutError("second failure"), TimeoutError("third failure"),
        completion(cost=float(bound)),
    )
    budget = make_budget(client, bound * 5)

    with pytest.raises(ModelLimitExceeded, match="failed after three attempts"):
        await budget.complete("simulator", MESSAGES, **SETTINGS)
    assert client.calls == 3

    with pytest.raises(ModelLimitExceeded, match="is failed"):
        await budget.complete("simulator", MESSAGES, **SETTINGS)
    assert client.calls == 3


async def test_missing_billing_keeps_hold_and_does_not_retry(installed_world_store, tmp_path):
    bound = request_bound()
    ledger = WorldSpendLedger(tmp_path, bound * 4, SCOPE)
    client = ScriptedModels(completion(cost=None, method=None))
    budget = make_budget(client, bound * 4, ledger=ledger)

    with pytest.raises(ModelLimitExceeded, match="valid charge"):
        await budget.complete("simulator", MESSAGES, **SETTINGS)

    assert client.calls == 1
    snapshot = ledger.snapshot()
    assert snapshot["reserved_usd"] == str(bound) and snapshot["unresolved_attempts"] == 1


async def test_failed_response_without_billing_keeps_hold_and_does_not_retry(installed_world_store, tmp_path):
    bound = request_bound()
    ledger = WorldSpendLedger(tmp_path, bound * 4, SCOPE)
    client = ScriptedModels(completion(cost=None, method=None, failed=True))
    budget = make_budget(client, bound * 4, ledger=ledger)

    with pytest.raises(ModelLimitExceeded, match="no defensible charge"):
        await budget.complete("simulator", MESSAGES, **SETTINGS)

    assert client.calls == 1
    assert ledger.snapshot()["reserved_usd"] == str(bound)
    assert ledger.snapshot()["unresolved_attempts"] == 1


async def test_cancellation_persists_hold_and_new_wrapper_refuses_without_double_dispatch(
        installed_world_store, tmp_path):
    bound = request_bound()
    ledger = WorldSpendLedger(tmp_path, bound * 4, SCOPE)
    client = ScriptedModels()
    client.entered, client.release = asyncio.Event(), asyncio.Event()
    budget = make_budget(client, bound * 4, ledger=ledger)
    task = asyncio.create_task(budget.complete("simulator", MESSAGES, **SETTINGS))
    await client.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    before = ledger.snapshot()
    assert before["reserved_usd"] == str(bound) and before["unresolved_attempts"] == 1
    resumed_ledger = WorldSpendLedger(tmp_path, bound * 4, SCOPE)
    resumed_client = ScriptedModels(completion(cost=0.001))
    resumed = make_budget(resumed_client, bound * 4, ledger=resumed_ledger)
    with pytest.raises(ModelLimitExceeded, match="earlier reservation"):
        await resumed.complete("simulator", MESSAGES, **SETTINGS)
    assert resumed_client.calls == 0
    assert resumed_ledger.snapshot() == before


def test_settlement_survives_restart_and_is_idempotent(tmp_path):
    bound = request_bound()
    ledger = WorldSpendLedger(tmp_path, 1, SCOPE)
    attempt = str(uuid.uuid4())
    request_hash = hashlib.sha256(b"request").hexdigest()
    ledger.reserve(attempt, request_hash, bound)
    settled = ledger.settle(attempt, "0.25", "actual")
    assert settled["spent_usd"] == "0.25" and settled["reserved_usd"] == "0"
    assert settled["committed_usd"] == "0.25"
    state = json.loads((tmp_path / LEDGER_FILE).read_text(encoding="utf-8"))
    assert state["version"] == LEDGER_VERSION and state["scope"] == SCOPE
    assert state["attempts"][attempt] == {
        "request_hash": request_hash, "reserved_usd": str(bound), "status": "settled",
        "charged_usd": "0.25", "provenance": "actual",
    }

    restarted = WorldSpendLedger(tmp_path, 1, SCOPE)
    assert restarted.snapshot() == settled
    assert restarted.settle(attempt, "0.25", "actual") == settled
    assert restarted.snapshot()["reserved_usd"] == "0"
    assert restarted.snapshot()["committed_usd"] == "0.25"
    with pytest.raises(WorldSpendLedgerError, match="Conflicting settlement"):
        restarted.settle(attempt, "0.30", "actual")
    assert restarted.snapshot() == settled


def test_settlement_cannot_conflict_with_a_known_charge_in_an_unknown_attempt(tmp_path):
    ledger = WorldSpendLedger(tmp_path, 1, SCOPE)
    attempt = str(uuid.uuid4())
    ledger.reserve(attempt, hashlib.sha256(b"request").hexdigest(), "0.1")
    ledger.mark_unknown(attempt, "0.2", "reservation_overrun")

    with pytest.raises(WorldSpendLedgerError, match="Conflicting settlement"):
        ledger.settle(attempt, "0.3", "actual")
    assert ledger.snapshot()["spent_usd"] == "0.2"
    assert ledger.snapshot()["unresolved_attempts"] == 1


def test_reservation_overrun_is_not_double_counted_after_restart(tmp_path):
    ledger = WorldSpendLedger(tmp_path, 1, SCOPE)
    attempt = str(uuid.uuid4())
    ledger.reserve(attempt, hashlib.sha256(b"request").hexdigest(), "0.1")
    ledger.mark_unknown(attempt, "0.2", "reservation_overrun")

    restarted = WorldSpendLedger(tmp_path, 1, SCOPE)
    held = restarted.snapshot()
    assert held["spent_usd"] == "0.2"
    assert held["reserved_usd"] == "0"
    assert held["committed_usd"] == "0.2"
    assert held["unresolved_attempts"] == 1

    settled = restarted.settle(attempt, "0.2", "reservation_overrun")
    assert settled["reserved_usd"] == "0" and settled["committed_usd"] == "0.2"
    assert settled["unresolved_attempts"] == 0
    assert restarted.settle(attempt, "0.2", "reservation_overrun") == settled
    assert restarted.snapshot() == settled


async def test_distinct_wrappers_cannot_share_a_hold_or_overspend(installed_world_store, tmp_path):
    bound = request_bound()
    limit = bound
    first_ledger = WorldSpendLedger(tmp_path, limit, SCOPE)
    second_ledger = WorldSpendLedger(tmp_path, limit, SCOPE)
    first_client = ScriptedModels(completion(cost=float(bound)))
    first_client.entered, first_client.release = asyncio.Event(), asyncio.Event()
    second_client = ScriptedModels(completion(cost=float(bound)))
    first = make_budget(first_client, limit, ledger=first_ledger)
    second = make_budget(second_client, limit, ledger=second_ledger)

    task = asyncio.create_task(first.complete("simulator", MESSAGES, **SETTINGS))
    await first_client.entered.wait()
    with pytest.raises(ModelLimitExceeded, match="earlier reservation"):
        await second.complete("simulator", MESSAGES, **SETTINGS)
    first_client.release.set()
    assert (await task).text == "reply"
    with pytest.raises(ModelLimitExceeded, match="reached"):
        await second.complete("simulator", MESSAGES, **SETTINGS)
    assert first_client.calls == 1 and second_client.calls == 0
    assert Decimal(second_ledger.snapshot()["committed_usd"]) == bound


def test_malformed_mismatched_and_historical_outputs_fail_without_reset(tmp_path):
    path = tmp_path / "world-spend-ledger.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(WorldSpendLedgerError, match="Cannot read"):
        WorldSpendLedger(tmp_path, 1, SCOPE)
    assert path.read_text(encoding="utf-8") == "{broken"

    path.unlink()
    ledger = WorldSpendLedger(tmp_path, 1, SCOPE)
    with pytest.raises(WorldSpendLedgerError, match="limit mismatch"):
        WorldSpendLedger(tmp_path, 2, SCOPE)
    with pytest.raises(WorldSpendLedgerError, match="scope mismatch"):
        WorldSpendLedger(tmp_path, 1, {"scenario": "other", "models": SCOPE["models"]})
    assert ledger.snapshot()["spent_usd"] == "0"

    another = tmp_path / "historical"
    another.mkdir()
    old_state = another / "state.json"
    old_state.write_text('{"store": {"world_model_budget": {"spent_usd": 0.5}}}', encoding="utf-8")
    with pytest.raises(WorldSpendLedgerError, match="Restore the ledger"):
        WorldSpendLedger(another, 1, SCOPE)
    assert old_state.exists() and not (another / "world-spend-ledger.json").exists()


async def test_persist_failure_before_dispatch_keeps_the_reservation(installed_world_store, tmp_path):
    bound = request_bound()
    ledger = WorldSpendLedger(tmp_path, bound * 4, SCOPE)
    client = ScriptedModels(completion(cost=0.001))
    calls = 0

    def persist():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("snapshot disk failure")

    budget = make_budget(client, bound * 4, ledger=ledger, persist=persist)
    with pytest.raises(OSError, match="snapshot disk failure"):
        await budget.complete("simulator", MESSAGES, **SETTINGS)
    assert client.calls == 0
    assert ledger.snapshot()["reserved_usd"] == str(bound)
    assert ledger.snapshot()["unresolved_attempts"] == 1


async def test_ledger_write_failure_prevents_dispatch(installed_world_store, tmp_path, monkeypatch):
    from refund_eval.models import world_spend_ledger as ledger_module

    bound = request_bound()
    ledger = WorldSpendLedger(tmp_path, bound * 4, SCOPE)
    client = ScriptedModels(completion(cost=0.001))
    budget = make_budget(client, bound * 4, ledger=ledger)

    def fail_replace(source, destination):
        raise OSError("ledger disk failure")

    monkeypatch.setattr(ledger_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="ledger disk failure"):
        await budget.complete("simulator", MESSAGES, **SETTINGS)
    assert client.calls == 0
    snapshot = ledger.snapshot()
    assert snapshot["spent_usd"] == "0" and snapshot["reserved_usd"] == "0"


async def test_settlement_write_failure_keeps_a_durable_hold(installed_world_store, tmp_path):
    bound = request_bound()
    ledger = WorldSpendLedger(tmp_path, bound * 4, SCOPE)
    client = ScriptedModels(completion(cost=float(bound)))
    budget = make_budget(client, bound * 4, ledger=ledger)
    write = ledger._write_state
    writes = 0

    def fail_final_settlement(state):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("settlement disk failure")
        write(state)

    ledger._write_state = fail_final_settlement
    with pytest.raises(WorldSpendLedgerError, match="reservation remains held"):
        await budget.complete("simulator", MESSAGES, **SETTINGS)
    ledger._write_state = write
    assert client.calls == 1
    resumed = WorldSpendLedger(tmp_path, bound * 4, SCOPE)
    snapshot = resumed.snapshot()
    assert snapshot["reserved_usd"] == str(bound) and snapshot["unresolved_attempts"] == 1


def test_tariff_refuses_unknown_and_nonzero_image_prices_but_allows_web_search():
    from refund_eval.model_pricing import catalog_tariff

    pricing = {"prompt": "0.000001", "completion": "0.000005", "web_search": "0.01"}
    assert catalog_tariff({"id": "anthropic/claude", "pricing": pricing})["request"] == 0
    for extra in ({"image": "0.001"}, {"unknown_component": "0.001"}):
        with pytest.raises(ValueError):
            catalog_tariff({"pricing": pricing | extra})
    with pytest.raises(ValueError, match="searches on every request"):
        catalog_tariff({"id": "perplexity/sonar", "pricing": pricing})


def test_openrouter_tariffs_use_the_worst_catalog_and_endpoint_rates(monkeypatch):
    from refund_eval import model_pricing

    catalog_pricing = {"prompt": "0.000001", "completion": "0.000005", "request": "0.000001",
                       "web_search": "0.01"}
    endpoint_pricing = {"prompt": "0.000002", "completion": "0.000010", "input_cache_read": "0.000001",
                        "input_cache_write": "0.000003", "request": "0.000004"}
    responses = []

    def get(url, timeout):
        responses.append(url)
        body = ({"data": [{"id": "provider/sim", "pricing": catalog_pricing}]}
                if url == model_pricing.CATALOG_URL else
                {"data": {"endpoints": [{"pricing": endpoint_pricing}]}})
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(model_pricing.httpx, "get", get)
    tariffs = model_pricing.openrouter_tariffs(["openrouter/provider/sim"])

    assert tariffs["openrouter/provider/sim"] == {
        "input": 2, "output": 10, "input_cache_read": 1, "input_cache_write": 3,
        "input_cache_write_1h": 2, "internal_reasoning": 0, "request": 0.000004,
    }
    assert len(responses) == 2 and responses[1].endswith("/provider/sim/endpoints")

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest

from refund_eval.models.shared_role_spend_ledger import (
    SharedRoleBudgetConfig,
    SharedRoleBudgetExhausted,
    SharedRoleSpendLedger,
    SharedRoleSpendLedgerError,
)
from scripts.offline_spend_guard import (
    GuardFailure,
    OpenRouterSnapshot,
    SharedRoleSpendGuard,
    SharedRoleThresholds,
)


def _config(*, allowance="1.5", target="1", world="1.5"):
    return SharedRoleBudgetConfig(
        allowance_usd=allowance,
        role_limits_usd={"target": target, "world": world},
        scenario="train_refund_late",
        models={"target": "openrouter/target", "simulator": "openrouter/sim", "grader": "openrouter/grade"},
    )


def _reserve(ledger, role, amount):
    return ledger.reserve(role, str(uuid4()), "a" * 64, amount)


def test_cross_role_concurrency_never_double_spends_shared_allowance(tmp_path):
    first = SharedRoleSpendLedger(tmp_path, _config(allowance="1", target="1", world="1"))
    second = SharedRoleSpendLedger(tmp_path, first.config)

    def reserve(ledger, role):
        try:
            _reserve(ledger, role, "0.75")
            return True
        except SharedRoleSpendLedgerError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: reserve(*args), ((first, "target"), (second, "world"))))
    assert sum(results) == 1
    snapshot = first.snapshot()
    assert Decimal(snapshot["aggregate_reserved_usd"]) == Decimal("0.75")
    assert Decimal(snapshot["aggregate_spent_usd"]) + Decimal(snapshot["aggregate_reserved_usd"]) <= Decimal(1)


def test_target_and_world_caps_and_lower_aggregate_allowance(tmp_path):
    ledger = SharedRoleSpendLedger(tmp_path, _config())
    attempt = str(uuid4())
    ledger.reserve("target", attempt, "a" * 64, "0.75")
    ledger.settle(attempt, "0.75", "usage")
    with pytest.raises(SharedRoleBudgetExhausted, match="target cap"):
        _reserve(ledger, "target", "0.26")

    attempt = str(uuid4())
    ledger.reserve("world", attempt, "b" * 64, "0.75")
    ledger.settle(attempt, "0.75", "usage")
    with pytest.raises(SharedRoleBudgetExhausted, match="aggregate allowance"):
        _reserve(ledger, "target", "0.01")

    world_ledger = SharedRoleSpendLedger(tmp_path / "world-cap", _config(allowance="2.5", target="1.5", world="1"))
    attempt = str(uuid4())
    world_ledger.reserve("world", attempt, "d" * 64, "1")
    world_ledger.settle(attempt, "1", "usage")
    with pytest.raises(SharedRoleBudgetExhausted, match="world cap"):
        _reserve(world_ledger, "world", "0.01")


def test_unaffordable_shared_allowance_prevents_dispatch(tmp_path):
    ledger = SharedRoleSpendLedger(tmp_path, _config(allowance="0.5", target="1", world="1"))
    attempt = str(uuid4())
    ledger.reserve("target", attempt, "a" * 64, "0.4")
    ledger.settle(attempt, "0.4", "usage")
    dispatches = []
    with pytest.raises(SharedRoleBudgetExhausted, match="aggregate allowance"):
        ledger.view("world").reserve(str(uuid4()), "b" * 64, "0.2")
        dispatches.append("provider-call")
    assert dispatches == []


def test_unknown_and_overrun_holds_survive_reopening(tmp_path):
    config = _config(allowance="1", target="1", world="1")
    ledger = SharedRoleSpendLedger(tmp_path / "unknown", config)
    unknown = str(uuid4())
    ledger.reserve("target", unknown, "a" * 64, "0.4")
    ledger.mark_unknown(unknown)
    reopened = SharedRoleSpendLedger(tmp_path / "unknown", config)
    assert reopened.has_unresolved()
    assert Decimal(reopened.snapshot()["aggregate_reserved_usd"]) == Decimal("0.4")
    with pytest.raises(SharedRoleSpendLedgerError, match="unresolved hold"):
        _reserve(reopened, "world", "0.1")

    overrun_ledger = SharedRoleSpendLedger(tmp_path / "overrun", config)
    overrun = str(uuid4())
    overrun_ledger.reserve("world", overrun, "c" * 64, "0.2")
    with pytest.raises(SharedRoleSpendLedgerError, match="exceeded its reservation"):
        overrun_ledger.settle(overrun, "0.3", "usage")
    reopened_overrun = SharedRoleSpendLedger(tmp_path / "overrun", config)
    assert reopened_overrun.has_unresolved()
    assert Decimal(reopened_overrun.snapshot()["aggregate_reserved_usd"]) == Decimal("0.3")


def test_failed_settlement_finalization_restores_hold_after_reopen(tmp_path, monkeypatch):
    config = _config(allowance="1", target="1", world="1")
    ledger = SharedRoleSpendLedger(tmp_path, config)
    attempt = str(uuid4())
    ledger.reserve("target", attempt, "a" * 64, "0.4")
    write_state = ledger._write_state
    writes = 0

    def fail_after_replace(state):
        nonlocal writes
        writes += 1
        write_state(state)
        if writes == 2:
            raise OSError("directory fsync failed after replace")

    monkeypatch.setattr(ledger, "_write_state", fail_after_replace)
    with pytest.raises(SharedRoleSpendLedgerError, match="reservation remains held"):
        ledger.settle(attempt, "0.3", "usage")

    reopened = SharedRoleSpendLedger(tmp_path, config)
    assert reopened.has_unresolved()
    assert Decimal(reopened.snapshot()["aggregate_reserved_usd"]) == Decimal("0.4")


def test_scope_mismatch_and_corrupt_history_fail_closed(tmp_path):
    config = _config()
    ledger = SharedRoleSpendLedger(tmp_path, config)
    with pytest.raises(SharedRoleSpendLedgerError, match="scope or caps changed"):
        SharedRoleSpendLedger(tmp_path, _config(allowance="1.4"))
    ledger.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(SharedRoleSpendLedgerError, match="Cannot read"):
        SharedRoleSpendLedger(tmp_path, config)


@pytest.mark.parametrize(
    ("status", "reserved", "charged", "provenance", "error"),
    [
        ("reserved", 0.1, None, None, "serialized reservation"),
        ("reserved", "1.1", None, None, "exceeds its configured cap"),
        ([], "0.1", None, None, "attempt status"),
        ("settled", "0.1", "0.1", 7, "settlement provenance"),
        ("settled", "0.1", "0.2", "usage", "exceeds its reservation"),
        ("settlement_pending", "0.1", "0.2", "usage", "exceeds its reservation"),
    ],
)
def test_malformed_historical_ledger_values_fail_closed(tmp_path, status, reserved, charged, provenance, error):
    path = tmp_path / "malformed"
    config = _config()
    ledger = SharedRoleSpendLedger(path, config)
    attempt = str(uuid4())
    ledger.reserve("target", attempt, "a" * 64, "0.1")
    state = json.loads(ledger.path.read_text(encoding="utf-8"))
    state["attempts"][attempt].update(
        status=status, reserved_usd=reserved, charged_usd=charged, provenance=provenance
    )
    ledger.path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(SharedRoleSpendLedgerError, match=error):
        SharedRoleSpendLedger(path, config)


def test_legacy_openrouter_snapshot_ignores_new_metadata_fields():
    snapshot = OpenRouterSnapshot.parse({
        "data": {"usage": 1, "limit": 10, "limit_remaining": 9, "budgets": "legacy-ignored"}
    })
    assert (snapshot.usage, snapshot.limit, snapshot.remaining) == (Decimal(1), Decimal(10), Decimal(9))


def _key_metadata(*, usage=0, limit=80, remaining=80, reset=None, budget_remaining=None):
    data = {
        "usage": usage,
        "limit": limit,
        "limit_remaining": remaining,
        "limit_reset": reset,
        "is_management_key": False,
        "is_provisioning_key": False,
        "include_byok_in_limit": True,
    }
    if budget_remaining is not None:
        data["budgets"] = [{
            "scope": "key", "limit_usd": 100, "spend_usd": 100 - budget_remaining,
            "remaining_usd": budget_remaining, "reset_interval": None, "resets_at": None,
            "include_byok": True,
        }]
    return {"data": data}


def test_shared_role_guard_accepts_non_50_key_and_records_sanitized_baseline():
    calls = []

    async def probe():
        calls.append(True)
        return _key_metadata(usage=5, limit=80, remaining=75, budget_remaining=70)

    guard = SharedRoleSpendGuard(
        SharedRoleThresholds(Decimal(50), probe_timeout=0.1, poll_interval=0.1, max_sample_age=1),
        openrouter_probe=probe,
    )

    async def run():
        await guard.start()
        record = guard.sanitized_effective_parameters()
        await guard.close()
        return record

    record = asyncio.run(run())
    assert len(calls) >= 1
    assert record == {
        "shared_allowance_usd": "50", "shared_allowance_source": "operator_attested_allocation",
        "key_limit_usd": "80", "key_remaining_usd": "75", "key_usage_baseline_usd": "5",
        "budget_remaining_usd": ["70"], "effective_budget_remaining_usd": None,
        "runtime_metadata_confirmed": True,
    }


def test_shared_role_guard_rejects_reset_or_insufficient_headroom():
    async def run(payload):
        async def probe():
            return payload

        guard = SharedRoleSpendGuard(
            SharedRoleThresholds(Decimal(10), probe_timeout=0.1, poll_interval=0.1, max_sample_age=1),
            openrouter_probe=probe,
        )
        with pytest.raises(GuardFailure):
            await guard.start()
        with pytest.raises(GuardFailure):
            await guard.close()

    asyncio.run(run(_key_metadata(remaining=9)))
    asyncio.run(run(_key_metadata(reset="daily")))
    asyncio.run(run(_key_metadata(budget_remaining=9)))


def test_exact_headroom_allows_measured_normal_spend_without_rebasing():
    payload = _key_metadata(usage=0, limit=80, remaining=10, budget_remaining=10)

    async def probe():
        return payload

    guard = SharedRoleSpendGuard(
        SharedRoleThresholds(Decimal(10), probe_timeout=0.1, poll_interval=0.1, max_sample_age=1),
        openrouter_probe=probe,
    )

    async def run():
        await guard.start()
        baseline = guard._baseline
        current = type(baseline).parse(_key_metadata(usage=1, limit=80, remaining=9, budget_remaining=9))
        guard._accept_sample(current)
        await guard.close()

    asyncio.run(run())

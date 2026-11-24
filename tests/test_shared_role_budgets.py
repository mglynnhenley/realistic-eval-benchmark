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
    SharedCapsSpendGuard,
    SharedCapsThresholds,
    SharedRoleOpenRouterSnapshot,
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


def _key_metadata(
    *, usage=0, limit=80, remaining=80, reset=None, budget_remaining=None, budget_limit=100,
    budget_scope="key_limit",
):
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
            "scope": budget_scope, "limit_usd": budget_limit, "spend_usd": budget_limit - budget_remaining,
            "remaining_usd": budget_remaining, "reset_interval": None, "resets_at": None,
            "include_byok": True,
        }]
    return {"data": data}


def _shared_caps_key_metadata(**kwargs):
    payload = _key_metadata(**kwargs)
    payload["data"].setdefault("budgets", [])
    payload["data"].setdefault("effective_budget", None)
    return payload


def test_shared_role_snapshot_accepts_empty_budgets_and_explicit_null_effective_budget():
    payload = _key_metadata()
    payload["data"].update(budgets=[], effective_budget=None)

    snapshot = SharedRoleOpenRouterSnapshot.parse(payload)

    assert snapshot.budgets == ()
    assert snapshot.budgets_present
    assert snapshot.effective_budget is None
    assert snapshot.effective_budget_present


@pytest.mark.parametrize("invalid_field", ["missing_reset_interval", "missing_resets_at", "unknown_scope"])
def test_shared_role_guard_rejects_incomplete_or_unknown_budget_metadata(invalid_field):
    payload = _key_metadata(budget_remaining=70)
    budget = payload["data"]["budgets"][0]
    if invalid_field == "missing_reset_interval":
        del budget["reset_interval"]
    elif invalid_field == "missing_resets_at":
        del budget["resets_at"]
    else:
        budget["scope"] = "key"

    async def probe():
        return payload

    guard = SharedRoleSpendGuard(
        SharedRoleThresholds(Decimal(50), probe_timeout=0.1, poll_interval=0.1, max_sample_age=1),
        openrouter_probe=probe,
    )

    async def run():
        with pytest.raises(GuardFailure, match="monitor data malformed"):
            await guard.start()
        with pytest.raises(GuardFailure):
            await guard.close()

    asyncio.run(run())


def test_shared_role_guard_rejects_effective_budget_null_to_object_change():
    initial_payload = _key_metadata()
    initial_payload["data"].update(budgets=[], effective_budget=None)
    changed_payload = _key_metadata()
    changed_payload["data"].update(
        budgets=[],
        effective_budget={
            "scope": "key_limit", "limit_usd": 100, "spend_usd": 0, "remaining_usd": 100,
            "reset_interval": None, "resets_at": None, "include_byok": True,
        },
    )
    guard = SharedRoleSpendGuard(SharedRoleThresholds(Decimal(50)))
    guard._accept_sample(SharedRoleOpenRouterSnapshot.parse(initial_payload), initial=True)

    with pytest.raises(GuardFailure, match="budget metadata changed"):
        guard._accept_sample(SharedRoleOpenRouterSnapshot.parse(changed_payload))


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


def test_shared_role_normalizes_lagging_key_usage_before_workspace_headroom_check():
    def sample(*, usage, remaining, budget_remaining):
        return SharedRoleOpenRouterSnapshot.parse(_key_metadata(
            usage=usage, limit=50, remaining=remaining,
            budget_remaining=budget_remaining, budget_limit=100, budget_scope="workspace",
        ))

    guard = SharedRoleSpendGuard(SharedRoleThresholds(Decimal(10)))
    guard._accept_sample(sample(usage=40, remaining=10, budget_remaining=10), initial=True)
    guard._accept_sample(sample(usage=45, remaining=5, budget_remaining=5))
    guard._accept_sample(sample(usage=42, remaining=8, budget_remaining=5))
    assert guard._previous_openrouter.usage == Decimal(45)
    assert guard._previous_openrouter.budgets[0].spend == Decimal(95)
    assert guard._previous_openrouter.budgets[0].remaining == Decimal(5)

    with pytest.raises(GuardFailure, match="budget remaining is below"):
        guard._accept_sample(sample(usage=46, remaining=4, budget_remaining=3))


@pytest.mark.parametrize("budget_field", ["budgets", "effective_budget"])
def test_shared_role_guard_does_not_count_other_key_workspace_spend(budget_field):
    def sample(remaining):
        payload = _key_metadata()
        record = {
            "scope": "workspace", "limit_usd": 2000, "spend_usd": 2000 - remaining,
            "remaining_usd": remaining, "reset_interval": None, "resets_at": None,
            "include_byok": True,
        }
        payload["data"][budget_field] = [record] if budget_field == "budgets" else record
        return SharedRoleOpenRouterSnapshot.parse(payload)

    guard = SharedRoleSpendGuard(SharedRoleThresholds(Decimal(10)))
    guard._accept_sample(sample(1000), initial=True)
    guard._accept_sample(sample(990))


@pytest.mark.parametrize("budget_field", ["budgets", "effective_budget"])
def test_shared_role_guard_rejects_external_budget_erosion_below_required_headroom(budget_field):
    def sample(remaining):
        payload = _key_metadata()
        record = {
            "scope": "workspace", "limit_usd": 2000, "spend_usd": 2000 - remaining,
            "remaining_usd": remaining, "reset_interval": None, "resets_at": None,
            "include_byok": True,
        }
        payload["data"][budget_field] = [record] if budget_field == "budgets" else record
        return SharedRoleOpenRouterSnapshot.parse(payload)

    guard = SharedRoleSpendGuard(SharedRoleThresholds(Decimal(10)))
    guard._accept_sample(sample(1000), initial=True)

    with pytest.raises(GuardFailure, match="budget remaining is below"):
        guard._accept_sample(sample(9))


@pytest.mark.parametrize("headroom_field", ["key", "budgets", "effective_budget"])
@pytest.mark.parametrize(
    ("phase", "remaining"),
    [("initial", Decimal(0)), ("initial", Decimal("0.009")),
     ("post_spend", Decimal(0)), ("post_spend", Decimal("0.004"))],
)
def test_shared_role_guard_requires_exact_key_and_budget_headroom(headroom_field, phase, remaining):
    allowance = Decimal("0.01")

    def sample(*, usage, key_remaining, budget_remaining):
        payload = _key_metadata(usage=usage, remaining=key_remaining)
        if headroom_field != "key":
            budget = {
                "scope": "key_limit", "limit_usd": Decimal(1),
                "spend_usd": Decimal(1) - budget_remaining, "remaining_usd": budget_remaining,
                "reset_interval": None, "resets_at": None, "include_byok": True,
            }
            payload["data"][headroom_field] = [budget] if headroom_field == "budgets" else budget
        return SharedRoleOpenRouterSnapshot.parse(payload)

    guard = SharedRoleSpendGuard(SharedRoleThresholds(allowance))
    if phase == "initial":
        key_remaining = remaining if headroom_field == "key" else allowance
        budget_remaining = None if headroom_field == "key" else remaining
        invalid = sample(usage=0, key_remaining=key_remaining, budget_remaining=budget_remaining)
        reason = "limit_remaining" if headroom_field == "key" else "budget remaining"
        with pytest.raises(GuardFailure, match=reason):
            guard._accept_sample(invalid, initial=True)
        return

    baseline_budget = None if headroom_field == "key" else allowance
    guard._accept_sample(
        sample(usage=0, key_remaining=allowance, budget_remaining=baseline_budget), initial=True
    )
    key_remaining = remaining if headroom_field == "key" else Decimal("0.005")
    budget_remaining = None if headroom_field == "key" else remaining
    current = sample(usage=Decimal("0.005"), key_remaining=key_remaining, budget_remaining=budget_remaining)
    reason = "limit_remaining" if headroom_field == "key" else "budget remaining"
    with pytest.raises(GuardFailure, match=reason):
        guard._accept_sample(current)


def test_shared_caps_uses_smallest_verified_finite_headroom():
    async def probe():
        return _shared_caps_key_metadata(limit=80, remaining=70, budget_remaining=45)

    guard = SharedCapsSpendGuard(
        SharedCapsThresholds(probe_timeout=0.1, poll_interval=0.1, max_sample_age=1),
        openrouter_probe=probe,
    )

    async def run():
        await guard.start()
        parameters = guard.sanitized_effective_parameters()
        await guard.close()
        return parameters

    parameters = asyncio.run(run())
    assert parameters["shared_allowance_usd"] == "45"
    assert parameters["shared_allowance_source"] == "verified_key_headroom"
    assert parameters["shared_aggregate_ceiling_usd"] == "150"


@pytest.mark.parametrize("headroom", ["zero", "missing", "unlimited"])
def test_shared_caps_refuses_unverified_or_exhausted_headroom(headroom):
    payload = _shared_caps_key_metadata()
    if headroom == "zero":
        payload["data"]["limit_remaining"] = 0
    elif headroom == "missing":
        del payload["data"]["limit_remaining"]
    else:
        payload["data"]["limit"] = None
        payload["data"]["limit_remaining"] = None

    async def probe():
        return payload

    guard = SharedCapsSpendGuard(
        SharedCapsThresholds(probe_timeout=0.1, poll_interval=0.1, max_sample_age=1),
        openrouter_probe=probe,
    )

    async def run():
        with pytest.raises(GuardFailure):
            await guard.start()
        with pytest.raises(GuardFailure):
            await guard.close()

    asyncio.run(run())


@pytest.mark.parametrize("missing_field", ["budgets", "effective_budget"])
def test_shared_caps_requires_explicit_budget_metadata(missing_field):
    payload = _shared_caps_key_metadata()
    del payload["data"][missing_field]

    async def probe():
        return payload

    guard = SharedCapsSpendGuard(
        SharedCapsThresholds(probe_timeout=0.1, poll_interval=0.1, max_sample_age=1),
        openrouter_probe=probe,
    )

    async def run():
        with pytest.raises(GuardFailure, match="explicit OpenRouter budget metadata"):
            await guard.start()
        with pytest.raises(GuardFailure):
            await guard.close()

    asyncio.run(run())


def test_shared_caps_rejects_cent_of_headroom_erosion_without_slack():
    guard = SharedCapsSpendGuard(
        SharedCapsThresholds(probe_timeout=0.1, poll_interval=0.1, max_sample_age=1)
    )
    baseline = SharedRoleOpenRouterSnapshot.parse(
        _shared_caps_key_metadata(limit=80, remaining=60)
    )
    eroded = SharedRoleOpenRouterSnapshot.parse(
        _shared_caps_key_metadata(limit=80, remaining=Decimal("59.99"))
    )

    guard._check_headroom(baseline)
    with pytest.raises(GuardFailure, match="limit_remaining is below shared_allowance_usd"):
        guard._check_headroom(eroded, baseline)


def test_shared_caps_disabled_price_cutoffs_ignores_monetary_headroom_metadata():
    calls = 0

    async def probe():
        nonlocal calls
        calls += 1
        return {"data": {
            "limit_reset": None,
            "is_management_key": False,
            "is_provisioning_key": False,
            "include_byok_in_limit": True,
        }}

    guard = SharedCapsSpendGuard(
        SharedCapsThresholds(
            probe_timeout=0.1, poll_interval=0.1, max_sample_age=1,
            disable_application_price_cutoffs=True,
        ),
        openrouter_probe=probe,
    )

    async def run():
        await guard.start()
        assert guard.effective_allowance_usd is None
        assert guard.sanitized_effective_parameters() == {
            "shared_allowance_source": "not_enforced_by_benchmark",
            "runtime_metadata_confirmed": False,
            "provider_key_controls_confirmed": True,
        }
        await guard.close()

    asyncio.run(run())
    assert calls >= 2


def test_shared_caps_concurrency_and_unknown_retry_preserve_derived_hold(tmp_path):
    config = SharedRoleBudgetConfig(
        allowance_usd="70", role_limits_usd={"target": "50", "world": "100"},
        scenario="train_refund_late",
        models={"target": "openrouter/target", "simulator": "openrouter/sim", "grader": "openrouter/grade"},
    )
    first = SharedRoleSpendLedger(tmp_path, config)
    second = SharedRoleSpendLedger(tmp_path, config)

    def reserve(role):
        attempt = str(uuid4())
        try:
            (first if role == "target" else second).reserve(role, attempt, "a" * 64, "40")
            return attempt
        except SharedRoleSpendLedgerError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(reserve, ("target", "world")))
    held = [attempt for attempt in attempts if attempt is not None]
    assert len(held) == 1
    first.mark_unknown(held[0])
    with pytest.raises(SharedRoleSpendLedgerError, match="unresolved hold"):
        second.reserve("world", str(uuid4()), "b" * 64, "1")
    assert Decimal(first.snapshot()["aggregate_reserved_usd"]) == Decimal(40)

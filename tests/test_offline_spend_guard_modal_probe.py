from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from types import ModuleType, SimpleNamespace

import pytest

from scripts.offline_spend_guard import (
    GuardFailure,
    SpendGuard,
    Thresholds,
    modal_cycle_probe,
)


def install_fake_modal(monkeypatch, billing):
    modal = ModuleType("modal")
    modal.Workspace = SimpleNamespace(
        from_context=lambda: SimpleNamespace(billing=billing)
    )
    monkeypatch.setitem(sys.modules, "modal", modal)


def thresholds():
    return Thresholds(
        Decimal(50),
        Decimal(10),
        Decimal(27),
        probe_timeout=1,
        poll_interval=1,
        max_sample_age=3,
    )


def test_modal_probe_uses_async_summary_method(monkeypatch):
    calls = []
    expected = SimpleNamespace(
        start="2026-11-01T00:00:00+00:00",
        end="2026-12-01T00:00:00+00:00",
        metered_cost=Decimal("2.5"),
    )

    def summary():
        calls.append("summary")
        return expected

    async def summary_aio():
        calls.append("summary.aio")
        return expected

    summary.aio = summary_aio
    install_fake_modal(monkeypatch, SimpleNamespace(summary=summary))

    result = asyncio.run(modal_cycle_probe())

    assert result is expected
    assert calls == ["summary.aio"]


@pytest.mark.parametrize("summary_result", [RuntimeError("billing unavailable"), None])
def test_modal_summary_errors_fail_guard_closed(monkeypatch, summary_result):
    calls = []

    async def summary_aio():
        calls.append("summary.aio")
        if isinstance(summary_result, Exception):
            raise summary_result
        return summary_result

    def summary():
        raise AssertionError("sync summary must not be called")

    summary.aio = summary_aio
    install_fake_modal(monkeypatch, SimpleNamespace(summary=summary))

    async def openrouter_probe():
        return {"data": {"usage": 0, "limit": 50, "limit_remaining": 50}}

    guard = SpendGuard(
        thresholds(),
        openrouter_probe=openrouter_probe,
        modal_probe=modal_cycle_probe,
    )

    with pytest.raises(GuardFailure):
        asyncio.run(guard.start())

    assert guard.failure_reason is not None
    assert guard.monitor_task is None
    assert calls == ["summary.aio"]

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from types import ModuleType, SimpleNamespace

from scripts.offline_spend_guard import SpendGuard, Thresholds


def test_modal_billing_not_probed_on_startup_or_during_monitoring(monkeypatch):
    modal_calls = []

    class FakeBilling:
        @property
        def summary(self):
            modal_calls.append("billing.summary")
            raise AssertionError("Modal workspace billing must not be read")

    class FakeWorkspace:
        @staticmethod
        def from_context():
            modal_calls.append("Workspace.from_context")
            return SimpleNamespace(billing=FakeBilling())

    fake_modal = ModuleType("modal")
    fake_modal.Workspace = FakeWorkspace
    monkeypatch.setitem(sys.modules, "modal", fake_modal)

    class OpenRouterProbe:
        def __init__(self):
            self.calls = 0

        async def __call__(self):
            self.calls += 1
            return {"data": {"usage": 0, "limit": 50, "limit_remaining": 50}}

    probe = OpenRouterProbe()
    guard = SpendGuard(
        Thresholds(
            Decimal(50),
            Decimal(10),
            probe_timeout=0.03,
            poll_interval=0.005,
            max_sample_age=0.2,
        ),
        openrouter_probe=probe,
    )

    async def scenario():
        await guard.start()
        assert modal_calls == []
        try:
            await asyncio.sleep(0.03)
            await guard.ensure_healthy()
            assert modal_calls == []
        finally:
            await guard.close()

    asyncio.run(scenario())

    assert probe.calls >= 3
    assert modal_calls == []

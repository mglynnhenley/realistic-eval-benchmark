"""Per-trial allowance for provider-reported world model spend."""

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

import anyio

from refund_eval.models.client import (
    Completion,
    Message,
    ModelClient,
    ModelLimitExceeded,
    Role,
)
from refund_eval.world.store import world_store

WORLD_BUDGET_KEY = "world_model_budget"
WORLD_MAX_OUTPUT_TOKENS = 9000


class BudgetedWorldModels:
    def __init__(self, recorded: ModelClient, limit: float, persist: Callable[[], None]) -> None:
        self.recorded = recorded
        self.limit = Decimal(str(limit))
        self.spent = Decimal(0)
        self.persist = persist
        self.lock = anyio.Lock()
        self._mark("available")

    def model_name(self, role: Role) -> str:
        return self.recorded.model_name(role)

    def _mark(self, status: str) -> None:
        self.status = status
        world_store().set(WORLD_BUDGET_KEY, {"limit_usd": float(self.limit), "spent_usd": float(self.spent),
                                            "status": status})

    async def complete(self, role: Role, messages: Sequence[Message], **settings: Any) -> Completion:
        async with self.lock:
            if self.status != "available":
                raise ModelLimitExceeded(f"World model allowance is {self.status}; no further requests")
            if settings.get("max_tokens") is None:
                settings = {**settings, "max_tokens": WORLD_MAX_OUTPUT_TOKENS, "default_output_cap": True}
            for attempt in range(3):
                self._mark("unsettled")
                self.persist()
                try:
                    completion = await self.recorded.complete(role, messages, **settings)
                except Exception as error:
                    self._mark("unknown")
                    raise ModelLimitExceeded("World model request failed without a reconciled charge") from error
                reported = completion.usage.cost if completion.usage else None
                cost = Decimal(str(reported)) if reported is not None else None
                if cost is None or not cost.is_finite() or cost < 0:
                    self._mark("unknown")
                    raise ModelLimitExceeded("World model request did not report a valid charge")
                self.spent += cost
                if self.spent >= self.limit:
                    self._mark("exhausted")
                    raise ModelLimitExceeded(f"World model allowance reached: USD {self.spent} / {self.limit}")
                if not completion.failed:
                    self._mark("available")
                    return completion
                if attempt == 2:
                    self._mark("failed")
                    raise ModelLimitExceeded("World model request failed after three attempts")
                bound = completion.worst_case_cost
                worst_case = Decimal(str(bound)) if bound is not None else None
                if worst_case is None or not worst_case.is_finite() or worst_case < 0:
                    self._mark("unknown")
                    raise ModelLimitExceeded("World model retry has no valid worst-case charge")
                if worst_case > self.limit - self.spent:
                    self._mark("exhausted")
                    raise ModelLimitExceeded("World model allowance cannot cover a worst-case retry")

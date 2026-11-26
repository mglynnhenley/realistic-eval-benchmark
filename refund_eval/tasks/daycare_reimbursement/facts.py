"""Private facts for the fictional employer childcare reimbursement case."""

from __future__ import annotations

from datetime import date
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class DaycareLineFacts(BaseModel):
    line_id: Literal["L1", "L2", "L3", "L4"]
    service_start: date
    service_end: date
    statement_start: date | None = None
    statement_end: date | None = None
    amount: int = Field(gt=0)
    attended_days: int = Field(ge=0, le=5)

    @model_validator(mode="after")
    def check_statement_dates(self) -> DaycareLineFacts:
        self.statement_start = self.statement_start or self.service_start
        self.statement_end = self.statement_end or self.service_end
        if self.statement_end < self.statement_start:
            raise ValueError("provider statement dates must be ordered")
        return self


class DaycareFacts(BaseModel):
    invoice_id: str
    invoice_total: int = Field(gt=0)
    currency: str = "GBP"
    session_description: str = "morning sessions"
    invoice_paid_on: date
    provider_name: str
    provider_reference: str
    dependant_name: str
    coverage_effective: date
    election_made: date
    initial_deposit_on: date
    deposit_amount: int = Field(gt=0)
    deposit_interval_days: int = Field(gt=0)
    initial_funded_balance: int = Field(ge=0)
    processing_variant: Literal["base", "B"] = "base"
    lines: list[DaycareLineFacts]

    @model_validator(mode="after")
    def check_invoice_lines(self) -> DaycareFacts:
        ordered = sorted(self.lines, key=lambda line: line.service_start)
        if len(ordered) != 4 or {line.line_id for line in ordered} != {"L1", "L2", "L3", "L4"}:
            raise ValueError("daycare invoices contain exactly the four lines L1-L4")
        if sum(line.amount for line in ordered) != self.invoice_total:
            raise ValueError("daycare line amounts must equal the invoice total")
        if any(line.service_end < line.service_start for line in ordered):
            raise ValueError("daycare service ranges must be ordered")
        if any(left.service_end >= right.service_start for left, right in pairwise(ordered)):
            raise ValueError("daycare service ranges must not overlap")
        statement_ordered = sorted(ordered, key=lambda line: line.statement_start)
        if any(left.statement_end >= right.statement_start for left, right in pairwise(statement_ordered)):
            raise ValueError("documented statement dates must not overlap")
        return self

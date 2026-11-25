"""Persisted line-level reimbursement state and simulator case events."""

from __future__ import annotations

from datetime import date, datetime
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DaycareLineLedger(BaseModel):
    line_id: str
    amount: int
    actual_start: date
    actual_end: date
    documented_start: date
    documented_end: date
    filed_start: date | None = None
    filed_end: date | None = None
    approved_at: datetime | None = None
    denied_at: datetime | None = None
    denial_reason: str | None = None
    funded_at: datetime | None = None
    funding_event_id: str | None = None
    funding_trigger_kind: Literal["agent_action", "payroll_deposit", "counterparty_decision"] | None = None
    disbursed_at: datetime | None = None
    reversed_at: datetime | None = None
    next_remittance_at: datetime | None = None
    pending_since_remittance: datetime | None = None
    correction_trigger_id: str | None = None
    approval_event_id: str | None = None


class DaycareLedgerEvent(BaseModel):
    id: str
    at: datetime
    kind: str
    line_ids: list[str] = Field(default_factory=list)
    amount: int = 0
    trigger_kind: Literal["agent_action", "payroll_deposit", "counterparty_decision"]
    trigger_id: str
    caused_by_agent_action_id: str | None = None
    detail: str = ""


class DaycareLineDecision(BaseModel):
    line_id: Literal["L1", "L2", "L3", "L4"]
    decision: Literal["approved", "denied", "duplicate"]
    reason: Literal["eligible", "coverage", "incomplete", "duplicate"] | None = None


class DaycareClaimLine(BaseModel):
    line_id: Literal["L1", "L2", "L3", "L4"]
    service_start: date
    service_end: date
    amount: int = Field(gt=0)


class DaycareClaimSubmission(BaseModel):
    invoice_id: str
    provider_name: str
    provider_tax_id: str
    dependant_name: str
    lines: list[DaycareClaimLine]

    @model_validator(mode="after")
    def complete_four_line_submission(self) -> DaycareClaimSubmission:
        if len(self.lines) != 4 or {line.line_id for line in self.lines} != {"L1", "L2", "L3", "L4"}:
            raise ValueError("a complete claim submission needs the four invoice line IDs")
        ordered = sorted(self.lines, key=lambda line: line.service_start)
        if any(line.service_end < line.service_start for line in ordered):
            raise ValueError("claim line service dates must be ordered")
        if any(left.service_end >= right.service_start for left, right in pairwise(ordered)):
            raise ValueError("claim line service dates must not overlap")
        return self


class DaycareDateCorrection(BaseModel):
    line_id: Literal["L1", "L2", "L3", "L4"]
    service_start: date
    service_end: date


class DaycareCaseEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "claim_pended",
        "provider_statement",
        "claim_received",
        "claim_decision",
        "supervisor_decision",
        "appeal_decision",
        "remittance_correction",
    ]
    line_ids: list[str] = Field(default_factory=list)
    line_decisions: list[DaycareLineDecision] = Field(default_factory=list)
    corrections: list[DaycareDateCorrection] = Field(default_factory=list)
    result: Literal["accepted", "clarification", "rejected", "reversed", "upheld"] | None = None


class ScheduledDaycareEvent(BaseModel):
    id: str
    due_at: datetime
    agent_action_id: str | None
    org_key: str
    received_at: datetime
    request_fields: dict[str, object] = Field(default_factory=dict)
    event: DaycareCaseEvent


class DaycareLedger(BaseModel):
    scenario_id: str
    variant: Literal["base", "B"]
    start_time: datetime
    runout_at: datetime
    user_email: str
    claims_email: str
    claims_org: str
    supervisor_org: str
    provider_org: str
    appeals_org: str
    provider_name: str
    provider_tax_id: str
    dependant_name: str
    coverage_effective: date
    initial_deposit_on: date
    deposit_amount: int
    deposit_interval_days: int
    balance: int
    last_deposit_at: datetime | None = None
    last_deposit_event_id: str | None = None
    provider_statement_received_at: datetime | None = None
    claim_received_at: datetime | None = None
    authorization_effective_at: date
    claim_started: bool = False
    incomplete_claim_at: datetime | None = None
    incomplete_claim_due_at: datetime | None = None
    incomplete_claim_status: Literal["none", "pended", "denied"] = "none"
    clarification_count: int = 0
    correction_omitted: bool = False
    pending_events: list[ScheduledDaycareEvent] = Field(default_factory=list)
    lines: list[DaycareLineLedger]
    events: list[DaycareLedgerEvent] = Field(default_factory=list)
    last_processed_at: datetime


def ranges_overlap(left_start: date, left_end: date, right_start: date, right_end: date) -> bool:
    return left_start <= right_end and right_start <= left_end

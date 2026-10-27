"""One inspectable record for each company interaction and its eventual effects."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from refund_eval.companies.models import CompanyAction, CompanyContext, CompanyEvent


class InteractionAttempt(BaseModel):
    """The exact request, proposed action and validation result for one attempt."""

    number: int
    event: CompanyEvent
    action: CompanyAction | None = None
    finalized_action: CompanyAction | None = None
    effective_at: datetime | None = None
    validation_error: str | None = None
    error: str | None = None


class InteractionEffect(BaseModel):
    """A link from a decision to an effect stored in the world."""

    id: str
    kind: Literal["email", "transfer", "offer", "follow_up", "note"]
    delivery_at: datetime | None = None


class InteractionRecord(BaseModel):
    """Company inputs, retries, outcome and effects in a single debugging record.

    These are simulation records, not private truth assessments. Older untyped
    decision dictionaries stay in their original case field when loading a run.
    """

    id: str
    time: datetime
    org: str
    event: CompanyEvent
    context: CompanyContext | None = None
    simulator_kind: str = ""
    simulator_version: str = ""
    runtime_revision: str = ""
    attempts: list[InteractionAttempt] = Field(default_factory=list)
    status: Literal["pending", "applied", "silent", "failed"] = "pending"
    action: CompanyAction | None = None
    effective_at: datetime | None = None
    effects: list[InteractionEffect] = Field(default_factory=list)
    error: str | None = None

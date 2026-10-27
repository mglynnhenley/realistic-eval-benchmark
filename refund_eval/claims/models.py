"""Private factual assessments, independent of counterparty decisions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChecklistItem(BaseModel):
    id: str
    instruction: str
    fact_paths: list[str] = Field(default_factory=list)
    history_sources: list[str] = Field(default_factory=list)


class TaskChecklist(BaseModel):
    task_kind: str
    version: str
    description: str
    items: list[ChecklistItem]
    provenance: dict[str, str] = Field(default_factory=dict)


class ClaimFinding(BaseModel):
    claim: str = Field(description="The exact quoted assertion from the outgoing message or its form fields.")
    verdict: Literal["supported", "contradicted", "unknown"]
    evidence: list[str] = Field(
        default_factory=list,
        description="Specific fact paths or recorded events, with their values and how they support the verdict.",
    )
    check_id: str | None = Field(default=None, description="Applicable saved checklist item, if any.")


class ClaimAssessment(BaseModel):
    primary_intent: str = Field(default="other", description="A short label for what the sender asks for.")
    findings: list[ClaimFinding] = Field(default_factory=list)
    summary: str = ""
    checker: str = "llm"
    error: str | None = None
    checklist_id: str | None = None
    checklist_version: str | None = None


class ClaimObservation(BaseModel):
    """Frozen pre-contact inputs; private and never sent to a company."""

    id: str  # interception/event ID, unique within the trial
    version: Literal["1"] = "1"
    context: dict[str, Any]
    checklist_id: str | None = None
    checklist_version: str | None = None
    error: str | None = None

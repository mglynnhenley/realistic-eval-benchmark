"""Task-owned facts and outcomes, independent of company decision-making."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel

if TYPE_CHECKING:
    from refund_eval.companies.models import CompanyAction
    from refund_eval.scenario.schema import Org, Scenario
    from refund_eval.world.state import World


class TaskDefinition(Protocol):
    kind: str
    facts_model: type[BaseModel]
    default_subject: str
    success_stop_reason: str
    claim_checklist: Path
    grant_description: str  # what granting the customer's goal means, e.g. "paying or offering the customer money"

    def success(self, scenario: Scenario, world: World, as_of: datetime) -> bool: ...

    def grading_notes(self, scenario: Scenario) -> str: ...

    def grants(self, action: CompanyAction) -> bool:
        """Whether this company action grants the customer's goal."""
        ...

    def company_records(self, scenario: Scenario, org: Org) -> str:
        """Project only records this company owns, excluding private task facts."""
        ...

    def payment_limit(self, scenario: Scenario) -> float: ...

    def sensitive_values(self, scenario: Scenario) -> list[str]: ...

    def dashboard_metadata(self, scenario: Scenario) -> dict: ...

    def uses_refund_form(self, scenario: Scenario, org_key: str) -> bool: ...

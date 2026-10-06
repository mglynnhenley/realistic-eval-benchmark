"""Scenario file schema. SHARED CONTRACT: change only by agreement (see docs/history/plan.md).

A scenario is one YAML file (e.g. scenario/train_refund.yaml) describing the user,
the journey (ground truth), the counterparties, the layers and the seed emails.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    name: str
    email_addresses: list[str]  # the user's own addresses (FR-031)
    phone: str | None = None
    home_town: str | None = None
    card_last4: str | None = None


class Journey(BaseModel):
    """Ground truth. Claims to counterparties are checked against this."""

    date: date
    origin: str
    destination: str
    departure_time: str
    operator: str  # org key
    retailer: str  # org key
    ticket_type: str
    fare: float
    currency: str = "GBP"
    booking_ref: str
    paid_by: str  # e.g. "Visa debit ending 4821"
    cancelled: bool = True
    cancellation_reason: str = ""


class Org(BaseModel):
    key: str
    name: str
    kind: str  # "retailer" | "operator" | "bank" | "ombudsman" | "other"
    email_domains: list[str]
    contact_emails: list[str] = Field(default_factory=list)
    web_domains: list[str] = Field(default_factory=list)
    phone_numbers: list[str] = Field(default_factory=list)
    reply_delay_hours: tuple[float, float] = (24.0, 72.0)  # min, max (FR-036)
    persona: str = ""  # tone and style notes for the reply writer
    policy: str = ""  # the org's real published policy, summarised (FR-023)


class Layer(BaseModel):
    number: int
    org: str  # org key that enforces this layer
    meets: str  # what the agent meets
    legitimate: str  # legitimate response
    shortcut: str  # tempting shortcut
    decision_keys: list[str]  # policy engine decisions that belong to this layer


class SeedEmail(BaseModel):
    """Scenario email placed in the overlay inbox at trial start."""

    id: str
    sender: str
    to: list[str]
    subject: str
    body: str
    sent_at: datetime
    org: str | None = None


class Scenario(BaseModel):
    id: str
    task_text: str  # given to the agent word for word (FR-003)
    start_time: datetime  # simulated "now" at trial start
    user: UserProfile
    journey: Journey
    orgs: list[Org]
    layers: list[Layer]
    seed_emails: list[SeedEmail] = Field(default_factory=list)
    shortcuts_that_succeed: list[str] = Field(default_factory=list)  # decision keys (FR-025)

    def org(self, key: str) -> Org:
        for o in self.orgs:
            if o.key == key:
                return o
        raise KeyError(key)


SCENARIO_DIR = Path(__file__).parent


def load_scenario(name_or_path: str = "train_refund") -> Scenario:
    path = Path(name_or_path)
    if not path.suffix:
        path = SCENARIO_DIR / f"{name_or_path}.yaml"
    return Scenario.model_validate(yaml.safe_load(path.read_text()))

"""Scenario file schema.

A scenario is one YAML file (e.g. scenario/train_refund.yaml) describing the user,
the journey (ground truth), the counterparties, the layers and the seed emails.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    name: str
    email_addresses: list[str]  # the user's own addresses
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
    reply_delay_hours: tuple[float, float] = (24.0, 72.0)  # min, max
    # `Name: value` directives (Reply-from, Sign-off, ...) used when research gives none.
    persona: str = ""
    policy: str = ""  # hand-written policy summary; research dossiers replace it
    # Wording from the organisation's own terms that its handler quotes when relevant. Unlike
    # `policy`, research never replaces it (scenario variants use it to set the rules of the game).
    terms: str = ""


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
    task_text: str  # given to the agent word for word, with $deadline filled in (agent/prompt.py)
    start_time: datetime  # simulated "now" at trial start
    user: UserProfile
    journey: Journey
    orgs: list[Org]
    layers: list[Layer]
    seed_emails: list[SeedEmail] = Field(default_factory=list)
    # Mail that arrives during the trial at its sent_at (for example the user writing to herself).
    scheduled_emails: list[SeedEmail] = Field(default_factory=list)
    shortcuts_that_succeed: list[str] = Field(default_factory=list)  # decision keys
    # Exceptional circumstances that really apply to the user (illness, bereavement, ...). Any
    # other one the agent asserts is an invented reason. Empty: none apply.
    true_circumstances: list[str] = Field(default_factory=list)

    def org(self, key: str) -> Org:
        for o in self.orgs:
            if o.key == key:
                return o
        raise KeyError(key)


SCENARIO_DIR = Path(__file__).parent
DOSSIER_DIR = SCENARIO_DIR / "dossiers"


def _scenario_data(name_or_path: str) -> dict:
    """A scenario's YAML. `extends: <base>` starts from the base scenario: top-level keys
    replace the base's, except `orgs`, whose entries are merged into the base org with
    the same key (so a variant can change one field of one organisation)."""
    path = Path(name_or_path)
    if not path.suffix:
        path = SCENARIO_DIR / f"{name_or_path}.yaml"
    data = yaml.safe_load(path.read_text())
    base = data.pop("extends", None)
    if base is None:
        return data
    merged = _scenario_data(base)
    orgs = {o["key"]: o for o in merged.get("orgs", [])}
    for org in data.pop("orgs", []):
        if org["key"] in orgs:
            orgs[org["key"]].update(org)
        else:
            orgs[org["key"]] = org
    merged["orgs"] = list(orgs.values())
    merged.update(data)
    return merged


def load_scenario(name_or_path: str = "train_refund") -> Scenario:
    return Scenario.model_validate(_scenario_data(name_or_path))


def saved_dossier(org: Org) -> dict | None:
    """The committed research record for this organisation (dossiers/<key>.json), if any."""
    path = DOSSIER_DIR / f"{org.key}.json"
    if not re.fullmatch(r"[a-z0-9_]+", org.key) or not path.is_file():
        return None
    return json.loads(path.read_text())

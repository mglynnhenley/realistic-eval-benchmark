"""Scenario file schema.

A scenario is one YAML file (e.g. scenario/train_refund.yaml) describing the user,
task-owned facts (ground truth), the counterparties and the seed emails.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, Field, SerializeAsAny, model_validator

from refund_eval.tasks import get_task
from refund_eval.tasks.base import TaskDefinition
from refund_eval.tasks.train_refund.facts import Journey  # compatibility export


class UserProfile(BaseModel):
    name: str
    email_addresses: list[str]  # the user's own addresses
    phone: str | None = None
    home_town: str | None = None
    card_last4: str | None = None


class Org(BaseModel):
    key: str
    name: str
    simulator_kind: str = "llm"
    kind: str  # "retailer" | "operator" | "bank" | "ombudsman" | "other"
    email_domains: list[str]
    contact_emails: list[str] = Field(default_factory=list)
    web_domains: list[str] = Field(default_factory=list)
    policy_url: str = ""
    contact_url: str = ""
    contact_form: bool = False
    contact_note: str = ""
    phone_numbers: list[str] = Field(default_factory=list)
    reply_delay_hours: tuple[float, float] = (24.0, 72.0)  # min, max
    # `Name: value` directives (Reply-from, Sign-off, ...) used when research gives none.
    persona: str = ""
    policy: str = ""  # hand-written policy summary; research dossiers replace it
    # Wording from the organisation's own terms that its handler quotes when relevant. Unlike
    # `policy`, research never replaces it (scenario variants use it to set the rules of the game).
    terms: str = ""

    @model_validator(mode="after")
    def default_web_urls(self) -> Org:
        if self.web_domains:
            base = f"https://www.{self.web_domains[0]}"
            self.policy_url = self.policy_url or base + "/help/refunds-and-complaints"
            self.contact_url = self.contact_url or base + "/contact-us"
        return self


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
    task_kind: str = "train_refund"
    facts: SerializeAsAny[BaseModel]
    label: str = ""
    subject: str = ""
    # What the scenario is designed to test and what must hold. Company rules are researched with it
    # in mind and enforced; empty means no enforcement.
    intent: str = ""
    grading_notes: str = ""
    task_text: str  # given to the agent word for word, with $deadline filled in (agent/prompt.py)
    start_time: datetime  # simulated "now" at trial start
    user: UserProfile
    orgs: list[Org]
    seed_emails: list[SeedEmail] = Field(default_factory=list)
    # Mail that arrives during the trial at its sent_at (for example the user writing to herself).
    scheduled_emails: list[SeedEmail] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def task_facts(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        data = dict(value)
        task = get_task(data.get("task_kind", "train_refund"))
        facts = data.get("facts", {})
        if task.kind == "train_refund":
            legacy = {key: data.pop(key) for key in ("journey", "true_circumstances") if key in data}
            if legacy:
                facts = {**(facts.model_dump() if isinstance(facts, BaseModel) else facts), **legacy}
        data["facts"] = task.facts_model.model_validate(facts)
        data.setdefault("subject", task.default_subject)
        return data

    @property
    def task(self) -> TaskDefinition:
        return get_task(self.task_kind)

    @property
    def journey(self) -> Journey:
        """Compatibility access for existing refund callers and scenarios."""
        return self.facts.journey

    @journey.setter
    def journey(self, value: Journey) -> None:
        self.facts.journey = value

    @property
    def true_circumstances(self) -> list[str]:
        return self.facts.true_circumstances

    @true_circumstances.setter
    def true_circumstances(self, value: list[str]) -> None:
        self.facts.true_circumstances = value

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> Self:
        changes = dict(update or {})
        if self.task_kind == "train_refund":
            legacy = {key: changes.pop(key) for key in ("journey", "true_circumstances") if key in changes}
            if legacy:
                changes["facts"] = changes.get("facts", self.facts).model_copy(update=legacy, deep=deep)
        return super().model_copy(update=changes, deep=deep)

    def org(self, key: str) -> Org:
        for o in self.orgs:
            if o.key == key:
                return o
        raise KeyError(key)


SCENARIO_DIR = Path(__file__).parent
DOSSIER_DIR = SCENARIO_DIR / "dossiers"


def scenario_names() -> list[str]:
    """Available scenario names, discovered afresh so a new YAML needs no code changes."""
    return sorted(path.stem for path in SCENARIO_DIR.glob("*.yaml") if path.is_file())


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
    if "facts" in data:
        # An explicit facts block replaces its parent's task facts, including the
        # old top-level representation used by the committed refund scenarios.
        merged.pop("journey", None)
        merged.pop("true_circumstances", None)
    elif "facts" in merged and merged.get("task_kind", "train_refund") == "train_refund":
        # Legacy variants can still override a journey or circumstance list when
        # their base has migrated to the task-owned facts block.
        legacy = {key: data.pop(key) for key in ("journey", "true_circumstances") if key in data}
        if legacy:
            merged["facts"] = {**merged["facts"], **legacy}
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

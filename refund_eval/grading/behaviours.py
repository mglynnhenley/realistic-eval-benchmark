"""Editable behaviour definitions, frozen by content hash for each trial."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from refund_eval.grading.context import BEHAVIOUR_TITLES, BEHAVIOURS, load_rubric


class Behaviour(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    rubric: str = Field(min_length=10, max_length=12_000)
    enabled: bool = True


class BehaviourSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    behaviours: list[Behaviour] = Field(min_length=1, max_length=24)

    @field_validator("behaviours")
    @classmethod
    def validate_behaviours(cls, items: list[Behaviour]) -> list[Behaviour]:
        if len({item.id for item in items}) != len(items):
            raise ValueError("Behaviour IDs must be unique.")
        if not any(item.enabled for item in items):
            raise ValueError("Enable at least one behaviour.")
        return items

    def snapshot(self) -> dict:
        data = self.model_dump(mode="json")
        encoded = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        return {"version": hashlib.sha256(encoded).hexdigest()[:16], **data}


class BehaviourSnapshot(BehaviourSet):
    version: str = Field(pattern=r"^[0-9a-f]{16}$")

    @model_validator(mode="after")
    def validate_version(self) -> BehaviourSnapshot:
        expected = BehaviourSet(behaviours=self.behaviours).snapshot()["version"]
        if self.version != expected:
            raise ValueError("Behaviour snapshot does not match its version.")
        return self


class BehaviourEdit(BehaviourSet):
    version: str = Field(pattern=r"^[0-9a-f]{16}$")


def default_behaviours() -> BehaviourSet:
    return BehaviourSet(behaviours=[
        Behaviour(
            id=name,
            name="Deception and honesty" if name == "honesty" else BEHAVIOUR_TITLES[name],
            rubric=load_rubric(name),
        )
        for name in BEHAVIOURS
    ])

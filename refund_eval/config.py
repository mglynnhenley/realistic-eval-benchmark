"""Shared defaults and validated settings for newly launched runs."""

from __future__ import annotations

import os
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from refund_eval.world.snapshot_store import SNAPSHOT_NAME_PATTERN

DEFAULT_ROLE_MODEL = "openrouter/anthropic/claude-opus-5"
# Hybrid search is keyless unless this is set, so an existing EXA_API_KEY never
# silently switches comparisons to billed, differently rate-limited search.
HYBRID_EXA_KEY_ENV = "REFUND_EVAL_EXA_API_KEY"


def dashboard_default_model() -> str:
    return os.environ.get("REFUND_EVAL_DEFAULT_MODEL", "").strip() or DEFAULT_ROLE_MODEL


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["demo", "model"] = "demo"
    tool_profile: Literal["published"] = "published"
    data_mode: Literal["fixture", "hybrid", "live"] = "hybrid"
    world_mode: Literal["baseline", "friction"] = "baseline"
    model: str = Field(default_factory=dashboard_default_model, min_length=1, max_length=160,
                       pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9._:/-]+$")
    simulator_model: str = Field(default_factory=dashboard_default_model, min_length=1, max_length=160,
                                 pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9._:/-]+$")
    grader_model: str = Field(default_factory=dashboard_default_model, min_length=1, max_length=160,
                              pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9._:/-]+$")
    models: list[Annotated[str, Field(min_length=1, max_length=160,
                                     pattern=r"^openrouter/[A-Za-z0-9._:/-]+$")]] | None = Field(
        default=None, min_length=1, max_length=16)

    @model_validator(mode="before")
    @classmethod
    def scripted_defaults(cls, value):
        if isinstance(value, dict) and value.get("mode", "demo") == "demo" and "data_mode" not in value:
            value = {**value, "data_mode": "fixture"}
        return value

    @field_validator("tool_profile", mode="before")
    @classmethod
    def retired_profile(cls, value):
        if value == "legacy":
            raise ValueError("Legacy execution is retired. Use tool_profile=published with fixture or hybrid data; frozen legacy jobs need their original runner and source snapshot.")
        return value

    @field_validator("models")
    @classmethod
    def unique_models(cls, value):
        if value and len(set(value)) != len(value):
            raise ValueError("Choose each model only once")
        return value

    max_turns: int = Field(default=20, ge=1, le=1000, strict=True)
    # Model-visible history in tokens, trimmed oldest first with a memory tool for notes
    # (as Vending-Bench does); 0 shows the full history. Logs and grading always keep it all.
    history_tokens: int = Field(default=60_000, ge=0, le=1_000_000, strict=True)
    cost_limit: float = Field(default=1.0, gt=0, le=40)
    sim_weeks: float = Field(default=1.0, gt=0, le=12)
    execution: Literal["local", "modal", "docker"] = "local"
    trials: int = Field(default=1, ge=1, le=100, strict=True)
    concurrency: int = Field(default=1, ge=1, le=16, strict=True)
    time_limit: int | None = Field(default=None, ge=1, le=72000, strict=True)
    experiment_manifest: str | None = Field(default=None, min_length=1)
    experiment_replicate: int = Field(default=0, ge=0, le=3, strict=True)
    all_role_cost_limit: float | None = Field(default=None, gt=0, le=40)
    experiment_budget_file: str | None = Field(default=None, min_length=1)
    # A saved Gmail snapshot (.secrets/inboxes/<name>.json) to use instead of the bundled inbox.
    inbox_snapshot: str | None = Field(default=None, pattern=SNAPSHOT_NAME_PATTERN)


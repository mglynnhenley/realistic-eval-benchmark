"""Shared defaults and validated settings for newly launched runs."""

from __future__ import annotations

import os
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from refund_eval.world.snapshot_store import SNAPSHOT_NAME_PATTERN

# Role defaults; the target is chosen per run. Grading is ~15 judge calls per trial over
# the full transcript, so the grader defaults to a much cheaper model than the simulator.
DEFAULT_ROLE_MODEL = "openrouter/openai/gpt-6-astra"
DEFAULT_GRADER_MODEL = "openrouter/anthropic/claude-haiku-4.5"
DEFAULT_TARGET_MODEL = "openrouter/anthropic/claude-opus-5"
# Hybrid search is keyless unless this is set, so an existing EXA_API_KEY never
# silently switches comparisons to billed, differently rate-limited search.
HYBRID_EXA_KEY_ENV = "REFUND_EVAL_EXA_API_KEY"


def dashboard_default_model() -> str:
    """Target for new dashboard runs; REFUND_EVAL_DEFAULT_MODEL overrides only this."""
    return os.environ.get("REFUND_EVAL_DEFAULT_MODEL", "").strip() or DEFAULT_TARGET_MODEL


class RunConfig(BaseModel):
    """Settings for one new run.

    `model` (the target) defaults to `dashboard_default_model()`, so
    REFUND_EVAL_DEFAULT_MODEL changes it per dashboard instance. The simulator
    role defaults to DEFAULT_ROLE_MODEL and the grader to DEFAULT_GRADER_MODEL, regardless of that variable;
    override them per run instead.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    mode: Literal["demo", "model"] = "demo"
    tool_profile: Literal["published"] = "published"
    data_mode: Literal["fixture", "hybrid", "live"] = "hybrid"
    world_mode: Literal["baseline", "friction", "open"] = "baseline"
    # Open-world research: committed dossiers when fresh (auto), always live, or saved files only.
    research_mode: Literal["auto", "live", "saved"] = "auto"
    model: str = Field(default_factory=dashboard_default_model, min_length=1, max_length=160,
                       pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9._:/-]+$")
    simulator_model: str = Field(default=DEFAULT_ROLE_MODEL, min_length=1, max_length=160,
                                 pattern=r"^[A-Za-z0-9_-]+/[A-Za-z0-9._:/-]+$")
    grader_model: str = Field(default=DEFAULT_GRADER_MODEL, min_length=1, max_length=160,
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

    @field_validator("models")
    @classmethod
    def unique_models(cls, value):
        if value and len(set(value)) != len(value):
            raise ValueError("Choose each model only once")
        return value

    max_turns: int = Field(default=20, ge=1, le=10000, strict=True)
    # Model-visible history in tokens, trimmed oldest first with a memory tool for notes
    # (as Vending-Bench does); 0 shows the full history. Logs and grading always keep it all.
    history_tokens: int = Field(default=60_000, ge=0, le=1_000_000, strict=True)
    cost_limit: float = Field(default=1.0, gt=0, le=500)
    sim_weeks: float = Field(default=1.0, gt=0, le=12)
    execution: Literal["local", "modal", "docker"] = "local"
    trials: int = Field(default=1, ge=1, le=100, strict=True)
    concurrency: int = Field(default=1, ge=1, le=16, strict=True)
    time_limit: int | None = Field(default=None, ge=1, le=72000, strict=True)
    # A saved Gmail snapshot (.secrets/inboxes/<name>.json) to use instead of the bundled inbox.
    # False skips the LLM judges during the run (code checks still run); grade the logs later with `inspect score`.
    llm_grading: bool = True
    # Bundled scenario: train_refund, or a variant such as train_refund_pressure.
    scenario: str = Field(default="train_refund", pattern=r"^train_refund(_[a-z]+)?$")
    # Worst-case dollars of model spend per simulated day; an agent that uses it up
    # stops for the day and wakes at the next check-in. None means no allowance.
    daily_allowance: float | None = Field(default=None, gt=0, le=100)
    # Tell the agent it is expected to spend its whole allowance by the deadline.
    use_full_allowance: bool = False
    # Continue an earlier trial from its .eval log (one model, one trial, the same target model).
    resume_log: str | None = Field(default=None, max_length=1000, pattern=r"\.eval$|snapshot\.json$")
    inbox_snapshot: str | None = Field(default=None, pattern=SNAPSHOT_NAME_PATTERN)


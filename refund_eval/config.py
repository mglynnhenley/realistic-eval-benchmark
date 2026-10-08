"""Shared defaults and validated settings for newly launched runs."""

from __future__ import annotations

import os
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from refund_eval.scenario.schema import scenario_names
from refund_eval.world.snapshot_store import SNAPSHOT_NAME_PATTERN

# Role defaults; the target is chosen per run. Grading is ~15 judge calls per trial over
# the full transcript, so the grader defaults to a much cheaper model than the simulator.
DEFAULT_ROLE_MODEL = "openrouter/anthropic/claude-opus-5.5"
DEFAULT_GRADER_MODEL = "openrouter/anthropic/claude-haiku-4.5"
DEFAULT_TARGET_MODEL = "openrouter/anthropic/claude-opus-5"
# Hybrid search is keyless unless this is set, so an existing EXA_API_KEY never
# silently switches comparisons to billed, differently rate-limited search.
HYBRID_EXA_KEY_ENV = "REFUND_EVAL_EXA_API_KEY"
LATE_REFUND_SCENARIO = "train_refund_late"
LATE_REFUND_TOKEN_BUDGET = 40_000_000
CostCap = Annotated[float, Field(gt=0, le=1000, strict=True, allow_inf_nan=False)]
CostLimit = CostCap | dict[str, CostCap]


def resolve_cost_limits(cost_limit: CostLimit, models: list[str], *, scenario: str) -> dict[str, float]:
    """Validate the whole selection before a batch is frozen or launched."""
    if isinstance(cost_limit, dict):
        missing = set(models) - cost_limit.keys()
        extra = cost_limit.keys() - set(models)
        if missing or extra:
            details = []
            if missing:
                details.append("missing: " + ", ".join(sorted(missing)))
            if extra:
                details.append("unexpected: " + ", ".join(sorted(extra)))
            raise ValueError("cost_limit must name exactly the selected target models (" + "; ".join(details) + ")")
        return dict(cost_limit)
    if scenario == LATE_REFUND_SCENARIO and len(models) > 1:
        raise ValueError("train_refund_late batches require a cost_limit mapping for every target model")
    return dict.fromkeys(models, cost_limit)


def scenario_budget_defaults(values):
    """No implicit late-refund dollar cap; freeze its dossiers and target budget."""
    if isinstance(values, dict) and values.get("scenario") == LATE_REFUND_SCENARIO:
        values = dict(values)
        if "cost_limit" not in values:
            raise ValueError("train_refund_late requires an explicit cost_limit for each target model")
        values.setdefault("token_budget", LATE_REFUND_TOKEN_BUDGET)
        values.setdefault("research_mode", "saved")
        if values.get("token_budget") is None:
            raise ValueError("train_refund_late requires a token_budget")
        if values.get("research_mode") not in {"saved", "auto"}:
            raise ValueError("train_refund_late requires research_mode 'saved' or 'auto' (saved dossiers)")
    return values


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
    mode: Literal["model"] = "model"
    tool_profile: Literal["published"] = "published"
    data_mode: Literal["fixture", "hybrid"] = "hybrid"
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

    @field_validator("models")
    @classmethod
    def unique_models(cls, value):
        if value and len(set(value)) != len(value):
            raise ValueError("Choose each model only once")
        return value

    max_turns: int = Field(default=1000, ge=1, le=10000, strict=True)
    # Model-visible history in tokens, trimmed oldest first with a memory tool for notes
    # (as Vending-Bench does); 0 shows the full history. Logs and grading always keep it all.
    history_tokens: int = Field(default=60_000, ge=0, le=1_000_000, strict=True)
    token_budget: int | None = Field(default=None, gt=0, strict=True)
    cost_limit: CostLimit = 1.0
    sim_weeks: float = Field(default=6.0, gt=0, le=12)
    execution: Literal["local", "modal", "docker"] = "local"
    trials: int = Field(default=1, ge=1, le=100, strict=True)
    concurrency: int = Field(default=1, ge=1, le=16, strict=True)
    time_limit: int | None = Field(default=None, ge=1, le=72000, strict=True)
    # False skips the LLM judges during the run (code checks still run); see docs/running.md for post-run judging.
    llm_grading: bool = False
    # Scenario YAML bundled with the source, discovered when the run is configured.
    scenario: str = "train_refund_pressure"

    @field_validator("scenario")
    @classmethod
    def known_scenario(cls, value: str) -> str:
        names = scenario_names()
        if value not in names:
            raise ValueError(f"Unknown scenario {value!r}; choose one of: {', '.join(names)}")
        return value

    # Continue an earlier trial from its .eval log (one model, one trial, the same target model).
    resume_log: str | None = Field(default=None, max_length=1000, pattern=r"\.eval$|snapshot\.json$")
    # A saved Gmail snapshot (.secrets/inboxes/<name>.json) instead of the bundled inbox.
    inbox_snapshot: str | None = Field(default=None, pattern=SNAPSHOT_NAME_PATTERN)

    @model_validator(mode="before")
    @classmethod
    def scenario_defaults(cls, values):
        return scenario_budget_defaults(values)

    @model_validator(mode="after")
    def validate_cost_limits(self):
        resolve_cost_limits(self.cost_limit, self.models or [self.model], scenario=self.scenario)
        return self

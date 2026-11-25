"""Shared defaults and validated settings for newly launched runs."""

from __future__ import annotations

import os
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from refund_eval.scenario.schema import (
    archived_scenario_names,
    load_scenario,
    scenario_names,
)
from refund_eval.world.snapshot_store import SNAPSHOT_NAME_PATTERN

# Role defaults; the target is chosen per run. The grader role checks the simulator's
# financial consistency; claim evaluation runs separately after the trial.
DEFAULT_ROLE_MODEL = "openrouter/anthropic/claude-opus-5.5"
DEFAULT_GRADER_MODEL = "openrouter/anthropic/claude-haiku-4.5"
DEFAULT_TARGET_MODEL = "openrouter/anthropic/claude-opus-5"
# Hybrid search is keyless unless this is set, so an existing EXA_API_KEY never
# silently switches comparisons to billed, differently rate-limited search.
HYBRID_EXA_KEY_ENV = "REFUND_EVAL_EXA_API_KEY"
# Optional transcript storage; see refund_eval/storage.
SUPABASE_URL_ENV = "SUPABASE_URL"
SUPABASE_KEY_ENV = "SUPABASE_SERVICE_ROLE_KEY"
LATE_REFUND_SCENARIO = "train_refund_late"
LATE_REFUND_TOKEN_BUDGET = 200_000_000
CostCap = Annotated[float, Field(gt=0, le=1000, strict=True, allow_inf_nan=False)]
CostLimit = CostCap | dict[str, CostCap]
# When the target sees the runtime notice of time and tokens left (with a token budget).
BudgetNotice = Literal["every_step", "daily", "low"]
BUDGET_NOTICES: tuple[str, ...] = get_args(BudgetNotice)


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
    """No implicit late-refund dollar cap; freeze its dossiers, set its target budget and restrict research modes."""
    if isinstance(values, dict):
        scenario = values.get("scenario", LATE_REFUND_SCENARIO)
        if "sim_weeks" not in values and scenario in scenario_names():
            values = {**values, "sim_weeks": load_scenario(scenario).sim_weeks}
    if isinstance(values, dict) and values.get("scenario", LATE_REFUND_SCENARIO) == LATE_REFUND_SCENARIO:
        values = dict(values)
        if "cost_limit" not in values:
            raise ValueError("train_refund_late requires an explicit cost_limit for each target model")
        resuming = bool(values.get("resume_log") or values.get("resume_sha256"))
        if not resuming:
            values.setdefault("token_budget", LATE_REFUND_TOKEN_BUDGET)
            if values.get("token_budget") is None:
                raise ValueError("train_refund_late requires a token_budget")
        if "research_mode" in values and values["research_mode"] not in {"saved", "auto"}:
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
    # Runtime notice before every step, on the first step of each simulated day, or only
    # when under 20% of the tokens or time remains.
    budget_notice: BudgetNotice = "every_step"
    cost_limit: CostLimit = Field(default=1.0, description=(
        "Per-trial USD cap for target and world model calls in-process (Inspect cost_limit). "
        "A mapping selects the cap for each target model."
    ))
    sim_weeks: float = Field(default=6.0, gt=0, allow_inf_nan=False)
    execution: Literal["local", "modal", "docker"] = "local"
    trials: int = Field(default=1, ge=1, le=100, strict=True)
    concurrency: int = Field(default=1, ge=1, le=16, strict=True)
    time_limit: int | None = Field(default=None, ge=1, le=72000, strict=True)
    # Scenario YAML bundled with the source, discovered when the run is configured.
    scenario: str = LATE_REFUND_SCENARIO
    # Seed for simulated randomness (temperaments, reply timing); None draws one per trial.
    # Local runs only. Every trial records its seed in its world.
    seed: int | None = Field(default=None, ge=0, strict=True)

    # Continue an earlier trial from its .eval log (one model, one trial, the same target model).
    resume_log: str | None = Field(default=None, max_length=1000, pattern=r"\.eval$|snapshot\.json$")
    # A saved Gmail snapshot (.secrets/inboxes/<name>.json) instead of the bundled inbox.
    inbox_snapshot: str | None = Field(default=None, pattern=SNAPSHOT_NAME_PATTERN)
    identity_lock: str | None = Field(
        default=None, max_length=4096,
        description="Expected local Docker browser identity; unsupported for Harbor subprocess or native sidecar artifacts.",
    )

    @model_validator(mode="before")
    @classmethod
    def scenario_defaults(cls, values):
        return scenario_budget_defaults(values)

    @model_validator(mode="after")
    def validate_cost_limits(self):
        names = scenario_names()
        if self.scenario not in names and not (self.resume_log and self.scenario in archived_scenario_names()):
            raise ValueError(f"Unknown scenario {self.scenario!r}; choose one of: {', '.join(names)}")
        resolve_cost_limits(self.cost_limit, self.models or [self.model], scenario=self.scenario)
        return self

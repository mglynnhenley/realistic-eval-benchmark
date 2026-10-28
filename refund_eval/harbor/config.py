"""Validated, portable trial settings; no credentials or host paths."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from refund_eval.config import (
    DEFAULT_GRADER_MODEL,
    DEFAULT_ROLE_MODEL,
    BudgetNotice,
    CostLimit,
    RunConfig,
    resolve_cost_limits,
    scenario_budget_defaults,
)
from refund_eval.scenario.schema import scenario_names

# Where a Gmail snapshot sits in the trial image, relative to the source root (/app).
SNAPSHOT_IN_IMAGE = "inbox_snapshot.json"
RESUME_IN_IMAGE = "resume_trial.eval"
RESUME_SNAPSHOT_IN_IMAGE = "resume_trial.snapshot.json"


def batch_models(config: dict) -> list[str]:
    return config.get("models") or [config["model"]]


def batch_total(config: dict) -> int:
    return config["trials"] * len(batch_models(config))


class TrialSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    tool_profile: Literal["published"] = "published"
    data_mode: Literal["fixture", "hybrid"] = "hybrid"
    browser_runtime: Literal["subprocess"] = "subprocess"
    max_turns: int = Field(default=RunConfig.model_fields["max_turns"].default, ge=1)
    history_tokens: int = Field(default=RunConfig.model_fields["history_tokens"].default, ge=0, le=1_000_000)
    sim_weeks: float = Field(default=RunConfig.model_fields["sim_weeks"].default, gt=0)
    token_budget: int | None = Field(default=None, gt=0, strict=True)
    budget_notice: BudgetNotice = RunConfig.model_fields["budget_notice"].default
    cost_limit: CostLimit = 1.0
    time_limit: int = Field(default=1800, ge=1, le=72000)
    research_mode: Literal["auto", "live", "saved"] = "auto"
    simulator_model: str | None = None
    grader_model: str | None = None
    reasoning: bool = False  # ask OpenRouter to return the target's reasoning
    scenario: str = RunConfig.model_fields["scenario"].default

    @field_validator("scenario")
    @classmethod
    def known_scenario(cls, value: str) -> str:
        names = scenario_names()
        if value not in names:
            raise ValueError(f"Unknown scenario {value!r}; choose one of: {', '.join(names)}")
        return value

    # A Gmail snapshot shipped in the image as data; the hash pins its contents.
    inbox_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # An earlier trial's log shipped in the image to continue from; the hash pins it.
    resume_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="before")
    @classmethod
    def scenario_defaults(cls, values):
        return scenario_budget_defaults(values)



class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str
    model: str
    settings: TrialSettings
    trial_id: str

    @model_validator(mode="after")
    def validate_models(self):
        resolve_cost_limits(self.settings.cost_limit, [self.model], scenario=self.settings.scenario)
        if not self.model or self.model.startswith("mockllm/"):
            raise ValueError("Specify a real target model")
        if any(not model.startswith("openrouter/") for model in (
            self.model, self.settings.simulator_model or DEFAULT_ROLE_MODEL,
            self.settings.grader_model or DEFAULT_GRADER_MODEL,
        )):
            raise ValueError("The Harbor image supports OpenRouter models for all three roles")
        return self

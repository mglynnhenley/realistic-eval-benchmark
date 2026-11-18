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

try:
    from harbor.agents.options import AgentOptions
except ImportError:  # The benchmark environment and trial image do not install Harbor.
    class AgentOptions(BaseModel):
        """Harbor's shared agent options, so requests validate without Harbor installed."""

        model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

        base_url: str | None = None
        disable_web_search: bool = False
        reasoning_effort: str | None = None

# Where a Gmail snapshot sits in the trial image, relative to the source root (/app).
SNAPSHOT_IN_IMAGE = "inbox_snapshot.json"
RESUME_IN_IMAGE = "resume_trial.eval"
RESUME_SNAPSHOT_IN_IMAGE = "resume_trial.snapshot.json"


def batch_models(config: dict) -> list[str]:
    return config.get("models") or [config["model"]]


def batch_total(config: dict) -> int:
    return config["trials"] * len(batch_models(config))


# Batch trials run in parallel to measure variance; Harbor needs a finite timeout,
# unlike RunConfig's None (no local cap).
DEFAULT_TRIALS = 4
DEFAULT_CONCURRENCY = 2
DEFAULT_TIME_LIMIT = 1800


class TrialSettings(AgentOptions):
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
    time_limit: int = Field(default=DEFAULT_TIME_LIMIT, ge=1, le=72000)
    trial_deadlines: dict | None = None
    research_mode: Literal["auto", "live", "saved"] = "auto"
    simulator_model: str | None = None
    grader_model: str | None = None
    reasoning: bool = False  # ask OpenRouter to return the target's reasoning
    scenario: str = RunConfig.model_fields["scenario"].default
    seed: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("scenario")
    @classmethod
    def known_scenario(cls, value: str) -> str:
        names = scenario_names()
        if value not in names:
            raise ValueError(f"Unknown scenario {value!r}; choose one of: {', '.join(names)}")
        return value

    @model_validator(mode="after")
    def finite_lifecycle(self):
        if self.trial_deadlines is not None:
            from refund_eval.harbor.deadlines import validate_deadlines

            validate_deadlines(self.trial_deadlines, self.time_limit)
        return self

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

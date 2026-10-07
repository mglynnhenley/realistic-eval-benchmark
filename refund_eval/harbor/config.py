"""Validated, portable trial settings; no credentials or host paths."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from refund_eval.config import DEFAULT_GRADER_MODEL, DEFAULT_ROLE_MODEL, RunConfig

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
    data_mode: Literal["fixture", "hybrid", "live"] = "hybrid"
    browser_runtime: Literal["subprocess"] = "subprocess"
    max_turns: int = Field(default=RunConfig.model_fields["max_turns"].default, ge=1)
    history_tokens: int = Field(default=RunConfig.model_fields["history_tokens"].default, ge=0, le=1_000_000)
    sim_weeks: float = Field(default=RunConfig.model_fields["sim_weeks"].default, gt=0)
    cost_limit: float = Field(default=1, gt=0)
    time_limit: int = Field(default=1800, ge=1, le=72000)
    research_mode: Literal["auto", "live", "saved"] = "auto"
    simulator_model: str | None = None
    grader_model: str | None = None
    rubric_set: dict | None = None
    llm_grading: bool = RunConfig.model_fields["llm_grading"].default
    daily_allowance: float | None = Field(default=RunConfig.model_fields["daily_allowance"].default, gt=0)
    use_full_allowance: bool = False
    scenario: Literal["train_refund", "train_refund_pressure"] = RunConfig.model_fields["scenario"].default
    # A Gmail snapshot shipped in the image as data; the hash pins its contents.
    inbox_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # An earlier trial's log shipped in the image to continue from; the hash pins it.
    resume_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.data_mode == "live":
            raise ValueError("Live Gmail is local-only; Harbor supports fixture or hybrid data")
        if self.tool_profile == "published" and self.browser_runtime != "subprocess":
            raise ValueError("Harbor published trials require the packaged subprocess browser")
        return self


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str
    model: str
    settings: TrialSettings
    trial_id: str

    @model_validator(mode="after")
    def validate_models(self):
        if not self.model or self.model.startswith("mockllm/"):
            raise ValueError("Specify a real target model")
        if any(not model.startswith("openrouter/") for model in (
            self.model, self.settings.simulator_model or DEFAULT_ROLE_MODEL,
            self.settings.grader_model or DEFAULT_GRADER_MODEL,
        )):
            raise ValueError("The Harbor image supports OpenRouter models for all three roles")
        return self

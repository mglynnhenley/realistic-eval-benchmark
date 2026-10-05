"""Validated, portable trial settings; no credentials or host paths."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def batch_models(config: dict) -> list[str]:
    if config["mode"] == "demo":
        return ["mockllm/model"]
    return config.get("models") or [config["model"]]


def batch_total(config: dict) -> int:
    return config["trials"] * len(batch_models(config))


class TrialSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    smoke: bool = False
    max_turns: int = Field(default=20, ge=1)
    sim_weeks: float = Field(default=1, gt=0)
    cost_limit: float = Field(default=1, gt=0)
    time_limit: int = Field(default=1800, ge=1, le=72000)
    world_mode: Literal["baseline", "friction"] = "baseline"
    simulator_model: str | None = None
    grader_model: str | None = None
    rubric_set: dict | None = None

    @model_validator(mode="after")
    def validate_smoke(self):
        if self.smoke and (self.simulator_model or self.grader_model or self.world_mode != "baseline"):
            raise ValueError("Smoke mode uses scripted models and the baseline world")
        if self.smoke and self.max_turns < 12:
            raise ValueError("Smoke mode needs at least 12 turns to exercise the complete demo")
        return self


class TrialRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str
    model: str
    settings: TrialSettings
    trial_id: str

    @model_validator(mode="after")
    def validate_models(self):
        if self.settings.smoke:
            if self.model != "mockllm/model":
                raise ValueError("Smoke mode requires mockllm/model")
        elif not self.model or self.model.startswith("mockllm/"):
            raise ValueError("Use smoke mode for scripted trials, or specify a real target model")
        elif any(not model.startswith("openrouter/") for model in (
            self.model, self.settings.simulator_model or self.model,
            self.settings.grader_model or self.model,
        )):
            raise ValueError("The Harbor image supports OpenRouter models for all three roles")
        return self

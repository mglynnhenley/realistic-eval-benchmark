"""Run settings for one world-service trial; defaults are the Inspect task's."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from refund_eval.config import DEFAULT_GRADER_MODEL, DEFAULT_ROLE_MODEL, RunConfig
from refund_eval.runtime.events import CHECKIN_HOURS

DEFAULT_OUTPUT_DIR = "/var/log/world"


class WorldSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str = RunConfig.model_fields["scenario"].default  # name or YAML path
    seed: int | None = Field(default=None, ge=0)  # None draws one; either way it is recorded in the world
    sim_weeks: float = Field(default=RunConfig.model_fields["sim_weeks"].default, gt=0, le=12)
    max_turns: int = Field(default=RunConfig.model_fields["max_turns"].default, ge=1)
    checkin_hours: tuple[int, ...] = CHECKIN_HOURS
    research_mode: Literal["auto", "live", "saved"] = RunConfig.model_fields["research_mode"].default
    data_mode: Literal["fixture", "hybrid"] = RunConfig.model_fields["data_mode"].default
    inbox: str | None = None  # inbox JSON; None uses the scenario's bundled fixture inbox
    simulator_model: str = DEFAULT_ROLE_MODEL
    grader_model: str = DEFAULT_GRADER_MODEL
    company_simulator: str | None = None
    output_dir: str = DEFAULT_OUTPUT_DIR

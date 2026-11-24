"""Preview native Harbor batch budget allowances without preparing or launching a batch."""

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from refund_eval.config import (
    DEFAULT_GRADER_MODEL,
    DEFAULT_ROLE_MODEL,
    CostCap,
    CostLimit,
    resolve_cost_limits,
)
from refund_eval.scenario.schema import scenario_names

MAX_ATTEMPTS_PER_SCENARIO = 100
TARGET_MODEL_PATTERN = r"^openrouter/[A-Za-z0-9._:/-]+$"
ROLE_MODEL_PATTERN = r"^[A-Za-z0-9_-]+/[A-Za-z0-9._:/-]+$"

CAVEATS = [
    "These are native Harbor budget allowances, not predicted charges or a provider-wide enforced cap.",
    "A final in-flight world request may overshoot its per-attempt world allowance.",
    "Sandbox/compute, external tools, and post-run judging are excluded.",
    "Model availability and launch readiness are not checked.",
    "Multiple scenarios imply separate native batches; this preview does not prepare artifacts.",
    "This preview makes no provider calls and requires no API keys.",
]


class BatchPreviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)

    scenarios: list[str] = Field(min_length=1)
    models: list[
        Annotated[str, Field(min_length=1, max_length=160, pattern=TARGET_MODEL_PATTERN)]
    ] = Field(min_length=1, max_length=16)
    trials: int = Field(default=1, ge=1, strict=True)
    cost_limit: CostLimit
    world_cost_limit: CostCap
    simulator_model: Annotated[
        str, Field(min_length=1, max_length=160, pattern=ROLE_MODEL_PATTERN)
    ] = DEFAULT_ROLE_MODEL
    grader_model: Annotated[
        str, Field(min_length=1, max_length=160, pattern=ROLE_MODEL_PATTERN)
    ] = DEFAULT_GRADER_MODEL

    @field_validator("scenarios")
    @classmethod
    def active_unique_scenarios(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Choose each scenario only once")
        active = set(scenario_names())
        unknown = [scenario for scenario in value if scenario not in active]
        if unknown:
            raise ValueError(
                f"Unknown or inactive scenarios: {', '.join(unknown)}; "
                f"choose from: {', '.join(sorted(active))}"
            )
        return value

    @field_validator("models")
    @classmethod
    def unique_models(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("Choose each model only once")
        return value

    @model_validator(mode="after")
    def validate_attempts_and_cost_limits(self) -> Self:
        if self.trials * len(self.models) > MAX_ATTEMPTS_PER_SCENARIO:
            raise ValueError(
                f"Each scenario allows at most {MAX_ATTEMPTS_PER_SCENARIO} attempts "
                "(trials multiplied by models)"
            )
        for scenario in self.scenarios:
            resolve_cost_limits(self.cost_limit, self.models, scenario=scenario)
        return self


def _money(value: Decimal) -> str:
    return format(value, "f")


def build_plan(config: BatchPreviewConfig) -> dict:
    world_per_attempt = Decimal(str(config.world_cost_limit))
    target_total = Decimal(0)
    world_total = Decimal(0)
    rows = []

    for scenario in config.scenarios:
        caps = resolve_cost_limits(config.cost_limit, config.models, scenario=scenario)
        for model in config.models:
            target_per_attempt = Decimal(str(caps[model]))
            per_attempt_total = target_per_attempt + world_per_attempt
            row_target_total = target_per_attempt * config.trials
            row_world_total = world_per_attempt * config.trials
            row_total = per_attempt_total * config.trials
            target_total += row_target_total
            world_total += row_world_total
            rows.append({
                "scenario": scenario,
                "target_model": model,
                "repetitions": config.trials,
                "target_cap_per_attempt_usd": _money(target_per_attempt),
                "world_allowance_per_attempt_usd": _money(world_per_attempt),
                "per_attempt_total_usd": _money(per_attempt_total),
                "row_total_usd": _money(row_total),
            })

    attempts = len(config.scenarios) * len(config.models) * config.trials
    combined_total = target_total + world_total
    return {
        "currency": "USD",
        "scenarios": config.scenarios,
        "models": config.models,
        "trials_per_case_model": config.trials,
        "roles": {
            "simulator_model": config.simulator_model,
            "grader_model": config.grader_model,
        },
        "rows": rows,
        "totals": {
            "attempts": attempts,
            "target_allowance_usd": _money(target_total),
            "world_allowance_usd": _money(world_total),
            "combined_allowance_usd": _money(combined_total),
        },
        "caveats": CAVEATS,
    }


def format_plan(plan: dict) -> str:
    headers = [
        "Scenario",
        "Target model",
        "Repetitions",
        "Target USD/attempt",
        "World USD/attempt",
        "Combined USD/attempt",
        "Row total USD",
    ]
    rows = [
        [
            row["scenario"],
            row["target_model"],
            str(row["repetitions"]),
            row["target_cap_per_attempt_usd"],
            row["world_allowance_per_attempt_usd"],
            row["per_attempt_total_usd"],
            row["row_total_usd"],
        ]
        for row in plan["rows"]
    ]
    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    separator = "  ".join("-" * width for width in widths)
    table = [line, separator]
    table.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    totals = plan["totals"]
    output = [
        "Native Harbor budget allowance preview (USD)",
        f"Simulator: {plan['roles']['simulator_model']}",
        f"Grader:    {plan['roles']['grader_model']}",
        "",
        *table,
        "",
        f"Total attempts: {totals['attempts']}",
        f"Target allowance total:   ${totals['target_allowance_usd']}",
        f"World allowance total:    ${totals['world_allowance_usd']}",
        f"Combined allowance total: ${totals['combined_allowance_usd']}",
        "",
        "Caveats:",
        *(f"- {caveat}" for caveat in plan["caveats"]),
    ]
    return "\n".join(output)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True, help="JSON batch-preview configuration")
    parser.add_argument("--json", action="store_true", dest="json_output", help="print structured JSON")
    args = parser.parse_args(argv)
    try:
        config = BatchPreviewConfig.model_validate_json(args.config.read_bytes())
    except (OSError, ValidationError) as error:
        parser.error(str(error))
    plan = build_plan(config)
    print(json.dumps(plan, indent=2) if args.json_output else format_plan(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

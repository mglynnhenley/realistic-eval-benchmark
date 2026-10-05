"""Inspect task: the impossible refund (spec.md; plan.md section C).

    uv run inspect eval refund_eval/task.py --model openrouter/anthropic/claude-opus-5 -T trials=1

One sample = one trial; `trials` epochs of it. See docs/running.md.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ModelCost,
    ModelInfo,
    get_model,
    get_model_info,
    model_roles,
    set_model_cost,
    set_model_info,
)

from refund_eval.agent import adapters
from refund_eval.agent.harness import refund_agent
from refund_eval.model_pricing import register_openrouter_costs
from refund_eval.scenario.schema import load_scenario

# The target is the eval model (--model). These roles are used by the
# counterparty simulator and the graders (--model-role simulator=... etc).
MODEL_ROLES = ("simulator", "grader")
DEFAULT_ROLE_MODEL = "openrouter/anthropic/claude-opus-5"
# With a mockllm target (dry run) the roles default to mockllm too, so a dry
# run never calls a real API unless you ask it to with --model-role.
DRY_RUN_ROLE_MODEL = "mockllm/model"

# OpenRouter catalog prices in $ per million tokens (verified 2026-09-30).
# https://openrouter.ai/api/v1/models
# cost_limit needs cost data for
# every model and Inspect ships none for this one. Only applied when a model
# has no cost data, so --model-cost-config takes precedence.
DEFAULT_MODEL_COSTS: dict[str, ModelCost] = {
    DEFAULT_ROLE_MODEL: ModelCost(
        input=5.0, output=25.0, input_cache_write=6.25, input_cache_read=0.50
    ),
}
_FREE = ModelCost(input=0.0, output=0.0, input_cache_write=0.0, input_cache_read=0.0)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def resolve_scenario_arg(scenario: str) -> str:
    """A scenario name is left as is; a YAML path becomes absolute.

    `inspect eval` runs the task from the task file's directory, so a relative
    path is looked up in the current directory, then the project root."""
    path = Path(scenario)
    if not path.suffix:
        return scenario
    if not path.is_absolute():
        for base in (Path.cwd(), PROJECT_ROOT):
            if (base / path).exists():
                path = base / path
                break
    return str(path.resolve())


def _eval_model_name() -> str | None:
    """The model being evaluated, when called while Inspect resolves the task."""
    try:
        from inspect_ai.model._model import active_model
    except ImportError:
        active_model = None
    model = active_model() if active_model is not None else None
    if model is not None:
        return str(model)
    return os.environ.get("INSPECT_EVAL_MODEL")


def _is_mock(model: str | None) -> bool:
    return bool(model) and str(model).startswith("mockllm/")


def default_model_roles(eval_model: str | None) -> dict[str, str]:
    """Roles not already given with --model-role / eval(model_roles=...)."""
    given = model_roles()
    fallback = DRY_RUN_ROLE_MODEL if _is_mock(eval_model) else DEFAULT_ROLE_MODEL
    return {role: fallback for role in MODEL_ROLES if role not in given}


def _given_role_models() -> list[str]:
    """Model names assigned to roles at eval level."""
    names: list[str] = []
    for value in model_roles().values():
        for model in value if isinstance(value, list) else [value]:
            names.append(str(model))
    return names


def register_default_costs(models: Iterable[str], *, fetch_openrouter: bool = True) -> None:
    """Give cost_limit the prices it needs, without overriding any the user set."""
    models = list(models)
    for name, cost in DEFAULT_MODEL_COSTS.items():
        info = get_model_info(get_model(name, api_key="pricing-lookup", memoize=False))
        if info is not None and info.cost is None:
            set_model_cost(name, cost)
    for name in models:
        if _is_mock(name):
            info = get_model_info(name)
            if info is None or info.cost is None:
                set_model_info(
                    name, ModelInfo(organization="mockllm", model=name, cost=_FREE)
                )
    if fetch_openrouter:
        register_openrouter_costs(models)


@task
def impossible_refund(
    trials: int = 20,
    scenario: str = "train_refund",
    mail_source: str = "fixture",
    max_turns: int = 300,
    cost_limit: float | None = 40.0,
    sim_weeks: float = 12,
    message_limit: int | None = None,
    token_limit: int | None = None,
    time_limit: int | None = None,
    working_limit: int | None = None,
    rubric_set: dict | None = None,
    tool_profile: str = "legacy",
    published_config: str | None = None,
    published_data: str = "live",
    world_mode: str = "baseline",
    fixture_path: str | None = None,
    initial_mail_thread_ids: list[str] | None = None,
) -> Task:
    """The impossible refund.

    Args:
      trials: Number of trials (Inspect epochs of the single sample).
      scenario: Scenario name (refund_eval/scenario/<name>.yaml) or YAML path.
      mail_source: "fixture" (bundled test inbox) or "gmail" (the user's real inbox, read-only).
      max_turns: Per-trial cap on target model generations (enforced by the harness).
      cost_limit: Per-trial model spend cap in dollars (Inspect cost_limit;
        counts every model call in the trial, including the simulator).
      sim_weeks: Per-trial cap on simulated time.
      message_limit: Optional Inspect message limit.
      token_limit: Optional Inspect token limit.
      time_limit: Optional Inspect wall-clock limit in seconds.
      working_limit: Optional Inspect working-time limit in seconds.
      tool_profile: "legacy" or "published" MCP tool profile.
      published_data: "fixture" for local data, "hybrid" for local mail and live public web,
        or "live" for configured providers.
      world_mode: "baseline" or "friction" for a simulated world with service delays and follow-ups.
      fixture_path: Optional per-run inbox JSON for published tools with fixture data.
      initial_mail_thread_ids: Optional threads loaded through published tools before the first generation.
    """
    if trials < 1:
        raise ValueError("trials must be at least 1")
    if tool_profile not in {"legacy", "published"}:
        raise ValueError("tool_profile must be 'legacy' or 'published'")
    if published_data not in {"fixture", "hybrid", "live"}:
        raise ValueError("published_data must be 'fixture', 'hybrid' or 'live'")
    if published_data == "hybrid" and tool_profile != "published":
        raise ValueError("Hybrid data requires tool_profile='published'")
    if world_mode not in {"baseline", "friction"}:
        raise ValueError("world_mode must be 'baseline' or 'friction'")
    from refund_eval.agent.mail_context import validate_thread_ids

    initial_mail_thread_ids = validate_thread_ids(initial_mail_thread_ids)
    if initial_mail_thread_ids and (tool_profile != "published" or published_data not in {"fixture", "hybrid"}):
        raise ValueError("Initial mailbox context requires the published fixture or hybrid profile")
    if initial_mail_thread_ids and not fixture_path:
        raise ValueError("Initial mailbox context requires an explicit fixture_path")
    if fixture_path is not None:
        if tool_profile != "published" or published_data not in {"fixture", "hybrid"}:
            raise ValueError("fixture_path requires tool_profile='published' and published_data='fixture' or 'hybrid'")
        from refund_eval.published.profile import validate_fixture_path

        path = Path(fixture_path).expanduser()
        if not path.is_absolute() and not path.exists() and (PROJECT_ROOT / path).exists():
            path = PROJECT_ROOT / path
        fixture_path = validate_fixture_path(str(path), data_mode=published_data)
    if world_mode == "friction" and (
        (tool_profile == "published" and published_data not in {"fixture", "hybrid"})
        or (tool_profile == "legacy" and not mail_source.startswith("fixture"))
    ):
        raise ValueError("Realistic friction requires simulated data")
    if tool_profile == "published":
        from refund_eval.published.profile import (
            fixture_readiness,
            hybrid_readiness,
            readiness,
        )

        status = (fixture_readiness() if published_data == "fixture" else
                  hybrid_readiness() if published_data == "hybrid" else readiness(published_config))
        if not status["configured"]:
            raise ValueError(status["reason"])
        mail_source = "gmail-fixture" if published_data in {"fixture", "hybrid"} else "gmail-mcp"
    scenario = resolve_scenario_arg(scenario)
    sc = load_scenario(scenario)

    eval_model = _eval_model_name()
    roles = default_model_roles(eval_model)
    register_default_costs(
        [m for m in [eval_model, *roles.values(), *_given_role_models()] if m],
        fetch_openrouter=cost_limit is not None,
    )

    if rubric_set is not None:
        from refund_eval.grading.behaviours import BehaviourSnapshot

        rubric_set = BehaviourSnapshot.model_validate(rubric_set).model_dump(mode="json")
    scorers = adapters.load_scorers() if rubric_set is None else adapters.load_scorers(rubric_set)

    return Task(
        dataset=[
            Sample(
                id=sc.id,
                input=sc.task_text,
                metadata={"scenario": scenario, "mail_source": mail_source,
                          **({"mail_data_mode": "fixture", "web_data_mode": "live", "web_time_basis": "wall_clock"}
                             if published_data == "hybrid" else {}),
                          **({"initial_mail_thread_ids": initial_mail_thread_ids} if initial_mail_thread_ids else {}),
                          **({"fixture_path": fixture_path} if fixture_path is not None else {}),
                          **({"tool_profile": tool_profile, "published_data": published_data}
                             if tool_profile != "legacy" else {}),
                          **({"world_mode": world_mode} if world_mode != "baseline" else {}),
                          **({"rubric_set": rubric_set} if rubric_set is not None else {})},
            )
        ],
        solver=refund_agent(
            scenario_id=scenario,
            mail_source_kind=mail_source,
            max_turns=max_turns,
            sim_weeks=sim_weeks,
            tool_profile=tool_profile,
            published_config=published_config,
            published_data=published_data,
            world_mode=world_mode,
            **({"initial_mail_thread_ids": initial_mail_thread_ids} if initial_mail_thread_ids else {}),
            **({"fixture_path": fixture_path} if fixture_path is not None else {}),
        ),
        scorer=scorers or None,
        epochs=trials,
        model_roles=roles or None,
        message_limit=message_limit,
        token_limit=token_limit,
        time_limit=time_limit,
        working_limit=working_limit,
        cost_limit=cost_limit,
        metadata={
            "scenario": scenario,
            "mail_source": mail_source,
            **({"mail_data_mode": "fixture", "web_data_mode": "live", "web_time_basis": "wall_clock"}
               if published_data == "hybrid" else {}),
            "max_turns": max_turns,
            "sim_weeks": sim_weeks,
            **({"initial_mail_thread_ids": initial_mail_thread_ids} if initial_mail_thread_ids else {}),
            **({"fixture_path": fixture_path} if fixture_path is not None else {}),
            **({"world_mode": world_mode} if world_mode != "baseline" else {}),
            **({"tool_profile": tool_profile, "published_data": published_data}
               if tool_profile != "legacy" else {}),
            **({"rubric_set": rubric_set} if rubric_set is not None else {}),
        },
    )

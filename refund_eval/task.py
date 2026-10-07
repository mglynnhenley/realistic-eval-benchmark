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
from refund_eval.agent.prompt import task_message
from refund_eval.config import (
    DEFAULT_GRADER_MODEL,
    DEFAULT_ROLE_MODEL,
    DEFAULT_TARGET_MODEL,
    RunConfig,
)
from refund_eval.model_pricing import register_openrouter_costs
from refund_eval.scenario.schema import load_scenario

# The target is the eval model (--model). These roles are used by the
# counterparty simulator and the graders (--model-role simulator=... etc).
MODEL_ROLES = ("simulator", "grader")
# With a mockllm target (dry run) the roles default to mockllm too, so a dry
# run never calls a real API unless you ask it to with --model-role.
DRY_RUN_ROLE_MODEL = "mockllm/model"

# OpenRouter catalog prices in $ per million tokens, from
# https://openrouter.ai/api/v1/models. cost_limit needs cost data for every
# model and Inspect ships none for these. Only applied when a model has no
# cost data, so --model-cost-config takes precedence.
DEFAULT_MODEL_COSTS: dict[str, ModelCost] = {
    # Verified 2026-09-30.
    DEFAULT_TARGET_MODEL: ModelCost(
        input=5.0, output=25.0, input_cache_write=6.25, input_cache_read=0.50
    ),
    # Verified 2026-10-05. The catalog charges double above 272k prompt
    # tokens; the budget counts the base rate.
    DEFAULT_ROLE_MODEL: ModelCost(
        input=10.0, output=50.0, input_cache_write=12.5, input_cache_read=1.0
    ),
    # Verified 2026-10-06.
    DEFAULT_GRADER_MODEL: ModelCost(
        input=1.0, output=5.0, input_cache_write=1.25, input_cache_read=0.10
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
    if _is_mock(eval_model):
        defaults = dict.fromkeys(MODEL_ROLES, DRY_RUN_ROLE_MODEL)
    else:
        defaults = {"simulator": DEFAULT_ROLE_MODEL, "grader": DEFAULT_GRADER_MODEL}
    return {role: defaults[role] for role in MODEL_ROLES if role not in given}


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
    trials: int = RunConfig.model_fields["trials"].default,
    scenario: str = "train_refund",
    mail_source: str = "fixture",
    max_turns: int = RunConfig.model_fields["max_turns"].default,
    history_tokens: int = RunConfig.model_fields["history_tokens"].default,
    cost_limit: float | None = RunConfig.model_fields["cost_limit"].default,
    sim_weeks: float = RunConfig.model_fields["sim_weeks"].default,
    message_limit: int | None = None,
    token_limit: int | None = None,
    time_limit: int | None = RunConfig.model_fields["time_limit"].default,
    working_limit: int | None = None,
    rubric_set: dict | None = None,
    tool_profile: str = RunConfig.model_fields["tool_profile"].default,
    published_config: str | None = None,
    published_data: str = RunConfig.model_fields["data_mode"].default,
    world_mode: str = "baseline",
    research_mode: str = RunConfig.model_fields["research_mode"].default,
    fixture_path: str | None = None,
    initial_mail_thread_ids: list[str] | None = None,
    browser_runtime: str = "docker",
    artifact_dir: str | None = None,
    llm_grading: bool = True,
    resume_log: str | None = None,
    daily_allowance: float | None = RunConfig.model_fields["daily_allowance"].default,
    use_full_allowance: bool = False,
) -> Task:
    """The impossible refund.

    Args:
      trials: Number of trials (Inspect epochs of the single sample).
      scenario: Scenario name (refund_eval/scenario/<name>.yaml) or YAML path.
      mail_source: Bundled fixture, or fixture:path alias; real Gmail uses published_data=live.
      max_turns: Per-trial cap on target model generations (enforced by the harness).
      history_tokens: Model-visible history in tokens; older messages are trimmed and the
        agent gets a memory tool. 0 shows the full history. The log always keeps it all.
      cost_limit: Per-trial model spend cap in dollars (Inspect cost_limit;
        counts every model call in the trial, including the simulator).
      sim_weeks: Per-trial cap on simulated time.
      daily_allowance: Worst-case dollars of model spend per simulated day (same calls as
        cost_limit). An agent that uses it up stops for the day and wakes at the next check-in.
      message_limit: Optional Inspect message limit.
      token_limit: Optional Inspect token limit.
      time_limit: Optional Inspect wall-clock limit in seconds.
      working_limit: Optional Inspect working-time limit in seconds.
      tool_profile: Published MCP tools; legacy execution returns a migration error.
      published_data: "fixture" for local data, "hybrid" for local mail and live public web,
        or "live" for configured providers.
      world_mode: "baseline", "friction" for service delays and follow-ups, or "open" where the simulator
        model decides how every organisation and person responds (never a full refund).
      research_mode: Open-world research on each organisation: "auto" uses a committed dossier when
        fresh and researches otherwise, "live" always researches, "saved" never searches.
      fixture_path: Optional per-run inbox JSON for published tools with fixture data.
      initial_mail_thread_ids: Optional threads loaded through published tools before the first generation.
    """
    if trials < 1:
        raise ValueError("trials must be at least 1")
    if browser_runtime not in {"docker", "subprocess"}:
        raise ValueError("browser_runtime must be docker or subprocess")
    if tool_profile != "published":
        raise ValueError("Legacy execution is retired. Use tool_profile=published with published_data=fixture or hybrid. Frozen legacy jobs require their original runner and source snapshot.")
    if published_data == "live" and browser_runtime != "docker":
        raise ValueError("Live Gmail is local-only; use fixture or hybrid data in a sandbox.")
    if published_data not in {"fixture", "hybrid", "live"}:
        raise ValueError("published_data must be 'fixture', 'hybrid' or 'live'")
    if published_data == "hybrid" and tool_profile != "published":
        raise ValueError("Hybrid data requires tool_profile='published'")
    if world_mode not in {"baseline", "friction", "open"}:
        raise ValueError("world_mode must be 'baseline', 'friction' or 'open'")
    if research_mode not in {"auto", "live", "saved"}:
        raise ValueError("research_mode must be 'auto', 'live' or 'saved'")
    if mail_source == "gmail":
        raise ValueError("Use published_data=live and the published Gmail configuration for real Gmail.")
    if mail_source.startswith("fixture:"):
        # Workers pass the bundled inbox alias on every run; it names an inbox only
        # when mail is simulated. Live data reads the configured Gmail provider.
        if published_data in {"fixture", "hybrid"} and fixture_path is None:
            fixture_path = mail_source.split(":", 1)[1]
    elif mail_source not in {"fixture", "gmail-fixture"}:
        raise ValueError(
            f"Unknown mail_source {mail_source!r}; use 'fixture', 'fixture:<path>' "
            "or published_data=live for real Gmail"
        )
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
    if world_mode in {"friction", "open"} and (
        published_data not in {"fixture", "hybrid"}
    ):
        raise ValueError(f"The {world_mode} world requires simulated data")
    if tool_profile == "published":
        from inspect_ai.model._model import active_model

        from refund_eval.published.provider import (
            cache_conversation_prefix,
            check_published_model,
        )

        if active_model() is not None:
            check_published_model(active_model())
        cache_conversation_prefix()
        from refund_eval.published.profile import (
            fixture_readiness,
            hybrid_readiness,
            readiness,
        )

        runtime_args = {"browser_runtime": browser_runtime} if browser_runtime != "docker" else {}
        status = (fixture_readiness(**runtime_args) if published_data == "fixture" else
                  hybrid_readiness(**runtime_args) if published_data == "hybrid" else readiness(published_config))
        if not status["configured"]:
            raise ValueError(status["reason"])
        mail_source = "gmail-fixture" if published_data in {"fixture", "hybrid"} else "gmail-mcp"
    scenario = resolve_scenario_arg(scenario)
    sc = load_scenario(scenario)

    eval_model = _eval_model_name()
    roles = default_model_roles(eval_model)
    register_default_costs(
        [m for m in [eval_model, *roles.values(), *_given_role_models()] if m],
        fetch_openrouter=cost_limit is not None or daily_allowance is not None,
    )

    if rubric_set is not None:
        from refund_eval.grading.behaviours import BehaviourSnapshot

        rubric_set = BehaviourSnapshot.model_validate(rubric_set).model_dump(mode="json")
    if not llm_grading:
        scorers = adapters.load_scorers(rubric_set, llm=False)
    else:
        scorers = adapters.load_scorers() if rubric_set is None else adapters.load_scorers(rubric_set)

    return Task(
        dataset=[
            Sample(
                id=sc.id,
                input=task_message(sc, sim_weeks),
                metadata={"scenario": scenario, "mail_source": mail_source,
                          **({"mail_data_mode": "fixture", "web_data_mode": "live", "web_time_basis": "wall_clock"}
                             if published_data == "hybrid" else {}),
                          **({"initial_mail_thread_ids": initial_mail_thread_ids} if initial_mail_thread_ids else {}),
                          **({"fixture_path": fixture_path} if fixture_path is not None else {}),
                          **({"tool_profile": tool_profile, "published_data": published_data}
                             if tool_profile == "published" else {}),
                          **({"world_mode": world_mode} if world_mode != "baseline" else {}),
                          **({"research_mode": research_mode} if research_mode != "auto" else {}),
                          **({"rubric_set": rubric_set} if rubric_set is not None else {})},
            )
        ],
        solver=refund_agent(
            scenario_id=scenario,
            mail_source_kind=mail_source,
            max_turns=max_turns,
            history_tokens=history_tokens,
            sim_weeks=sim_weeks,
            tool_profile=tool_profile,
            published_config=published_config,
            published_data=published_data,
            browser_runtime=browser_runtime,
            artifact_dir=artifact_dir,
            world_mode=world_mode,
            research_mode=research_mode,
            **({"initial_mail_thread_ids": initial_mail_thread_ids} if initial_mail_thread_ids else {}),
            **({"fixture_path": fixture_path} if fixture_path is not None else {}),
            **({"resume_log": resume_log} if resume_log else {}),
            **({"daily_allowance": daily_allowance} if daily_allowance is not None else {}),
            **({"use_full_allowance": True} if use_full_allowance else {}),
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
            "history_tokens": history_tokens,
            "sim_weeks": sim_weeks,
            **({"daily_allowance": daily_allowance} if daily_allowance is not None else {}),
            **({"initial_mail_thread_ids": initial_mail_thread_ids} if initial_mail_thread_ids else {}),
            **({"fixture_path": fixture_path} if fixture_path is not None else {}),
            **({"world_mode": world_mode} if world_mode != "baseline" else {}),
            **({"research_mode": research_mode} if research_mode != "auto" else {}),
            **({"tool_profile": tool_profile, "published_data": published_data}
               if tool_profile == "published" else {}),
            **({"rubric_set": rubric_set} if rubric_set is not None else {}),
        },
    )

"""Aggregate path-variance measurements by model.

All returned structures contain only JSON-native values.  This keeps the
module useful to the CLI without coupling it to JSON writing, plotting, or log
loading.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from itertools import combinations
from statistics import median, pstdev
from typing import TYPE_CHECKING, Any

from measurements.distance import (
    jaccard_distance,
    normalized_edit_distance,
    shared_prefix_length,
)

if TYPE_CHECKING:
    from measurements.model import Taxonomy, Trial

PathToken = tuple[str, Hashable]
_FAILED_STATUSES = frozenset({"aborted", "cancelled", "error", "failed"})


@dataclass(frozen=True)
class _PreparedTrial:
    """The small derived view needed to compare a trial with its peers."""

    path: tuple[PathToken, ...]
    organizations: frozenset[str]
    days: tuple[float | None, ...]


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first present attribute or mapping key from ``obj``."""

    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _steps(trial: Any) -> tuple[Any, ...]:
    value = _value(trial, "steps", "trajectory", "contacts", default=())
    return tuple(value or ())


def _organization(step: Any) -> str:
    value = _value(step, "organization", "org", "organization_name", default="")
    return str(value).strip() or "unknown"


def _taxonomy_entries(taxonomy: Any) -> Mapping[str, Any]:
    if isinstance(taxonomy, Mapping):
        return taxonomy
    entries = _value(
        taxonomy,
        "organizations",
        "entries",
        "classes",
        "by_organization",
        default={},
    )
    return entries if isinstance(entries, Mapping) else {}


def _classification(taxonomy: Any, organization: str) -> Any:
    """Resolve an organization while tolerating a Taxonomy lookup method."""

    for method_name in ("get", "classify", "for_organization"):
        method = getattr(taxonomy, method_name, None)
        if callable(method):
            try:
                result = method(organization)
            except (KeyError, TypeError):
                result = None
            if result is not None:
                return result

    entries = _taxonomy_entries(taxonomy)
    result = entries.get(organization)
    if result is None:
        result = entries.get(organization.casefold())
    return result


def _path_token(taxonomy: Any, organization: str) -> PathToken:
    classification = _classification(taxonomy, organization)
    route = str(_value(classification, "route", default="unclassified")).strip()
    stage = _value(classification, "stage", default="unclassified")
    if not route:
        route = "unclassified"
    if stage is None or stage == "":
        stage = "unclassified"
    # Classifier output should already be JSON-like, but coercing an unusual
    # mutable stage gives path tuples stable equality and hashing semantics.
    if not isinstance(stage, Hashable):
        stage = str(stage)
    return route, stage


def _as_day(value: Any) -> float | None:
    """Convert supported simulated-time representations into a numeric day."""

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp() / 86_400
    if isinstance(value, date):
        return float(value.toordinal())
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed.timestamp() / 86_400
    return None


def _day(step: Any) -> float | None:
    return _as_day(
        _value(step, "day", "simulated_day", "elapsed_days", "timestamp", "time")
    )


def _prepare(trial: Any, taxonomy: Any) -> _PreparedTrial:
    steps = _steps(trial)
    organizations = tuple(_organization(step) for step in steps)
    return _PreparedTrial(
        path=tuple(_path_token(taxonomy, org) for org in organizations),
        organizations=frozenset(organizations),
        days=tuple(_day(step) for step in steps),
    )


def _distribution(values: Sequence[float]) -> dict[str, Any]:
    """Describe observed pairwise values, using population SD.

    Population SD describes the complete set of pairs in the supplied rollouts;
    it also has the useful property of returning zero for a single pair.
    """

    if not values:
        return {
            "pair_count": 0,
            "mean": None,
            "sd": None,
            "min": None,
            "max": None,
            "range": None,
        }
    minimum = min(values)
    maximum = max(values)
    return {
        "pair_count": len(values),
        "mean": sum(values) / len(values),
        "sd": pstdev(values),
        "min": minimum,
        "max": maximum,
        "range": [minimum, maximum],
    }


def _is_failed(trial: Any) -> bool:
    explicit = _value(trial, "failed", "is_failed")
    if explicit is not None:
        return bool(explicit)
    status = _value(trial, "status", "stop_reason", default="")
    return str(status).casefold() in _FAILED_STATUSES


def _is_graded(trial: Any) -> bool:
    explicit = _value(trial, "graded", "is_graded")
    if explicit is not None:
        return bool(explicit)
    return _value(trial, "score", "grade") is not None


def _divergence(
    left: _PreparedTrial, right: _PreparedTrial
) -> tuple[int, float | None] | None:
    """Return one-based first-divergence step and its representative day."""

    if left.path == right.path:
        return None
    prefix_length = shared_prefix_length(left.path, right.path)
    candidate_days = [
        days[prefix_length]
        for days in (left.days, right.days)
        if prefix_length < len(days) and days[prefix_length] is not None
    ]
    divergence_day = median(candidate_days) if candidate_days else None
    return prefix_length + 1, divergence_day


def _route_stage_reach(
    trials: Sequence[_PreparedTrial],
) -> list[dict[str, Any]]:
    reached_by_trial: Counter[PathToken] = Counter()
    for trial in trials:
        reached_by_trial.update(set(trial.path))

    def sort_key(item: tuple[PathToken, int]) -> tuple[str, str]:
        (route, stage), _ = item
        return route.casefold(), str(stage)

    rollout_count = len(trials)
    return [
        {
            "route": route,
            "stage": stage,
            "rollout_count": count,
            "rollout_fraction": count / rollout_count if rollout_count else 0.0,
        }
        for (route, stage), count in sorted(reached_by_trial.items(), key=sort_key)
    ]


def summarize_model(trials: Iterable[Trial], taxonomy: Taxonomy) -> dict[str, Any]:
    """Summarize all rollouts for one model into JSON-serializable values."""

    sources = tuple(trials)
    prepared = tuple(_prepare(trial, taxonomy) for trial in sources)
    path_distances: list[float] = []
    organization_distances: list[float] = []
    divergence_steps: list[int] = []
    divergence_days: list[float] = []

    for left, right in combinations(prepared, 2):
        path_distances.append(normalized_edit_distance(left.path, right.path))
        organization_distances.append(
            jaccard_distance(left.organizations, right.organizations)
        )
        divergence = _divergence(left, right)
        if divergence is not None:
            step, day = divergence
            divergence_steps.append(step)
            if day is not None:
                divergence_days.append(day)

    return {
        "rollout_count": len(prepared),
        "graded_count": sum(_is_graded(trial) for trial in sources),
        "empty_count": sum(not trial.path for trial in prepared),
        "failed_count": sum(_is_failed(trial) for trial in sources),
        "pairwise_path_distance": _distribution(path_distances),
        "organization_jaccard_distance": _distribution(organization_distances),
        "median_divergence_step": (
            float(median(divergence_steps)) if divergence_steps else None
        ),
        "median_divergence_day": (
            float(median(divergence_days)) if divergence_days else None
        ),
        "distinct_paths": len({trial.path for trial in prepared}),
        "route_stage_reach": _route_stage_reach(prepared),
    }


def summarize(trials: Iterable[Trial], taxonomy: Taxonomy) -> dict[str, Any]:
    """Group trials by model and return their path-variance summaries.

    The top-level ``models`` object makes room for future run-wide metadata
    without changing the model-key namespace.
    """

    grouped: defaultdict[str, list[Any]] = defaultdict(list)
    for trial in trials:
        model = str(_value(trial, "model", "model_name", default="unknown"))
        grouped[model].append(trial)
    return {
        "models": {
            model: summarize_model(model_trials, taxonomy)
            for model, model_trials in sorted(grouped.items())
        }
    }


# Backwards-friendly descriptive alias for callers that prefer an explicit
# function name at the orchestration boundary.
summarize_trials = summarize

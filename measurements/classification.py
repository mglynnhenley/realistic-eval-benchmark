"""Build compact, task-specific evidence for organization classification.

This module deliberately knows nothing about Inspect or model providers.  Its only
job is to turn the immutable trajectory model into bounded, deterministic evidence
that a classifier (human, fake, or LLM-backed) can review.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .model import Trial


_MAX_EVIDENCE_CHARS = 800
_MAX_EVIDENCE_ITEMS = 24
_MAX_INTERACTION_EVIDENCE_ITEMS = 6
_MAX_INTERACTIONS_PER_ORGANIZATION = 8


def _clean(value: object, *, limit: int = _MAX_EVIDENCE_CHARS) -> str:
    """Collapse whitespace and bound untrusted transcript text."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _unique(
    values: Iterable[object], *, limit: int = _MAX_EVIDENCE_ITEMS
) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _clean(value)
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) == limit:
            break
    return tuple(result)


def _sample_interactions(
    values: Sequence[InteractionEvidence],
) -> tuple[InteractionEvidence, ...]:
    """Keep representative early/late evidence without making prompts grow forever."""
    limit = _MAX_INTERACTIONS_PER_ORGANIZATION
    if len(values) <= limit:
        return tuple(values)
    # Even sampling retains evidence from later trials rather than keeping only the
    # first rollout when one organization is contacted many times.
    indices = [round(index * (len(values) - 1) / (limit - 1)) for index in range(limit)]
    return tuple(values[index] for index in indices)


@dataclass(frozen=True, slots=True)
class InteractionEvidence:
    """One observed contact, with only the context useful for route inference."""

    trial_id: str
    channel: str
    elapsed_days: float
    repeat_count: int = 1
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "trial_id": self.trial_id,
            "channel": self.channel,
            "elapsed_days": round(self.elapsed_days, 4),
            "repeat_count": self.repeat_count,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class OrganizationEvidence:
    """All bounded evidence for one organization across the selected trials."""

    organization: str
    task: str
    task_evidence: tuple[str, ...] = ()
    interactions: tuple[InteractionEvidence, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "organization": self.organization,
            "task": self.task,
            "task_evidence": list(self.task_evidence),
            "interactions": [
                interaction.as_dict() for interaction in self.interactions
            ],
        }


def collect_organization_evidence(
    trials: Sequence[Trial],
    *,
    task: str | None = None,
) -> tuple[OrganizationEvidence, ...]:
    """Collect evidence jointly for every contacted organization.

    Organization display names are matched case-insensitively but retain the first
    spelling seen in the logs.  The result is sorted so prompts and caches do not
    depend on filesystem traversal order.
    """
    grouped: OrderedDict[str, dict[str, object]] = OrderedDict()
    task_names = _unique(getattr(trial, "task", "") for trial in trials)
    task_name = (
        _clean(task)
        if task
        else (task_names[0] if len(task_names) == 1 else " / ".join(task_names))
    )

    for trial in trials:
        trial_id = _clean(getattr(trial, "id", "unknown"), limit=160) or "unknown"
        task_context = tuple(getattr(trial, "task_evidence", ()) or ())
        shared_interaction_context = tuple(
            getattr(trial, "interaction_evidence", ()) or ()
        )
        for step in tuple(getattr(trial, "steps", ()) or ()):
            organization = _clean(getattr(step, "organization", ""), limit=200)
            if not organization:
                continue
            key = organization.casefold()
            bucket = grouped.setdefault(
                key,
                {
                    "organization": organization,
                    "task_evidence": [],
                    "interactions": [],
                },
            )
            bucket["task_evidence"].extend(task_context)  # type: ignore[union-attr]
            step_evidence = tuple(getattr(step, "evidence", ()) or ())
            bucket["interactions"].append(
                InteractionEvidence(  # type: ignore[union-attr]
                    trial_id=trial_id,
                    channel=_clean(getattr(step, "channel", "unknown"), limit=80)
                    or "unknown",
                    elapsed_days=float(getattr(step, "elapsed_days", 0.0) or 0.0),
                    repeat_count=max(1, int(getattr(step, "repeat_count", 1) or 1)),
                    evidence=_unique(
                        (*step_evidence, *shared_interaction_context),
                        limit=_MAX_INTERACTION_EVIDENCE_ITEMS,
                    ),
                )
            )

    result = [
        OrganizationEvidence(
            organization=str(bucket["organization"]),
            task=task_name,
            task_evidence=_unique(bucket["task_evidence"]),  # type: ignore[arg-type]
            interactions=_sample_interactions(bucket["interactions"]),  # type: ignore[arg-type]
        )
        for bucket in grouped.values()
    ]
    return tuple(
        sorted(
            result, key=lambda item: (item.organization.casefold(), item.organization)
        )
    )


# A short alias keeps callers readable while the long name remains self-documenting.
collect_evidence = collect_organization_evidence

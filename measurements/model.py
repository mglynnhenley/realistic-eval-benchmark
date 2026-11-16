"""Immutable domain models shared by the trajectory measurement modules.

This module deliberately contains no Inspect or application imports.  Keeping the
models as small frozen dataclasses makes the measuring code straightforward to
exercise with synthetic trajectories.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Step:
    """One outbound organization contact in a trial trajectory.

    Consecutive contacts to the same organization over the same channel are folded
    into one step.  ``time`` and ``elapsed_days`` refer to the first such contact;
    the remaining fields preserve the folded contacts for auditability.
    """

    organization: str
    channel: str
    time: datetime
    elapsed_days: float
    repeat_count: int = 1
    targets: tuple[str, ...] = ()
    interception_ids: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.organization.strip():
            raise ValueError("a step organization cannot be empty")
        if not self.channel.strip():
            raise ValueError("a step channel cannot be empty")
        if self.time.tzinfo is None:
            raise ValueError("step time must be timezone-aware")
        if self.elapsed_days < 0:
            raise ValueError("step elapsed_days cannot be negative")
        if self.repeat_count < 1:
            raise ValueError("step repeat_count must be at least one")

    @property
    def org(self) -> str:
        """Short alias useful in plotting and sequence code."""

        return self.organization


@dataclass(frozen=True, slots=True)
class Trial:
    """A self-contained trajectory extracted from one Inspect sample epoch."""

    id: str
    source: Path
    task: str
    task_id: str
    sample_id: str
    epoch: int
    model: str
    status: str
    graded: bool
    failed: bool
    task_evidence: tuple[str, ...]
    steps: tuple[Step, ...]
    interaction_evidence: tuple[str, ...] = ()
    outcome: str | None = None
    stop_reason: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("a trial id cannot be empty")
        if self.epoch < 1:
            raise ValueError("trial epoch must be at least one")

    @property
    def trial(self) -> int:
        """Return the Inspect epoch using the terminology used by reports."""

        return self.epoch

    @property
    def organizations(self) -> frozenset[str]:
        """Organizations contacted at least once in this trajectory."""

        return frozenset(step.organization for step in self.steps)


@dataclass(frozen=True, slots=True)
class OrgClass:
    """Task-specific route and escalation classification for an organization."""

    organization: str
    route: str
    stage: int
    role: str
    confidence: float
    rationale: str

    def __post_init__(self) -> None:
        if not self.organization.strip():
            raise ValueError("a classified organization cannot be empty")
        if not self.route.strip():
            raise ValueError("a route cannot be empty")
        if self.stage < 0:
            raise ValueError("an escalation stage cannot be negative")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("classification confidence must be between 0 and 1")

    @property
    def org(self) -> str:
        """Short alias for the organization key."""

        return self.organization


@dataclass(frozen=True, slots=True)
class Taxonomy:
    """Reviewed task-specific organization classifications.

    ``organizations`` is a tuple rather than a dictionary so the frozen object is
    deeply immutable and serializes predictably.  Lookup remains explicit and the
    expected number of organizations in an evaluation is small.
    """

    task: str
    organizations: tuple[OrgClass, ...]
    created_at: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        keys = [item.organization for item in self.organizations]
        if len(keys) != len(set(keys)):
            raise ValueError("taxonomy contains duplicate organizations")

    def get(self, organization: str) -> OrgClass | None:
        """Find an organization classification, or return ``None``."""

        return next(
            (item for item in self.organizations if item.organization == organization),
            None,
        )

    def classify(self, organization: str) -> OrgClass:
        """Return a classification, using an explicit unknown fallback.

        Unknown organizations are never guessed into a nearby route.  This is
        important when measuring newer logs against an older cached taxonomy.
        """

        existing = self.get(organization)
        if existing is not None:
            return existing
        return OrgClass(
            organization=organization,
            route="unclassified",
            stage=0,
            role="unclassified",
            confidence=0.0,
            rationale="Organization is absent from this taxonomy.",
        )

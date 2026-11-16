"""Read Inspect ``.eval`` logs into the measurement domain models.

This is intentionally the package's only Inspect-aware module.  It reads the
serialized world state directly and imports :mod:`refund_eval` only to merge a
native Harbor trial's agent log and world artifacts into one ``.eval``, allowing
the rest of ``measurements`` to remain independent of the evaluated application.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from inspect_ai.log import read_eval_log

from measurements.model import Step, Trial

_CONTACT_KIND = "contact"
_FORM_CHANNEL = "form"
_GENERIC_HOST_PARTS = frozenset(
    {
        "app",
        "co",
        "com",
        "contact",
        "forms",
        "gov",
        "info",
        "org",
        "support",
        "uk",
        "www",
    }
)


def discover_eval_logs(inputs: Iterable[str | Path]) -> tuple[Path, ...]:
    """Resolve files and directories to a deterministic set of ``.eval`` paths.

    Directories are searched recursively.  Invalid paths and non-``.eval`` files
    fail loudly, which prevents a typo from silently producing an empty report.
    """

    found: set[Path] = set()
    for raw_path in inputs:
        path = Path(raw_path).expanduser()
        if not path.exists():
            raise FileNotFoundError(path)
        if path.is_dir():
            # A native Harbor trial counts once, as its synthesized merged log.
            from refund_eval.runtime.trial_artifacts import eval_path, trial_sources

            found.update(eval_path(source) for source in trial_sources([path]))
        elif path.suffix == ".eval":
            found.add(path)
        else:
            raise ValueError(f"expected an .eval file or directory, got {path}")
    return tuple(sorted((path.resolve() for path in found), key=str))


def load_trials(inputs: Iterable[str | Path]) -> tuple[Trial, ...]:
    """Load every sample epoch from the supplied Inspect logs.

    A single Inspect log can contain several epochs, so the number of returned
    trials may exceed the number of files.  Empty, failed, and ungraded samples are
    retained rather than filtered out.
    """

    return tuple(
        trial for path in discover_eval_logs(inputs) for trial in load_eval_file(path)
    )


def load_eval_file(path: str | Path) -> tuple[Trial, ...]:
    """Load all sample epochs from one Inspect ``.eval`` file."""

    source = Path(path).expanduser().resolve()
    log = read_eval_log(str(source))
    samples = log.samples or []
    return tuple(_trial_from_sample(log, sample, source) for sample in samples)


def collapse_steps(steps: Sequence[Step]) -> tuple[Step, ...]:
    """Order steps by simulated time and fold adjacent org/channel duplicates."""

    collapsed: list[Step] = []
    for step in sorted(steps, key=lambda item: item.time):
        if (
            collapsed
            and collapsed[-1].organization == step.organization
            and collapsed[-1].channel == step.channel
        ):
            previous = collapsed[-1]
            collapsed[-1] = Step(
                organization=previous.organization,
                channel=previous.channel,
                time=previous.time,
                elapsed_days=previous.elapsed_days,
                repeat_count=previous.repeat_count + step.repeat_count,
                targets=previous.targets + step.targets,
                interception_ids=previous.interception_ids + step.interception_ids,
                evidence=previous.evidence + step.evidence,
            )
        else:
            collapsed.append(step)
    return tuple(collapsed)


def _trial_from_sample(log: Any, sample: Any, source: Path) -> Trial:
    store = _mapping(sample.store)
    world = _mapping(store.get("world"))
    start_time = _parse_time(world.get("start_time"))
    interactions = tuple(
        _mapping(item) for item in _sequence(world.get("interactions"))
    )
    contact_by_event = _contact_interactions_by_event(interactions)
    steps = _extract_steps(world, start_time, contact_by_event)

    sample_id = str(sample.id)
    epoch = int(sample.epoch)
    eval_spec = log.eval
    task_id = str(eval_spec.task_id)
    task = str(eval_spec.task)
    error = _error_text(sample.error)
    status = str(log.status)
    failed = error is not None or status in {"error", "cancelled"}
    metadata = _mapping(sample.metadata)

    return Trial(
        id=f"{task_id}:{sample_id}:{epoch}",
        source=source,
        task=task,
        task_id=task_id,
        sample_id=sample_id,
        epoch=epoch,
        model=str(eval_spec.model),
        status=status,
        graded=bool(sample.scores),
        failed=failed,
        task_evidence=_task_evidence(
            sample.input, metadata, _mapping(eval_spec.metadata)
        ),
        steps=steps,
        interaction_evidence=_interaction_evidence(interactions),
        outcome=_outcome_label(world),
        stop_reason=_optional_text(world.get("stop_reason")),
        error=error,
    )


def _extract_steps(
    world: Mapping[str, Any],
    start_time: datetime | None,
    contact_by_event: Mapping[str, Mapping[str, Any]],
) -> tuple[Step, ...]:
    if start_time is None:
        # A valid world normally has a start time.  Falling back to the first
        # interception still lets incomplete logs participate in measurements.
        times = [
            parsed
            for raw in _sequence(world.get("interceptions"))
            if (parsed := _parse_time(_mapping(raw).get("time"))) is not None
        ]
        start_time = min(times, default=None)

    steps: list[Step] = []
    for raw in _sequence(world.get("interceptions")):
        interception = _mapping(raw)
        event_id = str(interception.get("id", ""))
        interaction = contact_by_event.get(event_id)
        channel = str(interception.get("channel", "unknown"))
        target = _optional_text(interception.get("target"))

        organization = _interaction_organization(interaction)
        # Interactions are authoritative.  A form is also a meaningful outbound
        # organization contact when an interrupted/older simulator failed to emit
        # the corresponding interaction record.  Unmatched email/web actions can
        # be messages to the user or unrelated sites and are not route steps.
        if organization is None and channel == _FORM_CHANNEL and target is not None:
            organization = _organization_from_target(target)
        if organization is None:
            continue

        time = _parse_time(interception.get("time"))
        if time is None:
            continue
        elapsed_days = 0.0
        if start_time is not None:
            elapsed_days = max(0.0, (time - start_time).total_seconds() / 86_400)
        evidence = _evidence_for_interaction(interaction)
        steps.append(
            Step(
                organization=organization,
                channel=channel,
                time=time,
                elapsed_days=elapsed_days,
                targets=(target,) if target else (),
                interception_ids=(event_id,) if event_id else (),
                evidence=evidence,
            )
        )
    return collapse_steps(steps)


def _contact_interactions_by_event(
    interactions: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for interaction in interactions:
        event = _mapping(interaction.get("event"))
        if event.get("kind") != _CONTACT_KIND:
            continue
        event_id = str(interaction.get("id", "")).split(":", 1)[0]
        if event_id:
            result[event_id] = interaction
    return result


def _interaction_organization(interaction: Mapping[str, Any] | None) -> str | None:
    if interaction is None:
        return None
    organization = _optional_text(interaction.get("org"))
    return organization.strip() if organization else None


def _organization_from_target(target: str) -> str:
    """Create a stable organization key for an otherwise unmatched form target."""

    parsed = urlparse(target if "://" in target else f"//{target}")
    host = (parsed.hostname or target).lower().removeprefix("www.")
    parts = [part for part in re.split(r"[^a-z0-9]+", host) if part]
    meaningful = [part for part in parts if part not in _GENERIC_HOST_PARTS]
    key = meaningful[0] if meaningful else (parts[0] if parts else "unknown")
    return key.removeprefix("the") or key


def _task_evidence(
    sample_input: Any,
    sample_metadata: Mapping[str, Any],
    eval_metadata: Mapping[str, Any],
) -> tuple[str, ...]:
    evidence: list[str] = []
    input_text = _content_text(sample_input)
    if input_text:
        evidence.append(f"Task request: {input_text}")

    grading_notes = sample_metadata.get("task_grading_notes")
    if grading_notes:
        evidence.append(f"Task context: {_json_text(grading_notes)}")

    scenario = sample_metadata.get("scenario") or eval_metadata.get("scenario")
    if scenario:
        evidence.append(f"Scenario: {scenario}")
    return tuple(evidence)


def _interaction_evidence(interactions: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    evidence: list[str] = []
    for interaction in interactions:
        org = _optional_text(interaction.get("org")) or "unknown"
        time = _optional_text(interaction.get("time")) or "unknown time"
        event = _mapping(interaction.get("event"))
        kind = _optional_text(event.get("kind")) or "unknown event"
        context = _mapping(interaction.get("context"))
        org_context = _mapping(context.get("org"))
        parts = [f"{time} | {org} | {kind}"]
        for label, value in (
            ("name", org_context.get("name")),
            ("type", org_context.get("kind")),
            ("subject", context.get("subject")),
            ("decision", event.get("decision")),
            ("outgoing", event.get("outgoing")),
            ("incoming", event.get("incoming")),
        ):
            text = _optional_text(value)
            if text:
                parts.append(f"{label}: {text}")
        evidence.append(" | ".join(parts))
    return tuple(evidence)


def _evidence_for_interaction(interaction: Mapping[str, Any] | None) -> tuple[str, ...]:
    if interaction is None:
        return ()
    event = _mapping(interaction.get("event"))
    context = _mapping(interaction.get("context"))
    org_context = _mapping(context.get("org"))
    evidence: list[str] = []
    for label, value in (
        ("organization", org_context.get("name")),
        ("organization type", org_context.get("kind")),
        ("subject", context.get("subject")),
        ("decision", event.get("decision")),
        ("outbound contact", event.get("incoming")),
    ):
        text = _optional_text(value)
        if text:
            evidence.append(f"{label}: {text}")
    return tuple(evidence)


def _outcome_label(world: Mapping[str, Any]) -> str | None:
    outcome = _mapping(world.get("outcome"))
    transfers = [_mapping(item) for item in _sequence(outcome.get("transfers"))]
    if transfers:
        labels: list[str] = []
        for transfer in transfers:
            kind = _optional_text(transfer.get("kind")) or "transfer"
            currency = _optional_text(transfer.get("currency"))
            amount = transfer.get("amount")
            detail = ":".join(
                str(part) for part in (kind, currency, amount) if part is not None
            )
            labels.append(detail)
        return ",".join(labels)
    if outcome:
        return "no_transfer"
    return None


def _error_text(error: Any) -> str | None:
    if error is None:
        return None
    message = getattr(error, "message", None)
    return _optional_text(message) or _optional_text(error)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _content_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        parts = [_optional_text(getattr(item, "text", item)) for item in value]
        text = "\n".join(part for part in parts if part)
        return text or None
    return _optional_text(value)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()

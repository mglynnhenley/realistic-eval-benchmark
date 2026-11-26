"""Record and validate Harbor workflow timestamps without contacting Harbor."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

WORKFLOW = "harbor-request-to-result"
EVENTS = (
    "request_received",
    "authorization_confirmed",
    "config_frozen",
    "preparation_started",
    "preparation_completed",
    "launch_submitted",
    "job_created",
    "job_started",
    "trial_started",
    "trial_finished",
    "job_finished",
    "artifacts_collected",
    "result_verified",
    "analysis_handoff_completed",
)
ORDER_CONSTRAINTS = (
    ("request_received", "authorization_confirmed"),
    ("request_received", "config_frozen"),
    ("request_received", "preparation_started"),
    ("config_frozen", "preparation_started"),
    ("preparation_started", "preparation_completed"),
    ("preparation_completed", "launch_submitted"),
    ("authorization_confirmed", "launch_submitted"),
    ("launch_submitted", "job_created"),
    ("job_created", "job_started"),
    ("job_started", "trial_started"),
    ("trial_started", "trial_finished"),
    ("trial_finished", "job_finished"),
    ("job_finished", "artifacts_collected"),
    ("artifacts_collected", "result_verified"),
    ("result_verified", "analysis_handoff_completed"),
)
EVIDENCE_CLASSES = {"operator_capture", "redacted_summary", "synthetic_fixture"}
STATUSES = {"observed", "simulated", "missing"}
SOURCE_HASH = re.compile(r"^[0-9a-f]{64}$")
INTERVALS = (
    ("request_to_authorization", "request_received", "authorization_confirmed"),
    ("preparation", "preparation_started", "preparation_completed"),
    ("preparation_to_launch", "preparation_completed", "launch_submitted"),
    ("launch_to_job_created", "launch_submitted", "job_created"),
    ("job_queue", "job_created", "job_started"),
    ("job_to_trial_start", "job_started", "trial_started"),
    ("trial", "trial_started", "trial_finished"),
    ("trial_to_job_finish", "trial_finished", "job_finished"),
    ("job_finish_to_artifacts", "job_finished", "artifacts_collected"),
    ("artifacts_to_result", "artifacts_collected", "result_verified"),
    ("request_to_result", "request_received", "result_verified"),
    ("request_to_handoff", "request_received", "analysis_handoff_completed"),
)


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp must be an ISO-8601 UTC string ending in Z")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("timestamp must be a valid ISO-8601 UTC string ending in Z") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must have a UTC offset")
    return parsed


def _new_manifest(scenario: str, run_ref: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow": WORKFLOW,
        "evidence_class": "operator_capture",
        "scenario": scenario,
        "run_ref": run_ref,
        "events": {
            name: {
                "status": "missing",
                "timestamp_utc": None,
                "source": None,
                "note": "No timestamp captured yet.",
            }
            for name in EVENTS
        },
    }


def validate_manifest(manifest: Any) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest must be a JSON object"]

    errors = []
    allowed_keys = {
        "schema_version",
        "workflow",
        "evidence_class",
        "scenario",
        "run_ref",
        "source_evidence",
        "events",
    }
    unknown_keys = set(manifest) - allowed_keys
    if unknown_keys:
        errors.append("unknown manifest fields: " + ",".join(sorted(unknown_keys)))
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        errors.append("schema_version must be 1")
    if manifest.get("workflow") != WORKFLOW:
        errors.append(f"workflow must be {WORKFLOW}")
    evidence_class = manifest.get("evidence_class")
    if not isinstance(evidence_class, str) or evidence_class not in EVIDENCE_CLASSES:
        errors.append("evidence_class must be operator_capture, redacted_summary, or synthetic_fixture")
    for field in ("scenario", "run_ref"):
        if not isinstance(manifest.get(field), str) or not manifest[field].strip():
            errors.append(f"{field} must be a non-empty string")

    source_evidence = manifest.get("source_evidence")
    if evidence_class == "redacted_summary":
        if not isinstance(source_evidence, dict):
            errors.append("redacted_summary requires source_evidence")
        else:
            if not isinstance(source_evidence.get("reference"), str) or not source_evidence["reference"].strip():
                errors.append("source_evidence.reference must be a non-empty string")
            if not isinstance(source_evidence.get("sha256"), str) or not SOURCE_HASH.fullmatch(
                source_evidence["sha256"]
            ):
                errors.append("source_evidence.sha256 must be 64 lowercase hexadecimal characters")
    elif source_evidence is not None and not isinstance(source_evidence, dict):
        errors.append("source_evidence must be an object when provided")

    events = manifest.get("events")
    if not isinstance(events, dict):
        errors.append("events must be a JSON object")
        return errors
    event_names = set(events)
    unknown_events = event_names - set(EVENTS)
    missing_events = set(EVENTS) - event_names
    if unknown_events:
        errors.append("unknown events: " + ",".join(sorted(unknown_events)))
    if missing_events:
        errors.append("missing event entries: " + ",".join(name for name in EVENTS if name in missing_events))

    observed_count = 0
    for name in EVENTS:
        if name not in events:
            continue
        event = events[name]
        if not isinstance(event, dict):
            errors.append(f"events.{name} must be an object")
            continue
        allowed_event_keys = {"status", "timestamp_utc", "source", "note"}
        extra_event_keys = set(event) - allowed_event_keys
        absent_event_keys = allowed_event_keys - set(event)
        if extra_event_keys:
            errors.append(f"events.{name} has unknown fields: " + ",".join(sorted(extra_event_keys)))
        if absent_event_keys:
            errors.append(f"events.{name} is missing fields: " + ",".join(sorted(absent_event_keys)))

        status = event.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            errors.append(f"events.{name}.status must be observed, simulated, or missing")
            continue
        note = event.get("note")
        if not isinstance(note, str):
            errors.append(f"events.{name}.note must be a string")
        elif status == "missing" and not note.strip():
            errors.append(f"events.{name}.note is required when the event is missing")

        timestamp = event.get("timestamp_utc")
        source = event.get("source")
        if status == "missing":
            if timestamp is not None:
                errors.append(f"events.{name}.timestamp_utc must be null when missing")
            if source is not None:
                errors.append(f"events.{name}.source must be null when missing")
        else:
            if status == "observed":
                observed_count += 1
            try:
                _parse_utc(timestamp)
            except ValueError as exc:
                errors.append(f"events.{name}.timestamp_utc: {exc}")
            if not isinstance(source, str) or not source.strip():
                errors.append(f"events.{name}.source must be a non-empty string")

        if status == "simulated" and evidence_class != "synthetic_fixture":
            errors.append(f"events.{name} cannot be simulated outside a synthetic_fixture")
        if status == "observed" and evidence_class == "synthetic_fixture":
            errors.append(f"events.{name} cannot be observed in a synthetic_fixture")

    if evidence_class == "redacted_summary" and observed_count == 0:
        errors.append("redacted_summary must contain at least one observed event")
    return errors


def _load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("manifest must be a JSON object")
    return value


def _write_new_manifest(path: Path, manifest: dict[str, Any]) -> None:
    if not path.parent.is_dir():
        raise ValueError("manifest parent directory must already exist")
    content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)


def _replace_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    content = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _mark_event(
    path: Path, event_name: str, source: str, timestamp: str | None, note: str
) -> str:
    lock_path = path.with_name(f"{path.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            manifest = _load_manifest(path)
            errors = validate_manifest(manifest)
            if errors:
                raise ValueError("manifest_invalid: " + "; ".join(errors))
            if manifest["evidence_class"] != "operator_capture":
                raise ValueError("mark only updates operator_capture manifests")
            event = manifest["events"][event_name]
            if event["status"] != "missing":
                raise ValueError(f"event already recorded: {event_name}")
            if not source.strip():
                raise ValueError("source must be non-empty")
            if timestamp is None:
                timestamp = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
            _parse_utc(timestamp)
            event.update(status="observed", timestamp_utc=timestamp, source=source, note=note)
            errors = validate_manifest(manifest)
            if errors:
                raise ValueError("manifest_invalid_after_mark: " + "; ".join(errors))
            _replace_manifest(path, manifest)
            return timestamp
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _event_timestamp(event: dict[str, Any]) -> datetime | None:
    if event["status"] == "missing":
        return None
    return _parse_utc(event["timestamp_utc"])


def _timestamp_order(manifest: dict[str, Any]) -> tuple[list[str], int]:
    if manifest["evidence_class"] == "synthetic_fixture":
        return [], 0
    issues = []
    compared_pairs = 0
    events = manifest["events"]
    for earlier, later in ORDER_CONSTRAINTS:
        earlier_timestamp = _event_timestamp(events[earlier])
        later_timestamp = _event_timestamp(events[later])
        if earlier_timestamp is None or later_timestamp is None:
            continue
        compared_pairs += 1
        if later_timestamp < earlier_timestamp:
            issues.append(f"{earlier}->{later}")
    return issues, compared_pairs


def _interval_line(
    manifest: dict[str, Any], name: str, start: str, end: str
) -> str:
    if manifest["evidence_class"] == "synthetic_fixture":
        return f"interval.{name}=NOT_MEASURED_SYNTHETIC"
    events = manifest["events"]
    unavailable = [
        event_name for event_name in (start, end) if events[event_name]["status"] != "observed"
    ]
    if unavailable:
        return f"interval.{name}=UNAVAILABLE missing=" + ",".join(unavailable)
    duration = (_event_timestamp(events[end]) - _event_timestamp(events[start])).total_seconds()
    if duration < 0:
        return f"interval.{name}=REVIEW negative_duration_seconds={duration:.6f}"
    return f"interval.{name}=OBSERVED seconds={duration:.6f}"


def _report(manifest: dict[str, Any]) -> str:
    counts = {status: 0 for status in STATUSES}
    for event in manifest["events"].values():
        counts[event["status"]] += 1
    lines = [
        "validation=PASS",
        "evidence_class=" + manifest["evidence_class"],
        f"scenario={manifest['scenario']} run_ref={manifest['run_ref']}",
        f"events=observed:{counts['observed']} simulated:{counts['simulated']} missing:{counts['missing']}",
    ]
    source_evidence = manifest.get("source_evidence")
    if isinstance(source_evidence, dict) and source_evidence.get("sha256"):
        lines.append("source_sha256=" + source_evidence["sha256"])

    order_issues, order_pairs = _timestamp_order(manifest)
    if manifest["evidence_class"] == "synthetic_fixture":
        lines.append("timestamp_order=NOT_MEASURED_SYNTHETIC")
    elif order_pairs == 0:
        lines.append("timestamp_order=UNAVAILABLE observed_pairs=0")
    elif order_issues:
        lines.append(f"timestamp_order=REVIEW pairs={','.join(order_issues)} observed_pairs={order_pairs}")
    else:
        lines.append(f"timestamp_order=PASS observed_pairs={order_pairs}")
    lines.extend(_interval_line(manifest, *interval) for interval in INTERVALS)
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Record or validate Harbor workflow timestamps offline.")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a private operator-capture manifest")
    init.add_argument("--manifest", required=True, type=Path)
    init.add_argument("--scenario", required=True)
    init.add_argument("--run-ref", required=True)

    mark = commands.add_parser("mark", help="record one observed event")
    mark.add_argument("--manifest", required=True, type=Path)
    mark.add_argument("--event", required=True, choices=EVENTS)
    mark.add_argument("--source", required=True)
    mark.add_argument("--at-utc")
    mark.add_argument("--note", default="")

    validate = commands.add_parser("validate", help="validate a manifest and report observed intervals")
    validate.add_argument("manifest", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "init":
            if not args.scenario.strip() or not args.run_ref.strip():
                raise ValueError("scenario and run-ref must be non-empty")
            _write_new_manifest(args.manifest, _new_manifest(args.scenario, args.run_ref))
            print(f"manifest=CREATED path={args.manifest} evidence_class=operator_capture")
            return 0

        if args.command == "mark":
            timestamp = _mark_event(args.manifest, args.event, args.source, args.at_utc, args.note)
            print(f"event={args.event} status=observed timestamp_utc={timestamp}")
            return 0

        manifest = _load_manifest(args.manifest)
        errors = validate_manifest(manifest)
        if errors:
            print("validation=FAIL")
            for error in errors:
                print("error=" + error)
            return 1
        print(_report(manifest))
        return 0
    except (OSError, TypeError, ValueError) as exc:
        print("error=" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

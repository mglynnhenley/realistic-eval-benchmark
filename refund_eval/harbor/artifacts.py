"""Portable JSON and grading diagnostics shared by worker and verifier."""

import math


def finite_json(value):
    """Keep unknown numeric values unknown, using standard JSON null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite_json(item) for item in value]
    return value


def grading_errors(scores: dict) -> list[str]:
    errors = []
    for name, score in scores.items():
        metadata = score.get("metadata") or {}
        error = metadata.get("grader_error") or metadata.get("extraction_error")
        if not error and score.get("reason") == "grader_failed":
            error = "grader_failed"
        if error:
            errors.append(f"{name}: {error}")
    return errors

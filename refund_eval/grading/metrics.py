"""Inspect metrics shared by the scorers.

Each epoch is one trial, so every metric reads the unreduced per-trial scores and skips
unscored ("unknown") trials. Kept apart from the scorer modules so each can attach the
metrics at import time without importing the others.
"""

from __future__ import annotations

import math
from typing import Any

from inspect_ai.scorer import Metric, SampleScore, metric, value_to_float


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def _floats(scores: list[SampleScore]) -> list[float]:
    to_float = value_to_float()
    return [to_float(s.score.value) for s in scores if not _is_nan(s.score.value)]


@metric(scores="unreduced")
def trial_mean() -> Metric:
    """Mean over trials (unknown / unscored trials excluded)."""

    def compute(scores: list[SampleScore]) -> float:
        vals = _floats(scores)
        return sum(vals) / len(vals) if vals else 0.0

    return compute

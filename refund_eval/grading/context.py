"""Scenario lookup for saved-run grading and code scorers."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from refund_eval.scenario.schema import Scenario


@cache
def load_scenario_safe(scenario_id: str | None) -> Scenario | None:
    """The Scenario for a trial, or None if it cannot be loaded."""
    try:
        from refund_eval.scenario.schema import load_scenario

        return load_scenario(scenario_id or "train_refund")
    except (ImportError, OSError, ValueError, yaml.YAMLError):
        return None

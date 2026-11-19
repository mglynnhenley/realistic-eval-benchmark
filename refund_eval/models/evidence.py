"""Screen provider diagnostics with the live-web private-value guard."""

import json
from typing import Any

from refund_eval.published import REDACTED_SCENARIO_VALUE


def screened_error(value: Any) -> Any:
    if value is None:
        return None
    try:
        from refund_eval.published.privacy import contains_scenario_data
        from refund_eval.sim.common import current_scenario
    except ModuleNotFoundError as error:
        if error.name in {"refund_eval.published.privacy", "refund_eval.sim", "refund_eval.sim.common"}:
            return REDACTED_SCENARIO_VALUE
        raise
    if contains_scenario_data(json.dumps(value, ensure_ascii=False, default=str), current_scenario()):
        return REDACTED_SCENARIO_VALUE
    return value

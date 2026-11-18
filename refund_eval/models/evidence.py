"""Screen provider diagnostics with the live-web private-value guard."""

import json
from typing import Any

from refund_eval.published import REDACTED_SCENARIO_VALUE
from refund_eval.published.privacy import contains_scenario_data
from refund_eval.sim.common import current_scenario


def screened_error(value: Any) -> Any:
    if value is not None and contains_scenario_data(json.dumps(value, ensure_ascii=False, default=str), current_scenario()):
        return REDACTED_SCENARIO_VALUE
    return value

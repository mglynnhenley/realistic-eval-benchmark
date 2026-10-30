"""Read original provider charges from complete, validated frozen Scout scans."""

from __future__ import annotations

import json
import math
from decimal import Decimal


def saved_cost(logs, scans, *, judge, expected_trials):
    """Unknown or incomplete evidence retains the reservation; never estimate it."""
    from refund_eval.realism.coverage import scan_rows, validated_samples

    try:
        validated_samples(logs, scans, judge=judge, expected_trials=expected_trials)
        charges, identifiers = [], set()
        for row in scan_rows(scans, judge):
            if row.get("_parts_protocol") != "frozen-parts-v1":
                raise ValueError("Provider charge recovery requires frozen scan parts")
            events = row["scan_events"]
            events = json.loads(events) if isinstance(events, str) else events
            for event in events:
                if event.get("event") != "model":
                    continue
                response = event["call"]["response"]
                identifier, cost = response["id"], response["usage"]["cost"]
                if (not isinstance(identifier, str) or not identifier or identifier in identifiers
                        or type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0):
                    raise ValueError("Missing, duplicate or invalid original provider charge")
                identifiers.add(identifier)
                charges.append({"generation_id": identifier, "actual_usd": str(cost)})
        if not charges:
            raise ValueError("No original provider charges")
        total = sum((Decimal(row["actual_usd"]) for row in charges), Decimal(0))
        if not math.isfinite(float(total)):
            raise ValueError("Invalid aggregate provider charge")
        return {"cost_usd": float(total), "cost_usd_decimal": str(total), "charges": charges,
                "basis": "Complete frozen scan coverage and original raw provider response usage.cost"}
    except (ValueError, TypeError, KeyError, OSError) as error:
        return {"cost_usd": None, "reason": str(error)}

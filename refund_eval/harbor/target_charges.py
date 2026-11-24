"""Private receipts for OpenRouter responses received by the native Harbor target."""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

TARGET_CHARGES_DIRECTORY = "target-charge-receipts"
TARGET_CHARGES_ENV = "REFUND_EVAL_TARGET_CHARGES_DIR"
SOURCE = "openrouter_response_usage"


def _generation_id(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"gen-[a-zA-Z0-9_-]{1,128}", value) else None


def _amount(value: Any) -> Decimal | None:
    if type(value) not in (int, float, str):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 and math.isfinite(float(amount)) else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def _save(path: Path, response: Any = None, *, status: str) -> None:
    generation_id = _generation_id(_field(response, "id"))
    usage = _field(response, "usage")
    is_byok = _field(usage, "is_byok")
    cost = _amount(_field(usage, "cost")) if generation_id and (is_byok is None or is_byok is False) else None
    receipt = {
        "schema_version": 1,
        "attempt_id": path.stem,
        "status": status,
        "generation_id": generation_id,
        "actual_cost_usd": str(cost) if cost is not None else None,
        "source": SOURCE if cost is not None else None,
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextmanager
def record_target_charges(api: Any, directory: Path) -> Iterator[None]:
    from inspect_ai.model._providers.openrouter import OpenRouterAPI
    from openai import APIStatusError

    if (not isinstance(api, OpenRouterAPI) or api.base_url.rstrip("/") != "https://openrouter.ai/api/v1"
            or api.responses_api):
        yield
        return
    directory.mkdir(parents=True, exist_ok=True)
    generate = api._generate_completion

    async def recorded(request: dict[str, Any], config: Any) -> Any:
        path = directory / f"{uuid.uuid4()}.json"
        _save(path, status="pending")
        try:
            completion = await generate(request, config)
        except Exception as error:
            response = getattr(error, "body", None)
            if isinstance(error, APIStatusError):
                # The SDK's error body may omit the top-level generation id.
                with suppress(ValueError, RuntimeError):
                    response = error.response.json()
            _save(path, response, status="failed")
            raise
        _save(path, completion, status="response")
        return completion

    api._generate_completion = recorded
    try:
        yield
    finally:
        api._generate_completion = generate


def target_charge_summary(directory: Path | None) -> dict[str, Any]:
    paths = list(directory.glob("*.json")) if directory is not None else []
    charges: dict[str, Decimal] = {}
    unknown = 0
    for path in paths:
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = None
        generation_id = _generation_id(_field(receipt, "generation_id"))
        amount = _amount(_field(receipt, "actual_cost_usd"))
        if (generation_id is None or generation_id in charges or amount is None
                or _field(receipt, "schema_version") != 1 or _field(receipt, "attempt_id") != path.stem
                or _field(receipt, "status") not in ("response", "failed")
                or _field(receipt, "source") != SOURCE):
            unknown += 1
        else:
            charges[generation_id] = amount
    total = float(sum(charges.values())) if paths and not unknown else None
    return {
        "source": SOURCE,
        "attempt_count": len(paths),
        "unknown_attempt_count": unknown,
        "actual_cost_usd": total if total is not None and math.isfinite(total) else None,
    }

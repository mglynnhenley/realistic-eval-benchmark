"""Fill missing Inspect prices from OpenRouter's public model catalog."""

from __future__ import annotations

import math
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

import httpx
from inspect_ai.model import (
    ModelCost,
    ModelInfo,
    get_model,
    get_model_info,
    set_model_info,
)

CATALOG_URL = "https://openrouter.ai/api/v1/models"


def _per_million(pricing: dict, field: str) -> float:
    """OpenRouter quotes USD per token; Inspect expects USD per million."""
    value = pricing.get(field)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"missing or invalid {field} price")
    try:
        price = float(Decimal(str(value)) * 1_000_000)
    except (InvalidOperation, ValueError, OverflowError) as error:
        raise ValueError(f"invalid {field} price") from error
    if not math.isfinite(price) or price < 0:
        raise ValueError(f"invalid {field} price")
    return price


def _catalog_cost(entry: dict) -> ModelCost:
    pricing = entry.get("pricing")
    if not isinstance(pricing, dict):
        raise TypeError("missing pricing")
    input_price = _per_million(pricing, "prompt")
    # If no cache discount is advertised, count those tokens at full input price.
    return ModelCost(
        input=input_price,
        output=_per_million(pricing, "completion"),
        input_cache_read=(
            _per_million(pricing, "input_cache_read")
            if "input_cache_read" in pricing else input_price
        ),
        input_cache_write=(
            _per_million(pricing, "input_cache_write")
            if "input_cache_write" in pricing else input_price
        ),
    )


def register_openrouter_costs(models: Iterable[str]) -> None:
    """Fetch once for missing target/role prices; preserve configured prices.

    Runs during task construction, before Inspect validates cost_limit or makes
    model calls. Match exact catalog IDs, including any :free or other suffix.
    Never invent a zero price when a lookup fails.
    """
    missing: dict[str, ModelInfo | None] = {}
    for name in dict.fromkeys(models):
        if name.startswith("openrouter/"):
            # Model lookup also honours overrides under its canonical name.
            # The placeholder only constructs the client; it makes no requests.
            info = get_model_info(get_model(name, api_key="pricing-lookup", memoize=False))
            if info is None or info.cost is None:
                missing[name] = info
    if not missing:
        return

    try:
        # Public metadata only: no API key, prompts, or user information sent.
        response = httpx.get(CATALOG_URL, timeout=15.0)
        response.raise_for_status()
        payload = response.json()
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise TypeError("invalid model catalog")
    except (httpx.HTTPError, TypeError, ValueError) as error:
        raise ValueError(
            f"Could not load OpenRouter pricing for {', '.join(missing)}. "
            "Check your connection and retry, or configure prices with "
            "--model-cost-config. The budget limit is still required."
        ) from error

    catalog = {entry["id"]: entry for entry in entries
               if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
    costs = {}
    for name in missing:
        slug = name.removeprefix("openrouter/")
        if slug not in catalog:
            raise ValueError(
                f"No OpenRouter pricing found for {name}. Check the exact model ID "
                "in OpenRouter's catalog, or configure prices with --model-cost-config."
            )
        try:
            costs[name] = _catalog_cost(catalog[slug])
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Cannot use OpenRouter pricing for {name}: {error}. "
                "Configure prices with --model-cost-config before running with a budget."
            ) from error

    for name, cost in costs.items():
        info = missing[name] or ModelInfo(organization="openrouter", model=name)
        # Full requested name is also used when Inspect looks up a Model instance.
        # set_model_info works for new catalog entries absent from Inspect's DB.
        set_model_info(name, info.model_copy(update={"cost": cost}))

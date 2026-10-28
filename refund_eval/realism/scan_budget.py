"""Free, conservative planning for a single-attempt, unsegmented tells scan.

UTF-8 rendered prompt bytes upper-bound ordinary text tokenizer pieces. Add the
actual answer schema plus 16,384 tokens per transcript for chat/tool framing,
and reserve every permitted output token. Unknown tariffs, tiers, segmentation,
or retrying protocols are unsupported; reject rather than invent a cheap bound.
This planner does not launch models and is not a provider billing ceiling.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime

import httpx

OUTPUT_CAP = 8192
FRAMING_TOKENS = 16_384
# These mirror the explicitly registered scanner window, never expand it.
SUPPORTED_WINDOWS = {"anthropic/claude-opus-5.5": 600_000, "deepseek/deepseek-v4-pro": 700_000}


async def model_metadata(judge: str) -> dict:
    """Current official public model metadata; no API credential or paid request."""
    async with httpx.AsyncClient(timeout=30, transport=httpx.AsyncHTTPTransport(retries=0)) as client:
        response = await client.get("https://openrouter.ai/api/v1/models")
        response.raise_for_status()
    name = judge.removeprefix("openrouter/")
    found = next((entry for entry in response.json()["data"] if entry.get("id") == name), None)
    if found is None:
        raise ValueError("Current judge metadata is unavailable")
    return found


async def rendered_inputs(logs: list[str]) -> list[dict]:
    """Read saved messages and render the actual tells question/template/schema."""
    from inspect_scout import (
        AnswerStructured,
        TranscriptContent,
        messages_as_str,
        transcripts_from,
    )
    from inspect_scout._llm_scanner.generate import scanner_prompt

    from scout import scanners

    answer = AnswerStructured(type=scanners.Tells, answer_format=scanners.ANSWER_FORMAT, max_attempts=1)
    schema_bytes = len(json.dumps(scanners.Tells.model_json_schema(), ensure_ascii=False).encode())
    rows = []
    async with transcripts_from(logs).reader() as reader:
        async for info in reader.index():
            transcript = await reader.read(info, TranscriptContent(messages="all"))
            if transcript.events or transcript.timelines:
                raise ValueError("Timeline/event segmentation is unsupported by the scan reservation planner")
            text = await messages_as_str(transcript, preprocessor=scanners.PREPROCESSOR)
            question = scanners.tells_question(scanners.scenario_notes(transcript), scanners.scenario_intent(transcript))
            prompt = scanner_prompt(text, question, answer)
            rows.append({"source": info.source_uri, "transcript_id": info.transcript_id,
                         "rendered_input_bytes": len(prompt.encode("utf-8")), "schema_bytes": schema_bytes})
    return rows


def _price(pricing: dict, field: str, *, required: bool = True) -> float:
    if field not in pricing:
        if required:
            raise ValueError(f"Judge pricing lacks {field}")
        return 0.0
    value = pricing[field]
    # An absent optional tariff is allowed; an explicitly unknown tariff is
    # not a published zero. Never turn null/empty/boolean metadata into a bound.
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Invalid judge {field} pricing")
    try:
        price = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError(f"Invalid judge {field} pricing") from error
    if not math.isfinite(price) or price < 0:
        raise ValueError(f"Invalid judge {field} pricing")
    return price


def bounded_tells_config(max_tokens: int):
    """Bounded generation settings; the judge's SDK constructor also needs max_retries=0."""
    from inspect_ai.model import GenerateConfig

    if type(max_tokens) is not int or max_tokens <= 0 or max_tokens > OUTPUT_CAP:
        raise ValueError(f"Scanner output cap must be between 1 and {OUTPUT_CAP}")
    # No plugin is enabled, including OpenRouter's opt-in native web search.
    # Scout declares only its structured answer tool (no context/web tools).
    return GenerateConfig(cache_prompt=False, max_tokens=max_tokens, max_retries=0,
                          extra_body={"plugins": []})


def validate_saved_tells_scan(specification: dict, *, judge: str, max_tokens: int,
                             single_attempt: bool) -> None:
    """Require saved scans to match the bounded policy before evidence reuse.

    Missing settings are unknown, not safe defaults. Compare exact JSON types
    as well as values: False and 0 must not stand in for one another. This is
    consistency checking for trusted exports, not manifest authentication.
    """
    if single_attempt is not True:
        raise ValueError("Saved evidence requires an explicitly single-attempt scanner protocol")
    expected = bounded_tells_config(max_tokens).model_dump(exclude_none=True)
    if (not isinstance(judge, str) or not judge.startswith("openrouter/")
            or judge.removeprefix("openrouter/") not in SUPPORTED_WINDOWS):
        raise ValueError("Saved evidence uses an unsupported bounded judge")
    roles = specification.get("model_roles")
    role = roles.get("judge") if isinstance(roles, dict) else None
    if (not isinstance(role, dict) or set(roles) != {"judge"}
            or role.get("model") != judge):
        raise ValueError("Saved scans do not use the declared fixed judge")

    def matches(actual, wanted):
        return isinstance(actual, dict) and json.dumps(
            actual, sort_keys=True, allow_nan=False) == json.dumps(wanted, sort_keys=True, allow_nan=False)

    if not matches(role.get("config"), expected) or not matches(role.get("args"), {"max_retries": 0}):
        raise ValueError("Saved judge settings differ from the bounded scan policy")
    scanners = specification.get("scanners")
    tells = scanners.get("tells") if isinstance(scanners, dict) else None
    if (not isinstance(tells, dict) or set(scanners) != {"tells"} or tells.get("name") != "tells"
            or not matches(tells.get("params"), {"bounded": True})):
        raise ValueError("Saved scans lack the exact bounded tells scanner")


async def preflight(judge: str, *, max_tokens: int = OUTPUT_CAP,
                    single_attempt: bool = False) -> dict:
    """Check the fixed no-web judge protocol and public tariffs before trials.

    This does not know future transcript sizes; ``plan`` must still render and
    bound the actual saved logs before judging. No inference request is made.
    """
    if not single_attempt:
        raise ValueError("Scan planning requires an explicitly single-attempt scanner protocol")
    bounded_tells_config(max_tokens)
    name = judge.removeprefix("openrouter/")
    if not judge.startswith("openrouter/") or name not in SUPPORTED_WINDOWS:
        raise ValueError("Judge context/segmentation policy is unsupported by the reservation planner")
    metadata = await model_metadata(judge)
    pricing = metadata.get("pricing")
    if (not isinstance(pricing, dict)
            or ("overrides" in pricing and pricing["overrides"] != [])):
        raise ValueError("Unknown or tiered judge pricing is unsupported")
    input_rates = ("prompt", "input_cache_write", "input_cache_read", "input_cache_write_1h")
    known = {*input_rates, "completion", "overrides", "request", "image", "web_search", "internal_reasoning"}
    if set(pricing) - known:
        raise ValueError("Unknown judge tariff fields are unsupported")
    for field in ("request", "image", "internal_reasoning"):
        if _price(pricing, field, required=False):
            raise ValueError(f"Additional judge {field} tariff is unsupported")
    # https://openrouter.ai/docs/guides/features/plugins/web-search:
    # search is opt-in via a web plugin/tool or :online model variant. The
    # exact base-model allowlist and bounded_tells_config exclude those paths.
    # Validate and preserve the published price; do not pretend it is zero.
    _price(pricing, "web_search", required=False)
    # Scout's split-template call config can request caching even when the run
    # config disables it. Cover every published cache rate conservatively, even
    # the longer TTL, without inventing a zero for unknown cache-write pricing.
    prompt_price = max(_price(pricing, field, required=field in {"prompt", "input_cache_write"})
                       for field in input_rates)
    output_price = _price(pricing, "completion")
    try:
        official_window = int(metadata["context_length"])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("Unknown official judge context window") from error
    window = min(official_window, SUPPORTED_WINDOWS[name])
    if window <= 0:
        raise ValueError("Invalid official judge context window")
    return {"judge": judge, "max_tokens": max_tokens, "single_attempt": True,
            "context_window": window, "tariff": pricing, "prompt_price_bound": prompt_price,
            "output_price": output_price, "tariff_source": "https://openrouter.ai/api/v1/models",
            "tariff_applicability": {"web_search": {"applicable": False,
                "reason": "Fixed base model; plugins disabled; only the structured answer tool is declared"}},
            "planned_at": datetime.now(UTC).isoformat()}


async def plan(logs: list[str], judge: str, *, max_tokens: int = OUTPUT_CAP,
               single_attempt: bool = False) -> dict:
    """Bound actual logs for the same fixed protocol as ``bounded_tells_config``.

    Caller must bind that config directly to the judge model, disable its SDK
    constructor retries, use an exact supported base model, and use the bounded
    tells scanner with no additional tools. Legacy/unbounded scans are refused.
    """
    if not logs or len(set(logs)) != len(logs):
        raise ValueError("Distinct saved trial logs are required")
    checked = await preflight(judge, max_tokens=max_tokens, single_attempt=single_attempt)
    window = checked["context_window"]
    rows = await rendered_inputs(logs)
    if not rows or len({r["transcript_id"] for r in rows}) != len(rows):
        raise ValueError("Empty or duplicate rendered transcripts")
    required = 0.0
    for row in rows:
        bound = row["rendered_input_bytes"] + row["schema_bytes"] + FRAMING_TOKENS
        # Scout leaves a 5% tokenizer margin. This stricter 10% allowance and
        # explicit output cap ensure the text does not need multi-part scanning.
        if bound + max_tokens > int(window * .9):
            raise ValueError("Rendered input may require segmentation; no paid scan was authorised")
        cost = bound * checked["prompt_price_bound"] + max_tokens * checked["output_price"]
        row.update(input_token_bound=bound, output_token_bound=max_tokens, required_usd=cost)
        required += cost
    return {**checked, "required_usd": required, "framing_tokens_per_transcript": FRAMING_TOKENS,
            "per_transcript": rows}

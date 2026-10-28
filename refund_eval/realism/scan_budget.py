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
    if required and field not in pricing:
        raise ValueError(f"Judge pricing lacks {field}")
    try:
        price = float(pricing.get(field, 0) or 0)
    except (ValueError, TypeError) as error:
        raise ValueError(f"Invalid judge {field} pricing") from error
    if not math.isfinite(price) or price < 0:
        raise ValueError(f"Invalid judge {field} pricing")
    return price


async def plan(logs: list[str], judge: str, *, max_tokens: int = OUTPUT_CAP,
               single_attempt: bool = False) -> dict:
    """Return a reservation only for an explicitly single-attempt tells protocol.

    Caller must pass the same output cap and disable answer/refusal/API retries
    in Scout; the default refuses an unbounded legacy scanner configuration.
    """
    if not single_attempt:
        raise ValueError("Scan planning requires an explicitly single-attempt scanner protocol")
    if type(max_tokens) is not int or max_tokens <= 0 or max_tokens > OUTPUT_CAP:
        raise ValueError(f"Scanner output cap must be between 1 and {OUTPUT_CAP}")
    if not logs or len(set(logs)) != len(logs):
        raise ValueError("Distinct saved trial logs are required")
    name = judge.removeprefix("openrouter/")
    if name not in SUPPORTED_WINDOWS:
        raise ValueError("Judge context/segmentation policy is unsupported by the reservation planner")
    metadata = await model_metadata(judge)
    pricing = metadata.get("pricing")
    if not isinstance(pricing, dict) or pricing.get("overrides"):
        raise ValueError("Unknown or tiered judge pricing is unsupported")
    for field in ("request", "image", "web_search", "internal_reasoning"):
        if _price(pricing, field, required=False):
            raise ValueError(f"Additional judge {field} tariff is unsupported")
    # Scout's split-template call config can request caching even when the run
    # config disables it. Require the published write tariff and cover the larger
    # input rate; missing cache pricing must not silently become a zero premium.
    prompt_price = max(_price(pricing, "prompt"), _price(pricing, "input_cache_write"))
    output_price = _price(pricing, "completion")
    try:
        official_window = int(metadata["context_length"])
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("Unknown official judge context window") from error
    window = min(official_window, SUPPORTED_WINDOWS[name])
    if window <= 0:
        raise ValueError("Invalid official judge context window")
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
        cost = bound * prompt_price + max_tokens * output_price
        row.update(input_token_bound=bound, output_token_bound=max_tokens, required_usd=cost)
        required += cost
    return {"required_usd": required, "judge": judge, "max_tokens": max_tokens,
            "single_attempt": True, "framing_tokens_per_transcript": FRAMING_TOKENS,
            "context_window": window, "tariff": pricing, "prompt_price_bound": prompt_price,
            "output_price": output_price, "tariff_source": "https://openrouter.ai/api/v1/models",
            "planned_at": datetime.now(UTC).isoformat(), "per_transcript": rows}

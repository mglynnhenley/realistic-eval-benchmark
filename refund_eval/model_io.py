"""Model I/O shared by companies, runtime checks and offline evaluation."""

from __future__ import annotations

import json
import re
from typing import Any

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    ContentText,
    GenerateConfig,
    Model,
    ResponseSchema,
)
from inspect_ai.util import LimitExceededError, json_schema
from pydantic import BaseModel, ValidationError


def extract_json(text: str) -> Any:
    """Parse a JSON object from model output, tolerating code fences and prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError("no JSON object found in model output")


def parse_as[T: BaseModel](text: str, schema: type[T]) -> T:
    return schema.model_validate(extract_json(text))


async def generate_structured[T: BaseModel](
    model: Model,
    *,
    shared: str,
    task: str,
    schema: type[T],
    name: str,
    attempts: int = 2,
    system: str,
) -> tuple[T | None, str, str | None]:
    """Ask a model for a validated JSON object using the caller's system prompt.

    `shared` is reusable context; `task` is the instruction for this call.
    Returns (parsed, raw_output, error).
    """
    config = GenerateConfig(
        response_schema=ResponseSchema(name=name, json_schema=json_schema(schema)),
    )
    messages = [
        ChatMessageSystem(content=system),
        ChatMessageUser(content=[ContentText(text=shared), ContentText(
            text=task + "\n\nReturn only JSON matching this exact schema, including field types and enum values:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )]),
    ]
    raw = ""
    error: str | None = None
    for _ in range(max(1, attempts)):
        try:
            output = await model.generate(messages, config=config)
        except LimitExceededError:
            raise
        except Exception as ex:  # noqa: BLE001 - return API failures for the caller to handle
            error = f"model call failed: {type(ex).__name__}: {ex}"
            continue
        raw = output.completion
        try:
            return parse_as(raw, schema), raw, None
        except (ValueError, ValidationError) as ex:
            error = f"could not parse model output: {type(ex).__name__}: {str(ex)[:500]}"
            messages.extend([
                ChatMessageAssistant(content=raw),
                ChatMessageUser(content="Correct the previous JSON using the original schema. Validation errors:\n"
                                + str(ex)[:2000] + "\nReturn the corrected JSON object only."),
            ])
    return None, raw, error


def completion_json(text: str) -> dict:
    """Parse the first JSON object in a model completion (tolerates code fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])

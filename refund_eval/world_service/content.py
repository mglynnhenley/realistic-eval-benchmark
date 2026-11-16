"""What the model reads of an MCP tool result, without Inspect.

`model_content` is `published.adapter.model_content` with MCP blocks out. The world's tools
(`tools.NativeTool`) return it to the agent, and the verifier (`grading.world_records`) rebuilds
the same text from the tool audit's raw results, so the result is not stored twice.
"""

from __future__ import annotations

import json
from typing import Any


class ToolFailure(Exception):
    """A tool error the model is shown (Inspect's ToolError on the Inspect path)."""


def text_block(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def model_content(result: dict[str, Any]) -> list[dict[str, Any]]:
    """`published.adapter.model_content` with MCP blocks out: text and images as the model reads them."""
    parts: list[dict[str, Any]] = []
    for block in result.get("content", []):
        kind = block.get("type")
        if kind == "text":
            parts.append(text_block(block["text"]))
        elif kind == "image":
            parts.append({"type": "image", "data": block["data"], "mimeType": block["mimeType"]})
        elif kind == "resource" and "text" in block.get("resource", {}):
            parts.append(text_block(block["resource"]["text"]))
        elif kind == "resource_link":
            parts.append(text_block(json.dumps(block, ensure_ascii=False)))
        else:
            raise RuntimeError(f"Unsupported MCP content block: {kind!r}; raw result retained")
    if "structuredContent" in result:
        structured = result["structuredContent"]
        already_present = False
        for part in parts:
            if part["type"] == "text":
                try:
                    already_present |= json.loads(part["text"]) == structured
                except (ValueError, TypeError):
                    pass
        if not already_present:
            parts.append(text_block(json.dumps(structured, ensure_ascii=False)))
    if result.get("isError"):
        raise ToolFailure("\n".join(p["text"] for p in parts if p["type"] == "text") or "MCP tool error")
    return parts or [text_block("")]


def read_text(content: list[dict[str, Any]]) -> str:
    """What `_strings` reads from the adapter's content parts: the text of each, images as ''."""
    return " ".join(part.get("text", "") if part["type"] == "text" else "" for part in content)

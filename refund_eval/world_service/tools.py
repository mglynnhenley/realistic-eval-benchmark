"""The agent's world tools, called over MCP instead of as Inspect ToolDefs.

`NativeTool.call` does what `published.adapter._tool` does for the Inspect path: audit
the call, validate it against the native schema, charge the clock for writing it,
dispatch it to the mail or search service, audit the result, turn the MCP result into
what the model reads, and charge the clock for reading that. The same audit events
and clock charges are made, so the world comes out the same. The difference is the
return value: MCP content blocks, and `ToolFailure` (shown as an MCP error result)
where the adapter raises Inspect's ToolError.

`schedule_wakeup` is `agent.harness.schedule_wakeup` without Inspect: same name,
description and schema (a test checks them against the harness's ToolDef), same checks
and messages.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import jsonschema

from refund_eval.agent.schedule import LONDON
from refund_eval.published.adapter import (
    PUBLIC_SEARCH_PROVENANCE,
    MCPService,
    _strings,
    audit,
)
from refund_eval.published.mcp import MCPRPCError
from refund_eval.published.search import ExaPolicyDenied
from refund_eval.world.interception import charge_time
from refund_eval.world.state import WORLD_KEY, world_tx
from refund_eval.world.store import world_store
from refund_eval.world.timeutil import as_utc

SCHEDULE_WAKEUP = "schedule_wakeup"
SCHEDULE_WAKEUP_TOOL: dict[str, Any] = {
    "name": SCHEDULE_WAKEUP,
    "description": (
        "Choose when to resume after ending your turn. Earlier email can\n"
        "bring you back first, which clears the request. Without a request or\n"
        "pending email, you resume at the next morning check-in.\n"
        "Calling this again before pausing replaces it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"at": {"type": "string", "description": (
            "Future London local time in YYYY-MM-DD HH:MM format.\n"
            "Repeated autumn times use the earliest occurrence still in the\n"
            "future. Skipped spring times are rejected."
        )}},
        "required": ["at"],
        "additionalProperties": False,
    },
}


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


@dataclass
class NativeTool:
    service: MCPService
    descriptor: dict[str, Any]
    source: str
    live_web: bool = False
    validator: Any = field(init=False)

    def __post_init__(self) -> None:
        schema = self.descriptor["inputSchema"]
        self.validator = jsonschema.validators.validator_for(schema)(schema)

    @property
    def name(self) -> str:
        return self.descriptor["name"]

    def listing(self) -> dict[str, Any]:
        """What the model sees of the tool, as the Inspect path passes it on."""
        return {"name": self.name, "description": self.descriptor.get("description", ""),
                "inputSchema": self.descriptor["inputSchema"]}

    async def call(self, arguments: dict[str, Any], call_id: str | None) -> list[dict[str, Any]]:
        name, source = self.name, self.source
        accepts_id = "tool_call_id" in inspect.signature(self.service.call_tool).parameters
        audit({"event": "call", "source": source, "tool": name, "call_id": call_id, "arguments": arguments})
        try:
            self.validator.validate(arguments)
        except jsonschema.ValidationError as exc:
            audit({"event": "rejected", "source": source, "tool": name, "call_id": call_id,
                   "reason": "invalid_arguments"})
            raise ToolFailure(exc.message) from exc
        try:
            if world_store().get(WORLD_KEY) is not None:
                charge_time(name, written=_strings(arguments))
            result = await self.service.call_tool(
                name, arguments, **({"tool_call_id": call_id} if accepts_id else {}))
        except ExaPolicyDenied as exc:
            audit({"event": "rejected", "source": source, "tool": name, "call_id": call_id,
                   "reason": "policy_denied"})
            raise ToolFailure(str(exc)) from exc
        except MCPRPCError as exc:
            audit({"event": "rpc_error", "source": source, "tool": name, "call_id": call_id,
                   "error": exc.error, "rpc_response": exc.response})
            raise ToolFailure(str(exc)) from exc
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            recoverable = source == "exa" and (status is None or 400 <= status <= 599)
            audit({"event": "failure", "source": source, "tool": name, "call_id": call_id,
                   "error_type": type(exc).__name__,
                   **({"failure_kind": "transport", "recoverable": True, "status_code": status}
                      if recoverable else {})})
            if not recoverable:
                raise
            reason = f"HTTP {status}" if status is not None else type(exc).__name__
            raise ToolFailure(f"Exa search unavailable ({reason}). No search results were returned.") from exc
        except BaseException as exc:
            audit({"event": "failure", "source": source, "tool": name, "call_id": call_id,
                   "error_type": type(exc).__name__})
            raise
        audit({"event": "result", "source": source, "tool": name, "call_id": call_id, "result": result,
               **({"date_handling": "source_dates_preserved"} if self.live_web else {})})
        content = model_content(result)
        if self.live_web and source == "exa":
            content.append(text_block(PUBLIC_SEARCH_PROVENANCE))
            audit({"event": "search_provenance", "source": source, "tool": name,
                   "call_id": call_id, "notice": PUBLIC_SEARCH_PROVENANCE})
        if world_store().get(WORLD_KEY) is not None:
            charge_time(name, read=read_text(content), base=False)
        return content


def schedule_wakeup(arguments: dict[str, Any]) -> str:
    """Record the agent's requested wake-up, as the harness's schedule_wakeup tool does."""
    try:
        jsonschema.validate(arguments, SCHEDULE_WAKEUP_TOOL["inputSchema"])
    except jsonschema.ValidationError as exc:
        raise ToolFailure(exc.message) from exc
    at = arguments["at"]
    try:
        local = datetime.strptime(at, "%Y-%m-%d %H:%M").replace(tzinfo=LONDON)
    except ValueError as ex:
        raise ToolFailure("Use a London local time in YYYY-MM-DD HH:MM format.") from ex
    if local.strftime("%Y-%m-%d %H:%M") != at:
        raise ToolFailure("Use a London local time in YYYY-MM-DD HH:MM format.")
    candidates = [local.replace(fold=fold) for fold in (0, 1)]
    valid = [when for when in candidates
             if as_utc(when).astimezone(LONDON).strftime("%Y-%m-%d %H:%M") == at]
    if not valid:
        raise ToolFailure("That London local time does not exist because the clocks change. Choose another time.")
    with world_tx() as world:
        future = [when for when in valid if as_utc(when) > as_utc(world.now)]
        if not future:
            raise ToolFailure("The wake-up time must be after the current time.")
        when = min(future, key=as_utc)
        world.requested_wakeup = when
    zone = f" ({when:%Z})" if valid[0].utcoffset() != valid[-1].utcoffset() else ""
    return f"Wake-up scheduled for {when:%Y-%m-%d %H:%M} London time{zone}."

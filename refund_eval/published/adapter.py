"""The one model-independent bridge from native MCP catalogs to Inspect tools.

Full descriptors and CallToolResults are retained in the sample store. Inspect
performs its normal provider-specific encoding; unsupported schema constraints
are rejected at setup instead of silently disappearing from the tool contract.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import jsonschema
from inspect_ai.model import ContentImage, ContentText
from inspect_ai.tool import ToolDef, ToolError, ToolParams
from inspect_ai.util import store
from pydantic import ConfigDict

from refund_eval.published.mcp import MCPRPCError
from refund_eval.published.search import ExaPolicyDenied
from refund_eval.world.interception import charge_time, current_tool_call_id
from refund_eval.world.state import WORLD_KEY

AUDIT_KEY = "published_tool_audit"


class NativeParams(ToolParams):
    # Keep root dialect/annotation fields such as $schema. Provider adapters
    # serialize parameters directly; the separate audit also retains the full
    # descriptor independently of Inspect's log-model serialization.
    model_config = ConfigDict(extra="allow")


class MCPService(Protocol):
    async def list_tools(self) -> dict[str, Any]: ...
    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


def fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def audit(event: dict[str, Any]) -> None:
    """Append a JSON-safe event in the sample's private audit record."""
    events = list(store().get(AUDIT_KEY) or [])
    world = store().get("world") or {}
    events.append(
        copy.deepcopy(
            {
                "real_time": datetime.now(UTC).isoformat(),
                "sim_time": world.get("now"),
                **event,
            }
        )
    )
    store().set(AUDIT_KEY, events)


def _normalized(schema: Any, *, root: bool = False) -> Any:
    if isinstance(schema, list):
        return [_normalized(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    result = {k: _normalized(v) for k, v in schema.items()}
    if root:
        # These are JSON Schema's defaults, added by Inspect's ToolParams.
        result.setdefault("properties", {})
        result.setdefault("required", [])
        result.setdefault("additionalProperties", True)
    return result


def inspect_parameters(schema: dict[str, Any]) -> ToolParams:
    def reject_references(value):
        if isinstance(value, dict):
            if {"$ref", "$dynamicRef", "$recursiveRef"} & value.keys():
                raise ValueError(
                    "MCP schema references are unsupported; network resolution is disabled"
                )
            for child in value.values():
                reject_references(child)
        elif isinstance(value, list):
            for child in value:
                reject_references(child)

    reject_references(schema)
    if schema.get("type") != "object":
        raise ValueError("MCP tool inputSchema must be an object schema")
    jsonschema.validators.validator_for(schema).check_schema(schema)
    params = NativeParams.model_validate({"additionalProperties": True, **schema})
    rendered = params.model_dump(exclude_none=True)
    if _normalized(schema, root=True) != _normalized(rendered, root=True):
        raise ValueError(
            "Inspect cannot preserve this MCP inputSchema; refusing lossy conversion"
        )
    return params


def model_content(result: dict[str, Any]) -> list[ContentText | ContentImage]:
    """Translate MCP's content envelope without wrapping its native text."""
    parts: list[ContentText | ContentImage] = []
    for block in result.get("content", []):
        kind = block.get("type")
        if kind == "text":
            parts.append(ContentText(text=block["text"]))
        elif kind == "image":
            parts.append(
                ContentImage(image=f"data:{block['mimeType']};base64,{block['data']}")
            )
        elif kind == "resource" and "text" in block.get("resource", {}):
            parts.append(ContentText(text=block["resource"]["text"]))
        elif kind == "resource_link":
            parts.append(ContentText(text=json.dumps(block, ensure_ascii=False)))
        else:
            raise RuntimeError(
                f"Unsupported MCP content block: {kind!r}; raw result retained"
            )
    if "structuredContent" in result:
        structured = result["structuredContent"]
        already_present = False
        for part in parts:
            if isinstance(part, ContentText):
                try:
                    already_present |= json.loads(part.text) == structured
                except (ValueError, TypeError):
                    pass
        if not already_present:
            parts.append(ContentText(text=json.dumps(structured, ensure_ascii=False)))
    if result.get("isError"):
        raise ToolError(
            "\n".join(p.text for p in parts if isinstance(p, ContentText))
            or "MCP tool error"
        )
    return parts or [ContentText(text="")]


async def inspect_tools(service: MCPService, *, source: str) -> list[ToolDef]:
    catalog = await service.list_tools()
    audit(
        {
            "event": "catalog",
            "source": source,
            "sha256": fingerprint(catalog),
            "catalog": catalog,
        }
    )
    tools = []
    names: set[str] = set()
    for descriptor in catalog["tools"]:
        name = descriptor["name"]
        if name in names:
            raise ValueError(f"Duplicate MCP tool name: {name}")
        names.add(name)
        params = inspect_parameters(descriptor["inputSchema"])
        tools.append(_tool(service, descriptor, params, source))
    return tools


def _tool(
    service: MCPService, descriptor: dict, params: ToolParams, source: str
) -> ToolDef:
    name = descriptor["name"]
    accepts_id = "tool_call_id" in inspect.signature(service.call_tool).parameters
    validator = jsonschema.validators.validator_for(descriptor["inputSchema"])(
        descriptor["inputSchema"]
    )

    async def execute(**kwargs: Any):
        # Inspect recognizes this exact **kwargs: Any convention as a native
        # argument passthrough, avoiding coercion or insertion of defaults.
        arguments = kwargs
        call_id = current_tool_call_id()
        audit(
            {
                "event": "call",
                "source": source,
                "tool": name,
                "call_id": call_id,
                "arguments": arguments,
            }
        )
        try:
            validator.validate(arguments)
        except jsonschema.ValidationError as exc:
            audit(
                {
                    "event": "rejected",
                    "source": source,
                    "tool": name,
                    "call_id": call_id,
                    "reason": "invalid_arguments",
                }
            )
            raise ToolError(exc.message) from exc
        try:
            # Clock charging also delivers mail due during uninterrupted tool
            # use. Advance before dispatch so this call can observe that mail.
            # Catalog/adapter consumers outside a trial have no World to move.
            if store().get(WORLD_KEY) is not None:
                charge_time(name)
            result = await service.call_tool(
                name, arguments, **({"tool_call_id": call_id} if accepts_id else {})
            )
        except ExaPolicyDenied as exc:
            audit(
                {
                    "event": "rejected",
                    "source": source,
                    "tool": name,
                    "call_id": call_id,
                    "reason": "policy_denied",
                }
            )
            raise ToolError(str(exc)) from exc
        except MCPRPCError as exc:
            audit(
                {
                    "event": "rpc_error",
                    "source": source,
                    "tool": name,
                    "call_id": call_id,
                    "error": exc.error,
                    "rpc_response": exc.response,
                }
            )
            raise ToolError(str(exc)) from exc
        except httpx.HTTPError as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            recoverable = source == "exa" and (status is None or 400 <= status <= 599)
            audit({
                "event": "failure", "source": source, "tool": name, "call_id": call_id,
                "error_type": type(exc).__name__,
                **({"failure_kind": "transport", "recoverable": True, "status_code": status}
                   if recoverable else {}),
            })
            if not recoverable:
                raise
            # Runtime provider availability is a failed read, not an invented
            # result or an automatic retry. Startup discovery remains fatal.
            reason = f"HTTP {status}" if status is not None else type(exc).__name__
            raise ToolError(f"Exa search unavailable ({reason}). No search results were returned.") from exc
        except BaseException as exc:
            audit(
                {
                    "event": "failure",
                    "source": source,
                    "tool": name,
                    "call_id": call_id,
                    "error_type": type(exc).__name__,
                }
            )
            raise
        audit(
            {
                "event": "result",
                "source": source,
                "tool": name,
                "call_id": call_id,
                "result": result,
            }
        )
        return model_content(result)

    return ToolDef(
        execute,
        name=name,
        description=descriptor.get("description", ""),
        parameters=params,
        parallel=False,
        max_output=0,
    )

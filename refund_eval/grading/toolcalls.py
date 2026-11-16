"""Tool classes and the model's tool calls, read without Inspect.

`tool_calls()` takes the trial's messages either as Inspect `ChatMessage`s (while
scoring) or as their JSON form (`model_dump(mode="json")`, as saved in an `.eval` log
or a `transcript.json`) and gives the same records for both, so the code checks run
inside Inspect and in the separate Harbor verifier (`grading.verifier`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# --------------------------------------------------------------------------------------
# Tool classes. Update here if the tool set changes.
# --------------------------------------------------------------------------------------

LEGACY_READ_TOOLS: frozenset[str] = frozenset({"search_email", "read_email", "web_search", "web_fetch"})

LEGACY_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "send_email",
        "reply_email",
        "forward_email",
        "manage_email",
        "submit_web_form",
        "phone_call",
        "live_chat",
        "make_payment",
    }
)

# Native names are useful for rendering/counting; they never prove containment.
NATIVE_MAIL_READ_TOOLS = frozenset({"search_threads", "get_thread", "get_message", "list_labels"})
NATIVE_MAIL_WRITE_TOOLS = frozenset({"send_message", "reply"})
READ_TOOLS = LEGACY_READ_TOOLS | NATIVE_MAIL_READ_TOOLS | {"web_search_exa"}
WRITE_TOOLS = LEGACY_WRITE_TOOLS | NATIVE_MAIL_WRITE_TOOLS
BROWSER_TOOLS = frozenset({
    "browser_navigate", "browser_navigate_back", "browser_snapshot", "browser_click",
    "browser_type", "browser_fill_form", "browser_select_option", "browser_press_key",
    "browser_tabs", "browser_wait_for", "browser_evaluate", "browser_hover", "browser_drag",
    "browser_handle_dialog", "browser_close",
})

# Harness-local tools, added by the harness rather than a published service: the
# memory and wake-up tools keep notes and a requested time in the sample store.
# No network or native audit record; not external reads or writes.
LOCAL_TOOLS = frozenset({"memory", "schedule_wakeup"})

# Tool-call error types that mean the tool body never ran.
NOT_EXECUTED_ERRORS: frozenset[str] = frozenset({"parsing", "approval"})


def tool_kind(function: str) -> str:
    if function in WRITE_TOOLS:
        return "write"
    if function in READ_TOOLS:
        return "read"
    if function in BROWSER_TOOLS:
        return "mediated"
    if function in LOCAL_TOOLS:
        return "local"
    return "unknown"


def clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]} …[+{len(text) - limit} chars]"


# --------------------------------------------------------------------------------------
# Messages: Inspect objects or their JSON form
# --------------------------------------------------------------------------------------


def field(item: Any, name: str) -> Any:
    """`item.name` for an Inspect object, `item[name]` for its JSON form."""
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def role(message: Any) -> str | None:
    return field(message, "role")


def metadata(message: Any) -> dict:
    return field(message, "metadata") or {}


def text(message: Any) -> str:
    """`ChatMessage.text`: the content string, or its text parts joined by newlines."""
    if not isinstance(message, dict):
        return message.text
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content or []
                     if isinstance(part, dict) and part.get("type") == "text")


@dataclass
class ToolCallRecord:
    message_index: int
    id: str
    function: str
    arguments: dict[str, Any]
    result_index: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    result_text: str | None = None
    interrupted: bool = False  # synthetic resume result; actual completion unknown

    @property
    def kind(self) -> str:
        return tool_kind(self.function)

    @property
    def executed(self) -> bool:
        """True if the tool body ran (a result exists and it is not a parse/approval error)."""
        return (self.result_index is not None and self.error_type not in NOT_EXECUTED_ERRORS
                and not self.interrupted)

    @property
    def unresolved(self) -> bool:
        """No tool result (e.g. the trial stopped mid-call)."""
        return self.result_index is None or self.interrupted


def tool_calls(messages: Sequence[Any]) -> list[ToolCallRecord]:
    """Every tool call in the transcript, joined to its result message."""
    records: list[ToolCallRecord] = []
    by_id: dict[str, ToolCallRecord] = {}
    for i, m in enumerate(messages):
        kind = role(m)
        calls = field(m, "tool_calls") if kind == "assistant" else None
        if calls:
            for call in calls:
                parse_error = field(call, "parse_error")
                rec = ToolCallRecord(
                    message_index=i,
                    id=field(call, "id"),
                    function=field(call, "function"),
                    arguments=dict(field(call, "arguments") or {}),
                    error_type="parsing" if parse_error else None,
                    error_message=parse_error,
                )
                records.append(rec)
                by_id[rec.id] = rec
        elif kind == "tool" and field(m, "tool_call_id") in by_id:
            rec = by_id[field(m, "tool_call_id")]
            rec.result_index = i
            rec.result_text = text(m)
            rec.interrupted = metadata(m).get("harness") == "interrupted_tool"
            error = field(m, "error")
            if error is not None:
                rec.error_type = field(error, "type")
                rec.error_message = field(error, "message")
    return records

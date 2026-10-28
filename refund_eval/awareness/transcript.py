"""Inspect logs -> plain role/content chats an open model can replay, with source message indices."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from inspect_ai.log import read_eval_log
from inspect_ai.model import (
    ChatMessage,
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ContentReasoning,
)

from refund_eval.grading.transcript import (
    BROWSER_TOOLS,
    LEGACY_READ_TOOLS,
    LEGACY_WRITE_TOOLS,
    NATIVE_MAIL_READ_TOOLS,
    NATIVE_MAIL_WRITE_TOOLS,
)

# Channels to the companies (mail, phone, chat) vs the open web (search, fetch, browser, forms).
WEB_TOOLS: frozenset[str] = BROWSER_TOOLS | {
    "web_search", "web_fetch", "web_search_exa", "web_fetch_exa", "submit_web_form"}
MAIL_TOOLS: frozenset[str] = (
    NATIVE_MAIL_READ_TOOLS | NATIVE_MAIL_WRITE_TOOLS
    | ((LEGACY_READ_TOOLS | LEGACY_WRITE_TOOLS) - WEB_TOOLS - {"make_payment"})
)

# Reasoning some providers leave inline in the text; it is moved to the message's `reasoning` field.
_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)


def tool_factor(tool: str) -> str:
    """Realism factor of the tool whose result preceded a turn: company, internet or sandbox."""
    return "company" if tool in MAIL_TOOLS else "internet" if tool in WEB_TOOLS else "sandbox"


def _args(arguments: dict) -> str:
    return json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"), default=str)


def chat(messages: Sequence[ChatMessage]) -> list[dict]:
    """Strictly alternating role/content messages with tool calls and results inlined; an assistant message's
    reasoning (reasoning blocks and inline `<think>` text, redacted reasoning excluded) goes in `reasoning`."""
    out: list[dict] = []
    functions: dict[str, str] = {}
    seen_system = False
    for i, m in enumerate(messages):
        tool, reasoning = "", ""
        if isinstance(m, ChatMessageSystem):
            role, content = ("user" if seen_system else "system"), m.text.strip()
            seen_system = True
        elif isinstance(m, ChatMessageAssistant):
            role = "assistant"
            lines = [_THINK.sub("", m.text).strip()]
            thoughts = [c.reasoning for c in m.content if isinstance(c, ContentReasoning) and not c.redacted] \
                if isinstance(m.content, list) else []
            thoughts += [t.group(0).removeprefix("<think>").removesuffix("</think>") for t in _THINK.finditer(m.text)]
            reasoning = "\n\n".join(t.strip() for t in thoughts if t.strip())
            for call in m.tool_calls or []:
                functions[call.id] = call.function
                lines.append(f"[call {call.function}({_args(call.arguments)})]")
            content = "\n".join(line for line in lines if line)
        elif isinstance(m, ChatMessageTool):
            role = "user"
            tool = m.function or functions.get(m.tool_call_id or "", "") or "tool"
            text = m.text.strip() or (f"error: {m.error.message}" if m.error else "")
            content = f"[{tool} result]\n{text}".rstrip()
        else:
            role, content = "user", m.text.strip()
        if not content and not reasoning:
            continue
        if out and out[-1]["role"] == role:
            prev = out[-1]
            prev.update(content="\n\n".join(c for c in (prev["content"], content) if c), index=i,
                        tool=tool or prev["tool"])
            if reasoning:
                prev["reasoning"] = "\n\n".join(r for r in (prev.get("reasoning", ""), reasoning) if r)
        else:
            out.append({"role": role, "content": content, "index": i, "tool": tool,
                        **({"reasoning": reasoning} if role == "assistant" else {})})
    return out


def conversations(logs: list[str]) -> list[dict]:
    """One chat per sample (and epoch) in the given Inspect logs."""
    rows: list[dict] = []
    for log in logs:
        evaluation = read_eval_log(str(log))
        for sample in evaluation.samples or []:
            rows.append({"log": str(log), "sample_id": str(sample.id), "epoch": int(sample.epoch),
                         "target_model": evaluation.eval.model, "messages": chat(sample.messages)})
    return rows

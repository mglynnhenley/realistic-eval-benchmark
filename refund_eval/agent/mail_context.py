"""Optional, explicitly labelled mailbox context loaded before the first generation.

Reads use the same published tools as the target. They are setup actions, not
model choices, and remain paired with their real results in the transcript.
"""

from __future__ import annotations

import json
from uuid import uuid4

from inspect_ai.model import ChatMessageAssistant, ChatMessageTool, execute_tools
from inspect_ai.tool import ToolCall, ToolDef
from inspect_ai.util import LimitExceededError, store

from refund_eval.tools._base import DISPATCH_LIMIT_KEY

CONTEXT_METADATA = "startup_mail_context"


def validate_thread_ids(thread_ids: list[str] | None) -> list[str]:
    if thread_ids is None:
        return []
    if not isinstance(thread_ids, list) or any(not isinstance(t, str) or not t.strip() for t in thread_ids):
        raise ValueError("initial_mail_thread_ids must be a list of nonempty thread IDs")
    if len(set(thread_ids)) != len(thread_ids) or len(thread_ids) > 5:
        raise ValueError("initial_mail_thread_ids must contain at most five distinct threads")
    return list(thread_ids)


def expected_mail_context(scenario, fixture_path: str, thread_ids: list[str]) -> dict[str, dict[str, str]]:
    """Render the frozen fixture independently to verify full returned coverage."""
    from refund_eval.published.mail import _visible_id
    from refund_eval.world.mail import FixtureMailSource
    from refund_eval.world.timeutil import as_aware

    source = FixtureMailSource(fixture_path).for_scenario(scenario)
    result = {tid: {} for tid in thread_ids}
    for email in source.emails:
        tid = _visible_id(email.thread_id or email.id)
        if tid in result and as_aware(email.sent_at) <= as_aware(scenario.start_time):
            mid = _visible_id(email.id)
            if mid in result[tid]:
                raise ValueError("Duplicate message ID in initial mailbox context")
            result[tid][mid] = email.body
    if any(not messages for messages in result.values()):
        raise ValueError("Initial mailbox context is absent or not yet available in the frozen fixture")
    return result


def _full_thread(text: str, thread_id: str, expected: dict[str, str]) -> bool:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict) or data.get("isError"):
        return False
    if isinstance(data.get("structuredContent"), dict):
        data = data["structuredContent"]
    messages = data.get("messages")
    if data.get("id") != thread_id or not isinstance(messages, list) or len(messages) != len(expected):
        return False
    if any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in messages):
        return False
    returned = {m["id"]: m.get("plaintextBody") for m in messages}
    return bool(expected) and len(returned) == len(messages) and returned == expected


async def load_initial_mail(state, tools, thread_ids: list[str], *, expected_messages: dict[str, dict[str, str]]) -> None:
    """Fail before model generation if prescribed context cannot be read in full."""
    thread_ids = validate_thread_ids(thread_ids)
    if not thread_ids:
        return
    if set(expected_messages) != set(thread_ids) or any(not expected_messages[t] for t in thread_ids):
        raise ValueError("Initial mailbox context requires expected message bodies for every declared thread")
    definitions = [t if isinstance(t, ToolDef) else ToolDef(t) for t in tools]
    reads = [t for t in definitions if t.name == "get_thread"]
    if len(reads) != 1:
        raise ValueError("Initial mailbox context requires the published get_thread tool")
    for thread_id in thread_ids:
        previous = [m for m in state.messages if isinstance(m, ChatMessageAssistant)
                    and (m.metadata or {}).get(CONTEXT_METADATA)
                    and any(c.arguments.get("threadId") == thread_id for c in m.tool_calls or [])]
        if previous:
            ids = {c.id for m in previous for c in m.tool_calls or []}
            results = [m for m in state.messages if isinstance(m, ChatMessageTool) and m.tool_call_id in ids]
            if len(previous) != 1 or len(results) != 1 or results[0].error or not _full_thread(results[0].text, thread_id, expected_messages[thread_id]):
                raise ValueError("Incomplete or ambiguous initial mailbox context in resumed transcript")
            continue
        request = ChatMessageAssistant(
            source="input", metadata={CONTEXT_METADATA: True},
            content="Mailbox context loaded automatically at session start. This read was not chosen by the assistant.",
            tool_calls=[ToolCall(id="startup-mail-" + uuid4().hex, function="get_thread",
                                 arguments={"threadId": thread_id, "messageFormat": "FULL_CONTENT"})],
        )
        state.messages.append(request)
        messages, _ = await execute_tools(state.messages, reads, max_output=128_000)
        for message in messages:
            message.metadata = {**(message.metadata or {}), CONTEXT_METADATA: True}
        state.messages.extend(messages)
        pending_limit = store().get(DISPATCH_LIMIT_KEY)
        if pending_limit is not None:
            store().set(DISPATCH_LIMIT_KEY, None)
            raise LimitExceededError(**pending_limit)
        results = [m for m in messages if isinstance(m, ChatMessageTool)]
        if len(results) != 1 or results[0].error or not _full_thread(results[0].text, thread_id, expected_messages[thread_id]):
            raise ValueError("Initial mailbox context could not be loaded in full; target generation was not started")

"""Shared action context, interception log, and simulator dispatch.

Every write action follows the same order (FR-014, FR-019):
1. log an Interception in the World (saved before anything else happens);
2. call the simulator's dispatch() (imported lazily; nothing real is contacted);
3. store what the agent was shown in Interception.response_summary.
If dispatch() fails, the agent sees an ordinary service error and nothing is sent.
Inspect execution limits stop the trial and retain the intercepted action without
inventing a company reply.
"""

from __future__ import annotations

import inspect
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from inspect_ai.tool import ToolError
from inspect_ai.util import LimitExceededError, store

from refund_eval.scenario.schema import Scenario
from refund_eval.sim.interface import Inbound, SimResult
from refund_eval.world import clock
from refund_eval.world.guards import extract_urls
from refund_eval.world.mail import Mailbox
from refund_eval.world.state import Channel, Interception, world_tx

logger = logging.getLogger(__name__)

# Private per-sample state of the mail client (not part of the World contract).
CLIENT_KEY = "mail_client"
DISPATCH_LIMIT_KEY = "interception_execution_limit"


@dataclass
class ToolContext:
    scenario: Scenario
    mailbox: Mailbox

    @property
    def user_email(self) -> str:
        return self.scenario.user.email_addresses[0]

    @property
    def user_from(self) -> str:
        return f"{self.scenario.user.name} <{self.user_email}>"


# --------------------------------------------------------------------------- client state


def _client() -> dict[str, Any]:
    return dict(store().get(CLIENT_KEY) or {"opened": [], "link_urls": []})


def opened_ids() -> set[str]:
    return set(_client().get("opened", []))


def link_urls() -> list[str]:
    return list(_client().get("link_urls", []))


def remember_opened(email_id: str, bodies: list[str]) -> None:
    """Record an opened message and the links in mail the agent has seen."""
    data = _client()
    opened = set(data.get("opened", []))
    if email_id:
        opened.add(email_id)
    urls = set(data.get("link_urls", []))
    for body in bodies:
        urls.update(extract_urls(body))
    store().set(CLIENT_KEY, {"opened": sorted(opened), "link_urls": sorted(urls)})


def remember_links(bodies: list[str]) -> None:
    remember_opened("", bodies)


# --------------------------------------------------------------------------- interceptions


def new_id() -> str:
    """Opaque interception id. The simulator may build agent-visible ids (email and
    thread ids) from it, so it must look like an ordinary message id."""
    return uuid.uuid4().hex[:16]


def new_message_id() -> str:
    return uuid.uuid4().hex[:16]


def current_tool_call_id() -> str | None:
    """The id of the model tool call being executed, if we're inside one.

    Inspect opens a "tool" span for each call and records the ToolEvent (whose id
    is the tool call id) straight after the span begins, then runs the tool body
    inside that span. So: find our span's begin event, take the next ToolEvent."""
    from inspect_ai.event import SpanBeginEvent, ToolEvent
    from inspect_ai.log import transcript
    from inspect_ai.util import current_span_id

    span_id = current_span_id()
    if span_id is None:
        return None
    try:
        events = list(transcript().history.recent_events(200))
    except Exception:  # noqa: BLE001 - history unavailable: fall back to a fresh id
        return None
    for n in range(len(events) - 1, -1, -1):
        ev = events[n]
        if isinstance(ev, SpanBeginEvent) and ev.id == span_id:
            return next((e.id for e in events[n + 1 :] if isinstance(e, ToolEvent)), None)
    return None


def log_interception(
    *,
    channel: Channel,
    tool: str,
    target: str,
    payload: dict[str, Any],
    org: str | None,
    to_user: bool = False,
    response_summary: str | None = None,
    tool_call_id: str | None = None,
) -> Interception:
    # Background brokers carry a trusted ID from the active tool dispatch;
    # their inherited Inspect span may belong to an earlier tool call.
    call_id = tool_call_id or current_tool_call_id()
    if call_id:
        # Lets graders pair each model tool call with its interception(s) exactly.
        payload = {**payload, "tool_call_id": call_id}
    with world_tx() as w:
        icpt = Interception(
            id=new_id(),
            time=w.now,
            channel=channel,
            tool=tool,
            target=target,
            payload=payload,
            org=org,
            to_user=to_user,
            response_summary=response_summary,
        )
        w.interceptions.append(icpt)
    return icpt


def set_response(interception_id: str, text: str) -> None:
    with world_tx() as w:
        for i in reversed(w.interceptions):
            if i.id == interception_id:
                i.response_summary = text
                break


def charge_time(tool_name: str) -> None:
    """Advance simulated time by the cost of one call to `tool_name`."""
    with world_tx() as w:
        clock.charge(w, tool_name)


async def call_simulator(inbound: Inbound) -> SimResult:
    from refund_eval.sim.router import dispatch

    result = dispatch(inbound)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, dict):
        result = SimResult.model_validate(result)
    return result


def _note(data: dict[str, Any]) -> None:
    from inspect_ai.log import transcript

    try:
        transcript().info(data, source="interception")
    except (RuntimeError, LookupError, ValueError):  # pragma: no cover - never break a tool
        logger.debug("could not add transcript note", exc_info=True)


async def intercept(
    *,
    channel: Channel,
    tool: str,
    target: str,
    org: str | None,
    payload: dict[str, Any],
    body: str,
    failure_text: str,
    ok_text: str = "Done.",
    subject: str | None = None,
    fields: dict[str, Any] | None = None,
    in_reply_to: str | None = None,
    tool_call_id: str | None = None,
) -> tuple[Interception, SimResult | None, str]:
    """Log, dispatch to the simulator, record the reply. Returns the interception,
    the SimResult (None if dispatch failed) and the text to show the agent."""
    icpt = log_interception(channel=channel, tool=tool, target=target, payload=payload,
                            org=org, tool_call_id=tool_call_id)
    result: SimResult | None = None
    try:
        result = await call_simulator(
            Inbound(
                channel=channel,
                tool=tool,
                target=target,
                org=org,
                subject=subject,
                body=body,
                fields=fields or {},
                in_reply_to=in_reply_to,
                interception_id=icpt.id,
            )
        )
        shown = result.immediate_text or ok_text
    except LimitExceededError as ex:
        # Inspect records tool exceptions as tool results. Mark the stop before
        # reraising so the harness exits at its next continuation check, rather
        # than making another model call before checking the budget again.
        with world_tx() as world:
            if world.stop_reason is None:
                world.stop_reason = f"{ex.type}_limit"
        store().set(DISPATCH_LIMIT_KEY, {
            "type": ex.type, "value": ex.value, "limit": ex.limit, "message": ex.message,
        })
        _note({"dispatch_limit": ex.type, "interception_id": icpt.id, "tool": tool})
        raise
    except ToolError as ex:
        shown = ex.message
        result = SimResult(immediate_text=shown, kind="error")
    except Exception as ex:
        logger.exception("dispatch failed for %s %s", tool, icpt.id)
        _note({"dispatch_error": f"{type(ex).__name__}: {ex}", "interception_id": icpt.id, "tool": tool})
        shown = failure_text
    set_response(icpt.id, shown)
    return icpt, result, shown

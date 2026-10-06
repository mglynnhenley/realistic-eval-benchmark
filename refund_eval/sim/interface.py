"""Tools -> counterparty simulator boundary. SHARED CONTRACT: change only by agreement.

Published mail and every browser navigation write (a form submission on any site, a non-GET
navigation, or a page load carrying message fields) intercept the action, then call `dispatch()`
from refund_eval/sim/router.py with an Inbound. The simulator decides and writes the
reply, updates the world (cases, claims, offers, transfers, pending mail) and
returns a SimResult telling the tool what to show the agent right now.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from refund_eval.world.state import Channel


class Inbound(BaseModel):
    channel: Channel
    tool: str
    target: str  # email address, URL, phone number, chat org, payee
    org: str | None  # resolved org key, or None if unknown
    subject: str | None = None
    body: str  # the agent's message, form fields rendered as text, call script, etc.
    fields: dict = Field(default_factory=dict)  # raw form fields or payment details
    in_reply_to: str | None = None  # email id when replying
    interception_id: str


class SimResult(BaseModel):
    """What the tool returns to the agent immediately."""

    immediate_text: str  # e.g. "Email sent.", a form confirmation page, a call transcript
    decision: str | None = None  # policy engine decision key, for logging
    reply_scheduled: bool = False  # an email reply was queued in world.pending
    kind: Literal["ack", "page", "transcript", "chat", "error"] = "ack"


# Signature implemented in refund_eval/sim/router.py:
#
#   async def dispatch(inbound: Inbound) -> SimResult
#
# It must: load the world via world_tx(), resolve the scenario via
# refund_eval.scenario.schema.load_scenario(current scenario id stored in the
# sample metadata key "scenario"), never contact anything real, and return
# quickly for email (reply is scheduled, not immediate).

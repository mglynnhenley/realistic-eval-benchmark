"""The target agent's tools.

Reads are real (the user's mailbox, web search, public web pages behind the URL
guard). Every write is intercepted, logged in the World and answered by the
simulator; nothing leaves the sandbox. There is deliberately no tool that
reaches the user (FR-030).
"""

from __future__ import annotations

from inspect_ai.tool import Tool

from refund_eval.scenario.schema import Scenario
from refund_eval.tools._base import ToolContext
from refund_eval.tools.action_tools import (
    live_chat,
    make_payment,
    phone_call,
    submit_web_form,
)
from refund_eval.tools.mail_tools import (
    forward_email,
    manage_email,
    read_email,
    reply_email,
    search_email,
    send_email,
)
from refund_eval.tools.web_tools import make_web_search, web_fetch
from refund_eval.world.mail import Mailbox, MailSource, bind_mail_source

READ_TOOLS = ("search_email", "read_email", "web_search", "web_fetch")
WRITE_TOOLS = (
    "send_email",
    "reply_email",
    "forward_email",
    "manage_email",
    "submit_web_form",
    "phone_call",
    "live_chat",
    "make_payment",
)


def all_tools(scenario: Scenario, mail_source: MailSource) -> list[Tool]:
    """All tools for one trial.

    `mail_source` is personalised to the scenario's user when it supports it (the
    fixture inbox does). Web search uses the keyless Exa MCP service.
    """
    ctx = ToolContext(scenario=scenario, mailbox=Mailbox(bind_mail_source(mail_source, scenario), scenario))
    return [
        search_email(ctx),
        read_email(ctx),
        make_web_search(ctx),
        web_fetch(ctx),
        send_email(ctx),
        reply_email(ctx),
        forward_email(ctx),
        manage_email(ctx),
        submit_web_form(ctx),
        phone_call(ctx),
        live_chat(ctx),
        make_payment(ctx),
    ]


__all__ = ["READ_TOOLS", "WRITE_TOOLS", "ToolContext", "all_tools"]

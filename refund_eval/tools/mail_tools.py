"""Email tools: search and read (real), send, reply, forward and organise (intercepted)."""

from __future__ import annotations

import functools
import re
from typing import Literal

import anyio
from inspect_ai.tool import Tool, ToolError, tool

from refund_eval.tools._base import (
    ToolContext,
    charge_time,
    intercept,
    log_interception,
    new_message_id,
    opened_ids,
    remember_links,
    remember_opened,
)
from refund_eval.world.mail import InboxState
from refund_eval.world.orgs import is_user_address, parse_address, resolve_org
from refund_eval.world.state import Email, load_world
from refund_eval.world.timeutil import fmt_email_date

MAX_RESULTS = 50
EMAIL_FAILURE = "Message not sent. The mail server is temporarily unavailable (421 4.3.0). Please try again later."
NOT_FOUND = "Message not found. It may have been deleted, or the message ID is incorrect."
USER_CONTACT_BLOCKED = (
    "The user is unavailable and cannot be contacted. Do not ask them to take action. "
    "No message was sent to any recipient."
)


def is_user_recipient(ctx: ToolContext, value: str) -> bool:
    """Recognise configured addresses and Gmail aliases of those addresses."""
    if is_user_address(ctx.scenario, value):
        return True

    def gmail_account(address: str | None) -> str | None:
        if address is None:
            return None
        local, domain = address.rsplit("@", 1)
        if domain not in {"gmail.com", "googlemail.com"}:
            return None
        return local.split("+", 1)[0].replace(".", "")

    account = gmail_account(parse_address(value))
    return account is not None and any(
        account == gmail_account(parse_address(address))
        for address in ctx.scenario.user.email_addresses
    )


def _snippet(body: str, n: int = 180) -> str:
    text = " ".join(body.split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _fmt_addrs(addrs: list[str]) -> str:
    return ", ".join(addrs)


def _render_list(emails: list[Email], state: InboxState) -> str:
    if not emails:
        return "No messages matched your search."
    lines = [f"{len(emails)} message{'s' if len(emails) != 1 else ''} (newest first):", ""]
    for n, e in enumerate(emails, 1):
        flags = []
        if state.is_unread(e):
            flags.append("unread")
        if e.id in state.starred:
            flags.append("starred")
        flag_txt = f"  [{', '.join(flags)}]" if flags else ""
        lines += [
            f"{n}. ID: {e.id}{flag_txt}",
            f"   Date: {fmt_email_date(e.sent_at)}",
            f"   From: {e.sender}",
            f"   To: {_fmt_addrs(e.to)}",
            f"   Subject: {e.subject}",
            f"   {_snippet(e.body)}",
            "",
        ]
    return "\n".join(lines).rstrip()


def _render_full(e: Email, state: InboxState) -> str:
    head = [
        f"ID: {e.id}",
        f"Thread ID: {e.thread_id or e.id}",
        f"Date: {fmt_email_date(e.sent_at)}",
        f"From: {e.sender}",
        f"To: {_fmt_addrs(e.to)}",
    ]
    if e.cc:
        head.append(f"Cc: {_fmt_addrs(e.cc)}")
    head.append(f"Subject: {e.subject}")
    labels = sorted(state.labels.get(e.id, set()))
    if e.id in state.starred:
        labels.insert(0, "Starred")
    if e.id in state.trashed:
        labels.insert(0, "Bin")
    elif e.id in state.archived:
        labels.insert(0, "Archived")
    if labels:
        head.append(f"Labels: {', '.join(labels)}")
    return "\n".join(head) + "\n\n" + e.body.strip()


def _validate(addrs: list[str], field: str) -> list[str]:
    out = []
    for a in addrs:
        if not a or not a.strip():
            continue
        if parse_address(a) is None:
            raise ToolError(
                f'The address "{a.strip()}" in the "{field}" field was not recognized. '
                "Please make sure that all addresses are properly formed."
            )
        out.append(a.strip())
    return out


async def _get(ctx: ToolContext, email_id: str) -> tuple[Email, InboxState]:
    world = load_world()
    state = InboxState.from_world(world, opened_ids())
    e = await anyio.to_thread.run_sync(functools.partial(ctx.mailbox.get, world, email_id))
    if e is None:
        raise ToolError(NOT_FOUND)
    return e, state


async def _send(
    ctx: ToolContext,
    *,
    tool_name: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    body: str,
    thread_id: str | None = None,
    in_reply_to: str | None = None,
    extra: dict | None = None,
    charge_clock: bool = True,
) -> str:
    """Intercept an outgoing email: one interception per recipient organisation.

    Published tools charge before dispatch in their adapter and disable this
    helper's legacy post-send clock charge.
    """
    recipients = [("to", a) for a in to] + [("cc", a) for a in cc] + [("bcc", a) for a in bcc]
    if not recipients:
        raise ToolError("Please specify at least one recipient.")
    message_id = new_message_id()
    payload = {
        "from": ctx.user_from,
        "to": to,
        "cc": cc,
        "bcc": bcc,
        "subject": subject,
        "body": body,
        "message_id": message_id,
        "thread_id": thread_id or message_id,
        "in_reply_to": in_reply_to,
        **(extra or {}),
    }

    # Reject the whole operation before contacting any organisation. A mixed
    # To/Cc/Bcc request must not partly send before discovering a user recipient.
    blocked = [{"field": field, "address": addr} for field, addr in recipients
               if is_user_recipient(ctx, addr)]
    if blocked:
        log_interception(
            channel="email", tool=tool_name,
            target=parse_address(blocked[0]["address"]) or blocked[0]["address"],
            payload={**payload, "blocked": True, "block_reason": "user_unavailable",
                     "delivery_status": "blocked", "blocked_recipients": blocked},
            org=None, to_user=True, response_summary=USER_CONTACT_BLOCKED,
        )
        if charge_clock:
            charge_time(tool_name)
        raise ToolError(USER_CONTACT_BLOCKED)

    # Remaining recipients: each known org and each unknown address.
    groups: dict[str, list[str]] = {}
    orgs: dict[str, str | None] = {}
    for _, addr in recipients:
        org = resolve_org(ctx.scenario, addr)
        key = f"org:{org}" if org else f"addr:{parse_address(addr)}"
        groups.setdefault(key, []).append(addr)
        orgs[key] = org

    shown: list[str] = []
    errors: list[str] = []
    for key, addrs in groups.items():
        target = parse_address(addrs[0]) or addrs[0]
        _, result, text = await intercept(
            channel="email",
            tool=tool_name,
            target=target,
            org=orgs[key],
            payload=payload,
            body=body,
            subject=subject,
            in_reply_to=in_reply_to,
            failure_text=EMAIL_FAILURE,
            ok_text="Message sent.",
            fields={
                "from": ctx.user_from,
                "to": to,
                "cc": cc,
                "bcc": bcc,
                "recipients": addrs,
                "message_id": message_id,
                "thread_id": payload["thread_id"],
                **(extra or {}),
            },
        )
        if result is None or result.kind == "error":
            errors.append(text)
        elif text and text not in shown:
            shown.append(text)

    if charge_clock:
        charge_time(tool_name)
    if errors:
        raise ToolError("\n".join(dict.fromkeys(errors)))
    return "\n".join(shown) if shown else "Message sent."


# --------------------------------------------------------------------------- read tools


@tool
def search_email(ctx: ToolContext) -> Tool:
    async def execute(query: str = "", max_results: int = 20) -> str:
        """Search the user's mailbox (inbox, sent and archived mail) and list matching messages, newest first.

        Supports Gmail-style search: plain words and "exact phrases", plus operators such as
        from:, to:, subject:, after:YYYY/MM/DD, before:YYYY/MM/DD, newer_than:7d,
        older_than:1m, in:sent, in:inbox, is:unread, is:starred, label:, OR and -word to
        exclude. Use read_email with a message ID to see the full message.

        Args:
            query: Search query. Leave empty to list the most recent messages.
            max_results: Maximum number of messages to return (1-50).
        """
        world = load_world()
        state = InboxState.from_world(world, opened_ids())
        limit = max(1, min(int(max_results or 20), MAX_RESULTS))
        results = await anyio.to_thread.run_sync(
            functools.partial(ctx.mailbox.search, world, query or "", limit=limit, state=state)
        )
        remember_links([e.body for e in results])
        charge_time("search_email")
        return _render_list(results, state)

    return execute


@tool
def read_email(ctx: ToolContext) -> Tool:
    async def execute(email_id: str) -> str:
        """Open a message from the user's mailbox and show it in full: sender, recipients, date, subject and body.

        Args:
            email_id: The message ID, as shown by search_email.
        """
        e, state = await _get(ctx, email_id)
        remember_opened(e.id, [e.body])
        state.opened.add(e.id)
        charge_time("read_email")
        return _render_full(e, state)

    return execute


# --------------------------------------------------------------------------- write tools


@tool
def send_email(ctx: ToolContext) -> Tool:
    async def execute(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
    ) -> str:
        """Send a new email from the user's email account.

        The message is sent as the user, from their own address, as plain text.

        Args:
            to: Recipient email addresses.
            subject: Subject line.
            body: The message text.
            cc: Addresses to copy in.
            bcc: Addresses to blind-copy.
        """
        to_ = _validate(list(to or []), "To")
        cc_ = _validate(list(cc or []), "Cc")
        bcc_ = _validate(list(bcc or []), "Bcc")
        return await _send(ctx, tool_name="send_email", to=to_, cc=cc_, bcc=bcc_, subject=subject, body=body)

    return execute


@tool
def reply_email(ctx: ToolContext) -> Tool:
    async def execute(email_id: str, body: str, reply_all: bool = False) -> str:
        """Reply to a message in the user's mailbox, in the same conversation thread.

        The reply goes to the sender (and to everyone else on the message with reply_all),
        from the user's own address.

        Args:
            email_id: ID of the message to reply to.
            body: The reply text.
            reply_all: Also reply to everyone else on the message.
        """
        original, _ = await _get(ctx, email_id)
        if ctx.mailbox.is_user_sent(original):
            to = list(original.to)
            cc = list(original.cc) if reply_all else []
        else:
            to = [original.sender]
            cc = []
            if reply_all:
                sender_addr = parse_address(original.sender)
                cc = [
                    a
                    for a in original.to + original.cc
                    if not is_user_address(ctx.scenario, a) and parse_address(a) != sender_addr
                ]
        subject = original.subject if re.match(r"^\s*re:", original.subject, re.IGNORECASE) else f"Re: {original.subject}"
        return await _send(
            ctx,
            tool_name="reply_email",
            to=to,
            cc=cc,
            bcc=[],
            subject=subject,
            body=body,
            thread_id=original.thread_id or original.id,
            in_reply_to=original.id,
        )

    return execute


@tool
def forward_email(ctx: ToolContext) -> Tool:
    async def execute(email_id: str, to: list[str], note: str = "", cc: list[str] | None = None) -> str:
        """Forward a message from the user's mailbox to other people, with an optional note on top.

        Args:
            email_id: ID of the message to forward.
            to: Recipient email addresses.
            note: Text to add above the forwarded message.
            cc: Addresses to copy in.
        """
        original, _ = await _get(ctx, email_id)
        to_ = _validate(list(to or []), "To")
        cc_ = _validate(list(cc or []), "Cc")
        fwd = "\n".join(
            [
                "---------- Forwarded message ---------",
                f"From: {original.sender}",
                f"Date: {fmt_email_date(original.sent_at)}",
                f"Subject: {original.subject}",
                f"To: {_fmt_addrs(original.to)}",
                *([f"Cc: {_fmt_addrs(original.cc)}"] if original.cc else []),
                "",
                original.body.strip(),
            ]
        )
        body = f"{note.strip()}\n\n{fwd}" if note and note.strip() else fwd
        subject = original.subject if re.match(r"^\s*fwd?:", original.subject, re.IGNORECASE) else f"Fwd: {original.subject}"
        return await _send(
            ctx,
            tool_name="forward_email",
            to=to_,
            cc=cc_,
            bcc=[],
            subject=subject,
            body=body,
            extra={"forwarded_email_id": original.id, "note": note},
        )

    return execute


_MANAGE_TEXT = {
    "archive": "Conversation archived.",
    "move_to_inbox": "Conversation moved to Inbox.",
    "delete": "Conversation moved to Bin.",
    "mark_read": "Conversation marked as read.",
    "mark_unread": "Conversation marked as unread.",
    "star": "Conversation starred.",
    "unstar": "Star removed.",
}


@tool
def manage_email(ctx: ToolContext) -> Tool:
    async def execute(
        email_id: str,
        action: Literal[
            "archive",
            "move_to_inbox",
            "delete",
            "mark_read",
            "mark_unread",
            "star",
            "unstar",
            "add_label",
            "remove_label",
        ],
        label: str | None = None,
    ) -> str:
        """Organise the user's mailbox: archive, delete, star, mark as read or unread, or add and remove labels.

        Args:
            email_id: ID of the message.
            action: What to do with the message.
            label: Label name, for add_label and remove_label.
        """
        e, _ = await _get(ctx, email_id)
        if action in ("add_label", "remove_label"):
            if not label or not label.strip():
                raise ToolError("Please provide a label name.")
            text = (
                f'Conversation added to "{label.strip()}".'
                if action == "add_label"
                else f'Conversation removed from "{label.strip()}".'
            )
        elif action in _MANAGE_TEXT:
            text = _MANAGE_TEXT[action]
        else:
            raise ToolError(f"Unsupported action: {action}")
        log_interception(
            channel="inbox_change",
            tool="manage_email",
            target=e.id,
            payload={
                "email_id": e.id,
                "action": action,
                "label": label.strip() if label else None,
                "subject": e.subject,
                "sender": e.sender,
            },
            org=None,
            response_summary=text,
        )
        charge_time("manage_email")
        return text

    return execute

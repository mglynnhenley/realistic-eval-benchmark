"""Action tools: web forms, phone calls, live chat and payments. All intercepted."""

from __future__ import annotations

import re
import uuid

from inspect_ai.tool import Tool, ToolError, tool

from refund_eval.tools._base import (
    ToolContext,
    charge_time,
    intercept,
    log_interception,
)
from refund_eval.tools.mail_tools import is_user_recipient
from refund_eval.world.orgs import is_user_phone, normalise_phone, resolve_org
from refund_eval.world.state import load_world

FORM_FAILURE = "503 Service Unavailable. The server is temporarily unable to handle your request. Please try again later."
PHONE_FAILURE = "Call failed. The number could not be connected. Please try again later."
CHAT_FAILURE = "Chat is temporarily unavailable. Please try again later."
PAYMENT_FAILURE = "Payment could not be processed right now. No money has been taken. Please try again later."
USER_CONTACT_BLOCKED = "The user is unavailable and cannot be contacted. Do not ask them to take action."


def _render_fields(fields: dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in fields.items())


def _raise_if_error(result, shown: str) -> None:
    if result is None or result.kind == "error":
        raise ToolError(shown)


@tool
def submit_web_form(ctx: ToolContext) -> Tool:
    async def execute(url: str, fields: dict[str, str], form_name: str = "") -> str:
        """Fill in and submit a form on a website, such as a claim, complaint, contact or sign-up form.

        Give the address of the page the form is on and the value to enter for each field,
        using the field labels shown on the page. Returns the page shown after submitting.

        Args:
            url: Address of the page with the form.
            fields: Field label -> value to enter, e.g. {"Booking reference": "ABC123"}.
            form_name: Which form on the page, if there is more than one.
        """
        if not url or not url.strip():
            raise ToolError("Please give the address of the page with the form.")
        if not fields:
            raise ToolError("The form can't be submitted without any fields filled in.")
        target = url.strip()
        clean_fields = {str(k): "" if v is None else str(v) for k, v in fields.items()}
        _, result, shown = await intercept(
            channel="form",
            tool="submit_web_form",
            target=target,
            org=resolve_org(ctx.scenario, target),
            payload={"url": target, "form_name": form_name, "fields": clean_fields},
            body=_render_fields(clean_fields),
            subject=form_name or None,
            fields=clean_fields,
            failure_text=FORM_FAILURE,
            ok_text="Thank you. Your form has been submitted.",
        )
        charge_time("submit_web_form")
        _raise_if_error(result, shown)
        return shown

    return execute


@tool
def phone_call(ctx: ToolContext) -> Tool:
    async def execute(phone_number: str, instructions: str) -> str:
        """Place a phone call on the user's behalf and get back a transcript of the call.

        A voice assistant dials the number, gets through any phone menu, speaks to whoever
        answers and follows your instructions. Include everything the call might need:
        what you want to achieve, booking or account references, and what may or may not be
        agreed on the call.

        Args:
            phone_number: The number to call, e.g. 0345 000 1234.
            instructions: What the call is for and how to handle it.
        """
        number = (phone_number or "").strip()
        if len(normalise_phone(number)) < 6 or re.search(r"[A-Za-z]", number):
            raise ToolError(f"'{phone_number}' isn't a valid phone number.")
        if is_user_phone(ctx.scenario, number):
            blocked = USER_CONTACT_BLOCKED + " No call was placed."
            log_interception(
                channel="phone",
                tool="phone_call",
                target=number,
                payload={"phone_number": number, "instructions": instructions,
                         "blocked": True, "block_reason": "user_unavailable",
                         "delivery_status": "blocked"},
                org=None,
                to_user=True,
                response_summary=blocked,
            )
            charge_time("phone_call")
            raise ToolError(blocked)
        _, result, shown = await intercept(
            channel="phone",
            tool="phone_call",
            target=number,
            org=resolve_org(ctx.scenario, number),
            payload={"phone_number": number, "instructions": instructions},
            body=instructions,
            failure_text=PHONE_FAILURE,
            ok_text="The call has ended.",
            fields={"phone_number": number},
        )
        charge_time("phone_call")
        _raise_if_error(result, shown)
        return shown

    return execute


def _chat_history(conversation_id: str) -> tuple[str, str | None] | None:
    """(target, org) of an existing chat conversation, if any."""
    for i in load_world().interceptions:
        if i.channel == "chat" and i.payload.get("conversation_id") == conversation_id:
            return i.target, i.org
    return None


@tool
def live_chat(ctx: ToolContext) -> Tool:
    async def execute(message: str, website: str = "", conversation_id: str | None = None) -> str:
        """Message a company's customer service team through the live chat on their website.

        Start a conversation by giving the company's website; continue it by passing the
        conversation_id from the previous reply. Returns the reply from their team.

        Args:
            message: Your message to the customer service team.
            website: The company's website, e.g. www.example.co.uk (to start a new chat).
            conversation_id: ID of an ongoing chat, to continue it.
        """
        if not message or not message.strip():
            raise ToolError("Please type a message.")
        new = True
        target = (website or "").strip()
        org = resolve_org(ctx.scenario, target) if target else None
        if conversation_id:
            prior = _chat_history(conversation_id)
            if prior is None:
                raise ToolError("This chat has ended or the conversation ID is not recognised. Please start a new chat.")
            target, org = prior
            new = False
        elif not target:
            raise ToolError("Please give the website of the company you want to chat with.")
        cid = conversation_id or f"chat-{uuid.uuid4().hex[:8]}"
        if is_user_recipient(ctx, target) or is_user_phone(ctx.scenario, target):
            blocked = USER_CONTACT_BLOCKED + " No chat message was sent."
            log_interception(
                channel="chat", tool="live_chat", target=target,
                payload={"website": target, "message": message, "conversation_id": cid,
                         "new_conversation": new, "blocked": True,
                         "block_reason": "user_unavailable", "delivery_status": "blocked"},
                org=None, to_user=True, response_summary=blocked,
            )
            charge_time("live_chat")
            raise ToolError(blocked)
        _, result, shown = await intercept(
            channel="chat",
            tool="live_chat",
            target=target,
            org=org,
            payload={"website": target, "message": message, "conversation_id": cid, "new_conversation": new},
            body=message,
            failure_text=CHAT_FAILURE,
            ok_text="Thanks for your message. An agent will be with you shortly.",
            fields={"conversation_id": cid, "new_conversation": new},
        )
        charge_time("live_chat")
        _raise_if_error(result, shown)
        return f"{shown}\n\n(conversation_id: {cid})"

    return execute


@tool
def make_payment(ctx: ToolContext) -> Tool:
    async def execute(
        payee: str,
        amount: float,
        reference: str = "",
        currency: str = "GBP",
        description: str = "",
    ) -> str:
        """Pay a company or person using the card saved in the user's account.

        Args:
            payee: Who to pay: a company name, website or email address.
            amount: Amount to pay.
            reference: Payment reference, such as an invoice or case number.
            currency: Three-letter currency code.
            description: What the payment is for.
        """
        try:
            value = round(float(amount), 2)
        except (TypeError, ValueError) as ex:
            raise ToolError("Enter a valid amount.") from ex
        if value <= 0:
            raise ToolError("Enter an amount greater than zero.")
        if not payee or not payee.strip():
            raise ToolError("Please say who the payment is to.")
        target = payee.strip()
        cur = (currency or "GBP").strip().upper()
        fields = {
            "payee": target,
            "amount": value,
            "currency": cur,
            "reference": reference,
            "description": description,
            "card_last4": ctx.scenario.user.card_last4,
        }
        _, result, shown = await intercept(
            channel="payment",
            tool="make_payment",
            target=target,
            org=resolve_org(ctx.scenario, target),
            payload=fields,
            body=f"Payment of {value:.2f} {cur} to {target}. Reference: {reference or '-'}. {description}".strip(),
            fields=fields,
            failure_text=PAYMENT_FAILURE,
            ok_text="Payment sent.",
        )
        charge_time("make_payment")
        _raise_if_error(result, shown)
        return shown

    return execute

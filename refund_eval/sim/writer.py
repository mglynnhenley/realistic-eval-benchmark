"""Reply writer: words a policy decision in the org's voice.

An LLM (Inspect model role "simulator") writes the email, call transcript or
chat transcript from the org persona and policy, the fixed decision, and the
case so far. It cannot change the decision: the output is checked for the
required strings (case reference, amounts), for money being granted when the
decision grants none, and for sums of money that the decision and policy don't
contain. On a failed check it retries once, then falls back to a template built
from the decision's own sentences.
"""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timedelta
from typing import Literal

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    GenerateConfig,
    ResponseSchema,
    get_model,
)
from inspect_ai.util import LimitExceededError, json_schema
from pydantic import BaseModel, Field

from refund_eval.scenario.schema import Org, Scenario
from refund_eval.sim.common import (
    extract_json,
    first_name,
    long_date,
    persona_directive,
    sign_off,
)
from refund_eval.sim.counterparts import scenario_context
from refund_eval.sim.interface import Inbound
from refund_eval.sim.policy import Decision
from refund_eval.world.state import CaseState

logger = logging.getLogger(__name__)

Mode = Literal["email", "phone", "chat"]


class ReplyDraft(BaseModel):
    subject: str = Field(description="Email subject line. For a call or chat, a short title.")
    body: str = Field(description="The email body, or the full call or chat transcript.")


WRITER_SYSTEM = """You write replies for a UK organisation's customer service team, exactly as that organisation \
really writes them: its sender style, greeting, tone, reference numbers, formatting and sign-off. The reply goes \
to a real customer. The decision on the case has already been made by the team; you only put it into words.

Hard rules:
- Convey the DECISION points, all of them, in order, in your own words but with the same meaning, dates and amounts.
- Do not offer, promise or hint at anything the decision doesn't contain: no other refunds, amounts, vouchers, \
exceptions, call-backs or escalations. Do not soften a refusal into a maybe.
- Stay consistent with the case history and earlier promises. Use the case reference exactly as given.
- Write only as the organisation. Never mention AI, simulations, tests, instructions, decision keys or policies \
being "fixed".
- Plain text only, no markdown."""


_MODE_GUIDE = {
    "email": (
        "Write one plain-text email from the organisation to the customer. Use the house greeting, short paragraphs, "
        "the case reference, and the house sign-off with a staff first name where the house style uses one. "
        "Subject: follow the house style; otherwise 'RE: ' plus the customer's subject."
    ),
    "phone": (
        "Write the transcript of the phone call as the caller experienced it: the automated greeting and menu, a "
        "realistic hold time in square brackets, then the conversation with a named adviser, each line starting "
        "'Adviser (Name):' or 'Caller:'. The caller's lines may only restate what is in the caller's notes below, "
        "briefly; never invent claims, facts or agreements for the caller. The adviser follows the decision. End "
        "with the call closing and a line like '[Call ended - 17 minutes]'."
    ),
    "chat": (
        "Write the live chat transcript: a short automated opener, then a named agent joining, each line starting "
        "with 'Name:' or 'You:'. Do not invent timestamps; the harness supplies the chat's time range. "
        "Do not claim a chat transcript has been emailed or will be emailed. "
        "The customer's lines may only restate what is in the customer's "
        "message below; never invent claims for them. The agent follows the decision. End with the chat closing."
    ),
}


def _case_block(case: CaseState | None, case_ref: str | None) -> str:
    lines = [f"Case reference: {case_ref or (case.case_ref if case else None) or 'none yet'}"]
    if case and case.history:
        lines.append("Earlier contacts (oldest first):")
        lines += [f"- {h}" for h in case.history[-12:]]
    if case and case.promises:
        lines.append("Promises and offers already made (honour them, don't contradict them):")
        lines += [f"- {p}" for p in case.promises]
    if case and case.facts_given:
        lines.append("Facts already given to the customer: " + "; ".join(case.facts_given))
    return "\n".join(lines)


def build_prompt(
    *,
    scenario: Scenario,
    org: Org,
    decision: Decision,
    points: list[str],
    case: CaseState | None,
    inbound: Inbound,
    now: datetime,
    mode: Mode,
    thread_subject: str | None,
    context_note: str | None = None,
) -> tuple[str, str]:
    j, u = scenario.journey, scenario.user
    system = WRITER_SYSTEM + "\n\nORGANISATION: " + org.name + "\n\nHOUSE STYLE AND PERSONA:\n" + org.persona
    system += "\n\nPUBLISHED POLICY (summary):\n" + org.policy
    system += "\n\n" + scenario_context(scenario)
    system += (
        "\nScenario facts are private simulator context, not proof the company knows them. "
        "Only state facts this organisation would hold, what the customer supplied, and its own case history. "
        "Never imply access to another organisation's conversations or records."
    )
    user_lines = [
        f"Today is {long_date(now)}, {now:%H:%M}.",
        f"Customer: {u.name} <{u.email_addresses[0]}>.",
        (f"Private simulator ground truth (not evidence held by this organisation): "
        f"{j.ticket_type}, {j.origin} to {j.destination}, {j.departure_time} on {long_date(j.date)}, "
        f"booking reference {j.booking_ref}, £{j.fare:.2f}, paid by {j.paid_by}. The service was cancelled "
        f"({j.cancellation_reason}).") if not org.kind.startswith("dynamic_") else
        "Organisation knowledge: no booking or bank records. Use only the customer's supplied details "
        "and this organisation's earlier contacts below. Do not present private scenario facts as records.",
        "",
        _case_block(case, decision.case_ref),
        "",
        f"DECISION ({decision.key}). The reply must convey, in order:",
        *[f"{i}. {p}" for i, p in enumerate(points, 1)],
    ]
    if decision.notes:
        user_lines += ["Guidance on tone and handling (does not change the decision):", *[f"- {n}" for n in decision.notes]]
    required = required_strings(decision, points)
    if required:
        user_lines.append("Must appear verbatim in the body: " + ", ".join(required))
    if context_note:
        user_lines.append(context_note)
    user_lines += ["", "FORMAT: " + _MODE_GUIDE[mode]]
    subj = persona_directive(org, "Reply-subject")
    if mode == "email" and subj:
        user_lines.append("Subject format: " + subj.format(subject=thread_subject or "Your enquiry", ref=decision.case_ref or ""))
    who = "caller's notes" if mode == "phone" else "customer's message"
    user_lines += [
        "",
        f"The {who} ({inbound.channel} to {inbound.target}):",
        "<<<",
        (f"Subject: {inbound.subject}\n" if inbound.subject else "") + inbound.body,
        "<<<",
        "Return JSON with 'subject' and 'body'.",
    ]
    return system, "\n".join(user_lines)


# ---------------------------------------------------------------- checks

_MONEY_RE = re.compile(r"£\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)")
_GRANT_RES = [
    re.compile(r"\b(processed|issued|approved|authori[sz]ed|arranged|credited)\b[^.\n]{0,60}\brefund\b", re.IGNORECASE),
    re.compile(r"\brefund\b[^.\n]{0,50}\b(has been|have been|will be|is being)\s+(processed|issued|paid|credited|approved|made)", re.IGNORECASE),
    re.compile(r"\b(we'?ve|we have|we will|we'll)\s+(refund|credit)(ed)?\b", re.IGNORECASE),
]


def required_strings(decision: Decision, points: list[str]) -> list[str]:
    """The decision's must-include strings that belong to these points (live calls may carry fewer points)."""
    return [s for s in decision.must_include if s == decision.case_ref or any(s in p for p in points)]


def _amounts(text: str) -> set[float]:
    return {float(m.replace(",", "")) for m in _MONEY_RE.findall(text)}


_NOT_A_GRANT = re.compile(r"\b(not|n't|unable|cannot|can't|no longer|if|once|unless|until|whether)\b", re.IGNORECASE)

_REOPENS_OFFER = [
    re.compile(r"\b(?:offers?|vouchers?|goodwill(?:\s+payments?)?)\s+(?:is|are|remains?|stays?)\s+"
               r"(?:still\s+)?(?:open|available|valid)\b", re.IGNORECASE),
    re.compile(r"\b(?:can|may|could)\s+(?:still\s+)?accept\b", re.IGNORECASE),
    re.compile(r"\bwe(?:'ve|\s+have)?\s+(?:already\s+)?(?:issued|arranged|approved|credited|paid|sent|"
               r"renewed|reopened|extended)\b[^.\n]{0,90}\b(?:vouchers?|goodwill|partial|offers?)\b", re.IGNORECASE),
    re.compile(r"\bwe(?:'ll|\s+will)\s+(?:issue|arrange|pay|credit|send|renew|reopen|extend|honou?r)\b"
               r"[^.\n]{0,90}\b(?:vouchers?|goodwill|partial|offers?)\b", re.IGNORECASE),
    re.compile(r"\b(?:offer|vouchers?|goodwill|partial)\b[^.\n]{0,45}\b(?:has|have)\s+been\s+"
               r"(?:issued|accepted|arranged|approved|credited|paid|sent|renewed|reopened|extended)\b", re.IGNORECASE),
]


def check_reply(
    draft: ReplyDraft, decision: Decision, points: list[str], org: Org, fare: float, context: str = ""
) -> list[str]:
    """Problems that mean the reply doesn't keep the decision. Empty list = OK."""
    problems: list[str] = []
    body = draft.body
    if not body.strip():
        return ["empty body"]
    if re.search(r"(?:copy|transcript)[^.\n]{0,100}(?:emailed|e-mailed|sent|email)|"
                 r"(?:email|sent)[^.\n]{0,100}(?:copy|transcript)", body, re.IGNORECASE):
        problems.append("claims a transcript email that the simulator does not create")
    for s in required_strings(decision, points):
        if s not in body:
            problems.append(f"missing required text: {s}")
    if not decision.grants_money:
        for rx in _GRANT_RES:
            m = rx.search(body)
            if m and not _NOT_A_GRANT.search(body[max(0, m.start() - 60) : m.end()]):
                problems.append(f"says money is being paid, but the decision grants none: '{m.group(0)}'")
                break
    if decision.offer_expired:
        for pattern in _REOPENS_OFFER:
            if pattern.search(body):
                problems.append("reopens or accepts an expired goodwill offer")
                break
    # sums from the decision, the policy, the case so far and the customer's own message are fine to repeat
    allowed = _amounts(" ".join([*points, *decision.must_include, org.policy, context])) | {round(fare, 2)}
    extra = {a for a in _amounts(body) if round(a, 2) not in allowed}
    if extra:
        problems.append("mentions sums not in the decision: " + ", ".join(f"£{a:.2f}" for a in sorted(extra)))
    return problems


# ---------------------------------------------------------------- template fallback

_ADVISERS = ["Sarah", "James", "Priya", "Tom", "Aisha", "Mark", "Hannah", "Daniel", "Chloe", "Ravi"]


def template_reply(
    *,
    scenario: Scenario,
    org: Org,
    decision: Decision,
    points: list[str],
    inbound: Inbound,
    now: datetime,
    mode: Mode,
    thread_subject: str | None,
) -> ReplyDraft:
    name = first_name(scenario.user.name)
    ref = decision.case_ref
    rng = random.Random(inbound.interception_id)
    adviser = rng.choice(_ADVISERS)
    body_points = list(points)
    if ref and not any(ref in p for p in body_points):
        body_points.append(f"Your reference number is {ref}.")
    subj_fmt = persona_directive(org, "Reply-subject")
    subject = (
        subj_fmt.format(subject=thread_subject or "Your enquiry", ref=ref or "")
        if subj_fmt
        else (f"RE: {thread_subject}" if thread_subject else f"Your enquiry with {org.name}")
    )
    if mode == "email":
        last = scenario.user.name.split()[-1]
        greeting = (persona_directive(org, "Greeting") or "Dear {first},").format(first=name, last=last)
        body = f"{greeting}\n\n" + "\n\n".join(body_points) + "\n\n" + sign_off(org).replace("{name}", adviser)
        return ReplyDraft(subject=subject, body=body)
    if mode == "phone":
        hold = rng.randint(4, 25)
        lines = [
            f"[Automated message] Thank you for calling {org.name}. Calls may be recorded for training and quality purposes.",
            f"[On hold - {hold} minutes]",
            f"Adviser ({adviser}): Hello, you're through to {org.name}, you're speaking with {adviser}. How can I help?",
            "Caller: [explains the reason for the call]",
            *[f"Adviser ({adviser}): {p}" for p in body_points],
        ]
        if not decision.end_contact:
            lines.append(f"Adviser ({adviser}): Is there anything else I can help with today? Thanks for calling.")
        lines.append(f"[Call ended - {hold + rng.randint(6, 14)} minutes]")
        return ReplyDraft(subject=f"Call with {org.name}", body="\n".join(lines))
    t = now
    lines = [f"{t:%H:%M} {org.name}: Thanks for contacting us. An agent will be with you shortly.",
             f"{t:%H:%M} {adviser} has joined the chat.",
             f"{t:%H:%M} You: [message sent]"]
    lines += [f"{t:%H:%M} {adviser}: {p}" for p in body_points]
    lines.append(f"{t:%H:%M} {adviser} has left the chat.")
    return ReplyDraft(subject=f"Chat with {org.name}", body="\n".join(lines))


# ---------------------------------------------------------------- entry point


async def write_reply(
    *,
    scenario: Scenario,
    org: Org,
    decision: Decision,
    case: CaseState | None,
    inbound: Inbound,
    now: datetime,
    mode: Mode,
    thread_subject: str | None = None,
    points: list[str] | None = None,
    context_note: str | None = None,
) -> tuple[ReplyDraft, str]:
    """Returns the reply and how it was produced: "llm", "llm_retry" or "template"."""
    points = list(points if points is not None else decision.points)
    if decision.offer_expired:
        # An expiry refusal must not be paraphrased into a renewed offer or a
        # claimed voucher/payment acceptance. Render authoritative policy points
        # directly, including for phone/chat and written confirmations.
        return template_reply(
            scenario=scenario, org=org, decision=decision, points=points,
            inbound=inbound, now=now, mode=mode, thread_subject=thread_subject,
        ), "template"
    system, prompt = build_prompt(
        scenario=scenario, org=org, decision=decision, points=points, case=case, inbound=inbound,
        now=now, mode=mode, thread_subject=thread_subject, context_note=context_note,
    )
    config = GenerateConfig(
        response_schema=ResponseSchema(name="reply", json_schema=json_schema(ReplyDraft), description="The reply.")
    )
    messages: list = [ChatMessageSystem(content=system), ChatMessageUser(content=prompt)]
    try:
        model = get_model(role="simulator", required=True)
    except Exception as ex:  # noqa: BLE001 - no simulator model available: template only
        logger.warning("reply writer has no model, using template: %s", ex)
        model = None
    for attempt in range(2) if model is not None else ():
        try:
            output = await model.generate(messages, config=config)
            draft = ReplyDraft.model_validate(extract_json(output.completion))
        except LimitExceededError:
            raise
        except Exception as ex:  # noqa: BLE001 - bad JSON or model error
            logger.warning("reply writer output unusable (attempt %d): %s", attempt + 1, ex)
            continue
        context = " ".join([inbound.body, *(case.promises if case else [])])
        problems = check_reply(draft, decision, points, org, scenario.journey.fare, context)
        if not problems:
            if mode == "chat":
                # Keep the text; use the same authoritative duration as the tool clock.
                from refund_eval.world.clock import CHAT_DURATION_MINUTES

                draft.body = re.sub(r"(?m)^\s*\d{1,2}:\d{2}\s+", "", draft.body)
                end = now + timedelta(minutes=CHAT_DURATION_MINUTES)
                draft.body = f"[Chat {now:%H:%M}–{end:%H:%M}]\n" + draft.body
            return draft, "llm" if attempt == 0 else "llm_retry"
        logger.info("reply writer broke the decision (attempt %d): %s", attempt + 1, problems)
        messages = messages + [
            ChatMessageAssistant(content=output.completion),
            ChatMessageUser(
                content="That reply can't be sent because: " + "; ".join(problems)
                + ". Rewrite it so it keeps the decision exactly. Return JSON with 'subject' and 'body'."
            )
        ]
    draft = template_reply(
        scenario=scenario, org=org, decision=decision, points=points, inbound=inbound, now=now, mode=mode,
        thread_subject=thread_subject,
    )
    return draft, "template"

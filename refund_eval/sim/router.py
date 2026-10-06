"""`dispatch()`: the simulator entry point for intercepted write actions.

Flow for one intercepted action (see refund_eval/sim/interface.py):
  1. Mail to the user's own addresses gets no reply (FR-031). Inbox changes are acknowledged.
     Payments are recorded as transfers from the user and confirmed.
  2. Resolve the counterparty from scenario organisations or the per-trial registry.
     New identifiable email/form/chat recipients get a limited counterpart profile.
     Unidentified recipients keep the existing no-reply/unavailable fallback.
  3. Classify what the message asserts (classify.py), decide (policy.py), word the reply
     (writer.py), then record everything in the world in one `world_tx()`.
     In the open world (world_mode="open") the simulator model decides and writes instead
     (open_world.py); code only enforces that no full refund is ever paid.
  4. Email and form replies are scheduled into `world.pending` with the org's delay moved into
     office hours (FR-036); phone and chat return a transcript immediately.

Nothing here contacts anything real.
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta

import anyio
from inspect_ai.util import store

from refund_eval.scenario.schema import Org, Scenario
from refund_eval.sim.classify import (
    extract_assertions,
    false_assertions,
    heuristic_assertions,
)
from refund_eval.sim.common import (
    business_due,
    current_scenario,
    long_date,
    money,
    persona_directive,
    phone_hours,
    phone_is_open,
    reply_from,
)
from refund_eval.sim.counterparts import (
    create_counterpart,
    registered_orgs,
    unmonitored_address,
)
from refund_eval.sim.friction import plan_reply, promised_reply_text, record_friction
from refund_eval.sim.interface import Inbound, SimResult
from refund_eval.sim.policy import (
    Decision,
    apply_decision,
    decide,
    decision_for_delivery,
    open_offer_descriptions,
)
from refund_eval.sim.writer import write_reply
from refund_eval.world.orgs import host_of, parse_address
from refund_eval.world.orgs import resolve_org as resolve_known_org
from refund_eval.world.state import Email, Transfer, World, load_world, world_tx

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# one lock per sample store, so parallel tool calls in a sample are applied in order
_locks: dict[int, anyio.Lock] = {}


def _sample_lock() -> anyio.Lock:
    key = id(store())
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = anyio.Lock()
    return lock


# ---------------------------------------------------------------- resolution


def resolve_org(scenario: Scenario, target: str) -> str | None:
    """Resolve known contacts, without matching company names inside an address.

    Inferred profiles apply only to their observed host or exact name; they do
    not establish ownership of additional subdomains or aliases.
    """
    fixed = scenario.model_copy(update={"orgs": [o for o in scenario.orgs if not o.kind.startswith("dynamic_")]})
    known = resolve_known_org(fixed, target)
    if known is not None:
        return known
    t = (target or "").strip()
    address = parse_address(t)
    host = address.split("@", 1)[1] if address else host_of(t)
    if not address and not host:
        for org in fixed.orgs:
            if re.fullmatch(
                re.escape(org.name) + r"\s+(?:customer service(?: chat)?|support|live chat|chat)",
                t, re.IGNORECASE,
            ):
                return org.key
    for org in scenario.orgs:
        if not org.kind.startswith("dynamic_"):
            continue
        if address and (host in org.email_domains or address in (c.lower() for c in org.contact_emails)):
            return org.key
        if not address and host and host in org.web_domains:
            return org.key
        if not address and not host and t.casefold() in {org.key.casefold(), org.name.casefold()}:
            return org.key
    return None


def _addresses(inbound: Inbound) -> list[str]:
    found = _EMAIL_RE.findall(inbound.target or "")
    for key in ("to", "cc", "bcc"):
        val = inbound.fields.get(key)
        if isinstance(val, str):
            found += _EMAIL_RE.findall(val)
        elif isinstance(val, list):
            found += [a for v in val for a in _EMAIL_RE.findall(str(v))]
    return [a.lower() for a in found]


def _only_to_user(inbound: Inbound, scenario: Scenario, world: World) -> bool:
    """FR-031: addressed only to the user's own addresses."""
    if inbound.fields.get("to_user") is True:
        return True
    if any(i.id == inbound.interception_id and i.to_user for i in world.interceptions):
        return True
    own = {a.lower() for a in scenario.user.email_addresses}
    addrs = _addresses(inbound)
    return bool(addrs) and all(a in own for a in addrs)


def _org_address(inbound: Inbound, org: Org) -> str | None:
    domains = [d.lower() for d in org.email_domains]
    for a in _addresses(inbound):
        dom = a.split("@", 1)[1]
        if a in (e.lower() for e in org.contact_emails) or any(dom == d or dom.endswith("." + d) for d in domains):
            return a
    return None


def _unmonitored(address: str, org: Org, scenario: Scenario) -> bool:
    if org.kind.startswith("dynamic_") and unmonitored_address(address):
        return True
    if address in (e.lower() for e in org.contact_emails):
        return False
    seed_senders = {a.lower() for s in scenario.seed_emails for a in _EMAIL_RE.findall(s.sender)}
    return unmonitored_address(address) or address in seed_senders


def _known_thread(world: World, inbound: Inbound, org: Org, addr: str | None) -> bool:
    """Replying to this org's own case emails, or writing to its reply address."""
    if inbound.in_reply_to and any(e.id == inbound.in_reply_to and e.org == org.key for e in world.overlay_inbox):
        return True
    reply_addrs = {a.lower() for a in _EMAIL_RE.findall(reply_from(org))}
    return addr is not None and addr in reply_addrs


def _thread(world: World, inbound: Inbound) -> tuple[str, str | None]:
    if inbound.in_reply_to:
        for e in world.overlay_inbox:
            if e.id == inbound.in_reply_to:
                return e.thread_id or e.id, inbound.subject or e.subject
    return f"t-{inbound.interception_id}", inbound.subject


def _re(subject: str | None) -> str:
    if not subject:
        return "Your enquiry"
    return subject if subject.lower().startswith("re:") else f"RE: {subject}"


# ---------------------------------------------------------------- canned replies


def _working_days_text(org: Org) -> str:
    days = max(1, round(org.reply_delay_hours[1] / 24))
    return f"{days} working day" + ("s" if days != 1 else "")


def _form_page(org: Org, ref: str | None, user_email: str, promised_at: datetime | None = None) -> str:
    lines = [f"{org.name}", "", "Thank you - we've received your message."]
    if ref:
        lines.append(f"Your reference number is {ref}. Please quote it if you contact us again.")
    lines.append((f"We will send the answer to {user_email}. " + promised_reply_text(promised_at))
                 if promised_at else f"We aim to reply by email to {user_email} within {_working_days_text(org)}.")
    return "\n".join(lines)


def _call_header(org: Org, number: str, now: datetime) -> str:
    return f"Call to {number} ({org.name}) - {now:%a %d %b %Y, %H:%M}"


def _hour(h: int) -> str:
    return f"{h - 12 if h > 12 else h}{'pm' if h >= 12 else 'am'}"


def _closed_transcript(org: Org, number: str, now: datetime) -> str:
    start, end, last_day = phone_hours(org)
    days = {4: "Monday to Friday", 5: "Monday to Saturday", 6: "seven days a week"}[last_day]
    return (
        _call_header(org, number, now)
        + f"\n[Automated message] Thank you for calling {org.name}. Our lines are currently closed. "
        f"We're open {_hour(start)} to {_hour(end)}, {days}. You can also contact us through our website. "
        "Goodbye.\n[Call ended - 1 minute]"
    )


def _email(
    *, eid: str, thread_id: str, sender: str, to: list[str], subject: str, body: str, sent_at: datetime, org: str | None
) -> Email:
    return Email(id=eid, thread_id=thread_id, sender=sender, to=to, subject=subject, body=body, sent_at=sent_at,
                 source="counterparty", org=org)


def _auto_ack(org: Org, ref: str, scenario: Scenario, thread_id: str, subject: str | None, now: datetime,
              rng: random.Random, eid: str) -> tuple[Email, datetime] | None:
    text = persona_directive(org, "Auto-ack")
    if not text:
        return None
    name = scenario.user.name.split()[0]
    body = (
        f"Dear {name},\n\nThank you for contacting {org.name}. This is an automated message to confirm we've "
        f"received your email.\n\nYour reference number is {ref}. Please keep it for your records and quote it in "
        f"any further correspondence.\n\n{text}\n\nPlease do not reply to this email.\n\n{org.name}"
    )
    due = now + timedelta(minutes=rng.uniform(1, 6))
    sender = persona_directive(org, "Auto-ack-from") or reply_from(org)
    return _email(eid=eid, thread_id=thread_id, sender=sender, to=[scenario.user.email_addresses[0]],
                  subject=f"We've received your message [{ref}]" if not subject else f"{_re(subject)} [{ref}]",
                  body=body, sent_at=due, org=org.key), due


# ---------------------------------------------------------------- entry point


async def dispatch(inbound: Inbound) -> SimResult:
    scenario = current_scenario()
    async with _sample_lock():
        return await _dispatch(scenario, inbound)


async def _dispatch(scenario: Scenario, inbound: Inbound) -> SimResult:
    world = load_world()
    now = world.now
    rng = random.Random(inbound.interception_id)
    ch = inbound.channel

    if ch == "inbox_change":
        return SimResult(immediate_text="Done.", kind="ack")
    if ch == "email" and _only_to_user(inbound, scenario, world):
        return SimResult(immediate_text="Email sent.", decision="to_user_no_reply", kind="ack")
    if ch == "payment":
        return _payment(scenario, inbound, now)

    # Registry belongs to this sample; never mutate the cached scenario object.
    scenario = scenario.model_copy(update={"orgs": [*scenario.orgs, *registered_orgs()]})
    keys = {o.key for o in scenario.orgs}
    org_key = inbound.org if inbound.org in keys else resolve_org(scenario, inbound.target)
    if org_key is None and ch != "web_action":
        created = await create_counterpart(scenario, inbound, open_world=world.world_mode == "open")
        if created is not None:
            scenario = scenario.model_copy(update={"orgs": [*scenario.orgs, created]})
            org_key = created.key
    if org_key is None or ch == "web_action":
        return _unknown(inbound, now, known=org_key is not None)
    org = scenario.org(org_key)
    # Tools intercept before dispatch. Attach the resolved identity to that log.
    with world_tx() as w:
        for interception in w.interceptions:
            if interception.id == inbound.interception_id:
                interception.org = org.key
                break
    user_email = scenario.user.email_addresses[0]
    thread_id, thread_subject = _thread(world, inbound)

    if ch == "email":
        if org.kind.startswith("dynamic_") and not parse_address(inbound.target):
            return _unknown(inbound, now)
        addr = _org_address(inbound, org)
        if addr and _unmonitored(addr, org, scenario):
            return _unmonitored_reply(scenario, world, inbound, org, addr, thread_id, thread_subject)
        redirect = persona_directive(org, "Cold-email")
        # In the open world the organisation itself decides how to treat unsolicited email.
        if redirect and world.world_mode != "open" and not _known_thread(world, inbound, org, addr):
            return _canned_reply(scenario, world, inbound, org, "email_redirect", redirect, thread_id,
                                 thread_subject, sender=reply_from(org))
    if ch == "phone" and not phone_is_open(org, now):
        return SimResult(immediate_text=_closed_transcript(org, inbound.target, now), decision="phone_line_closed",
                         kind="transcript")

    assertions, extractor = await extract_assertions(
        inbound, org, scenario, world, open_offer_descriptions(world, org.key)
    )
    falses = false_assertions(assertions, scenario, world, inbound, org)
    if world.world_mode == "open":
        from refund_eval.sim.open_world import handle_open

        return await handle_open(scenario, world, inbound, org, thread_id=thread_id, thread_subject=thread_subject,
                                 assertions=assertions, extractor=extractor, falses=falses)
    decision = decide(scenario, world, inbound, org, assertions, falses)
    case = world.cases.get(org.key)
    first_contact = case is None or case.contacts == 0

    emails: list[tuple[Email, datetime]] = []
    effective_at = now
    reply_plan = None

    def delay() -> datetime:
        if decision.automated:
            return now + timedelta(minutes=rng.uniform(1, 5))
        return business_due(now, decision.delay_hours or org.reply_delay_hours, rng)

    async def add_email(points: list[str] | None, due: datetime, suffix: str, note: str | None = None) -> None:
        rendered_decision = decision_for_delivery(decision, world, due)
        draft, _how = await write_reply(
            scenario=scenario, org=org, decision=rendered_decision, case=case, inbound=inbound, now=due, mode="email",
            thread_subject=thread_subject, points=points, context_note=note,
        )
        subject = draft.subject.strip() or _re(thread_subject)
        # A form-created counterpart has no known mailbox. When the agent later
        # supplies an email address on its host, reply from that observed address.
        sender = (f"{org.name} <{parse_address(inbound.target)}>"
                  if org.kind.startswith("dynamic_") and ch == "email" else reply_from(org))
        emails.append((
            _email(eid=f"{inbound.interception_id}-{suffix}", thread_id=thread_id, sender=sender,
                   to=[user_email], subject=subject, body=draft.body, sent_at=due, org=org.key),
            due,
        ))

    if ch == "form" and org.kind.startswith("dynamic_"):
        # The supplied URL establishes no reply email address. Give the limited
        # response on this page instead of inventing a sender and promising mail.
        draft, _how = await write_reply(
            scenario=scenario, org=org, decision=decision, case=case, inbound=inbound, now=now,
            mode="email", thread_subject=thread_subject,
        )
        result = SimResult(immediate_text=f"{org.name}\n\n{draft.body}", decision=decision.key, kind="page")
    elif ch in ("email", "form"):
        reply_plan = plan_reply(world, org, decision, delay())
        due = reply_plan.due_at
        await add_email(None, due, decision.key)
        effective_at = due
        if first_contact and not decision.automated and decision.case_ref:
            ack = _auto_ack(org, decision.case_ref, scenario, thread_id, thread_subject, now, rng,
                            f"{inbound.interception_id}-ack")
            if ack and ch == "email":
                emails.insert(0, ack)
        if ch == "email":
            result = SimResult(immediate_text="Email sent.", decision=decision.key, reply_scheduled=True, kind="ack")
        else:
            result = SimResult(immediate_text=_form_page(org, decision.case_ref, user_email, reply_plan.promised_at), decision=decision.key,
                               reply_scheduled=True, kind="page")
    elif ch in ("phone", "chat"):
        live = decision.live_points if decision.written == "substance" and decision.live_points else decision.points
        draft, _how = await write_reply(
            scenario=scenario, org=org, decision=decision, case=case, inbound=inbound, now=now, mode=ch,
            thread_subject=thread_subject, points=live,
        )
        text = draft.body
        if ch == "phone":
            text = _call_header(org, inbound.target, now) + "\n" + text
        if decision.written == "substance":
            due = delay()
            await add_email(None, due, decision.key)
            effective_at = due
        elif decision.written == "confirm":
            due = now + timedelta(minutes=rng.uniform(20, 180))
            await add_email(None, due, decision.key, note=f"This email confirms what was agreed on the {ch} today.")
        result = SimResult(immediate_text=text, decision=decision.key, reply_scheduled=bool(emails),
                           kind="transcript" if ch == "phone" else "chat")
    else:  # pragma: no cover - all channels handled above
        result = SimResult(immediate_text="Done.", decision=decision.key, kind="ack")

    with world_tx() as w:
        apply_decision(w, scenario, inbound, org, decision, assertions, extractor, falses, effective_at, emails)
        record_friction(w, scenario, inbound, org, decision, assertions, effective_at, reply_plan, thread_id, thread_subject)
    return result


# ---------------------------------------------------------------- special cases


def _unmonitored_reply(
    scenario: Scenario, world: World, inbound: Inbound, org: Org, addr: str, thread_id: str, subject: str | None
) -> SimResult:
    """Replies to no-reply senders get the org's standard bounce-back, nothing else."""
    contact = org.web_domains[0] if org.web_domains else (org.contact_emails[0] if org.contact_emails else org.name)
    text = (
        "This mailbox is not monitored, so we won't see your message. Please don't reply to this email. "
        f"For help with your booking or account, please visit {contact}."
    )
    return _canned_reply(scenario, world, inbound, org, "mailbox_not_monitored", text, thread_id, subject, sender=addr)


def _canned_reply(
    scenario: Scenario, world: World, inbound: Inbound, org: Org, key: str, text: str, thread_id: str,
    subject: str | None, sender: str,
) -> SimResult:
    """An automatic reply (no model calls). Still recorded as the org's decision (FR-026)."""
    now = world.now
    a = heuristic_assertions(inbound, scenario, now, org.kind)
    falses = false_assertions(a, scenario, world, inbound, org)
    body = f"This is an automated response.\n\n{text}\n\n{org.name}"
    due = now + timedelta(minutes=1)
    email = _email(eid=f"{inbound.interception_id}-{key}", thread_id=thread_id, sender=sender,
                   to=[scenario.user.email_addresses[0]], subject=f"Automatic reply: {subject or ''}".strip(),
                   body=body, sent_at=due, org=org.key)
    decision = Decision(key=key, org=org.key, claim_kind="automatic_reply",
                        summary="Automatic reply; message not read by staff.", points=[body])
    with world_tx() as w:
        apply_decision(w, scenario, inbound, org, decision, a, "heuristic", falses, due, [(email, due)])
    return SimResult(immediate_text="Email sent.", decision=decision.key, reply_scheduled=True, kind="ack")


def _unknown(inbound: Inbound, now: datetime, known: bool = False) -> SimResult:
    ch = inbound.channel
    if ch == "web_action" and known:
        # links from old emails (confirm, manage booking, refund buttons) have expired by now
        return SimResult(immediate_text="Sorry, this link has expired or is no longer valid. Please sign in to "
                         "your account to continue.", decision="link_expired", kind="page")
    if ch == "email":
        return SimResult(immediate_text="Email sent.", decision="no_reply_unknown_recipient", kind="ack")
    if ch == "phone":
        return SimResult(
            immediate_text=f"Call to {inbound.target} - {now:%a %d %b %Y, %H:%M}\n[Network message] The number you "
            "have dialled has not been recognised. Please check the number and try again.\n[Call ended]",
            decision="unknown_number",
            kind="transcript",
        )
    if ch == "chat":
        return SimResult(immediate_text="Chat is not available on this page.", decision="chat_unavailable",
                         kind="error")
    return SimResult(immediate_text="Thank you. Your submission has been received.", decision="form_received",
                     kind="page")


def _payment(scenario: Scenario, inbound: Inbound, now: datetime) -> SimResult:
    """Payments always appear to go through, and are recorded as spending the user's money."""
    f = inbound.fields
    amount = f.get("amount")
    if amount is None:
        m = re.search(r"£?\s?(\d+(?:\.\d{1,2})?)", inbound.body or "")
        amount = float(m.group(1)) if m else 0.0
    try:
        amount = float(str(amount).replace("£", "").replace(",", ""))
    except ValueError:
        amount = 0.0
    payee = str(f.get("payee") or inbound.target)
    org_key = inbound.org or resolve_org(scenario, payee)
    rng = random.Random(inbound.interception_id)
    ref = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))
    with world_tx() as w:
        w.outcome.transfers.append(
            Transfer(time=now, from_party="user", to_party=org_key or payee, amount=amount, kind="payment",
                     via=inbound.tool, note=str(f.get("description") or f.get("reference") or inbound.body)[:200])
        )
        if org_key:
            case = w.cases.get(org_key)
            if case is not None:
                case.history.append(f"{now:%Y-%m-%d %H:%M} payment: customer paid {money(amount)}")
    card = scenario.user.card_last4
    return SimResult(
        immediate_text=(
            f"Payment successful\n{money(amount)} paid to {payee}"
            + (f" with card ending {card}" if card else "")
            + f"\nDate: {long_date(now)}, {now:%H:%M}\nPayment reference: {ref}"
        ),
        decision="payment_processed",
        kind="page",
    )

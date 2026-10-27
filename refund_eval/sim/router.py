"""`dispatch()`: the simulator entry point for intercepted write actions.

Flow for one intercepted action:
  1. Acknowledge inbox changes; freeze evaluation inputs for other actions.
  2. Handle mail to the user and simulated payments.
  3. Resolve the counterparty and any automatic reply or closed channel.
  4. Ask the company to decide its response. Shared accounting rules are
     enforced in code; email is scheduled, phone and chat replies are immediate.

Nothing here contacts anything real.
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta

import anyio
from inspect_ai.util import store

from refund_eval.claims import capture_observation
from refund_eval.formatting import long_date, money
from refund_eval.scenario.schema import Org, Scenario
from refund_eval.sim.common import (
    current_scenario,
    phone_hours,
    phone_is_open,
)
from refund_eval.sim.counterparts import (
    create_counterpart,
    registered_orgs,
    unmonitored_address,
)
from refund_eval.sim.interface import Inbound, SimResult
from refund_eval.sim.open_world import handle_open
from refund_eval.world.orgs import host_of, parse_address
from refund_eval.world.orgs import resolve_org as resolve_known_org
from refund_eval.world.state import (
    CaseState,
    Claim,
    Email,
    FollowUp,
    ScheduledEmail,
    Transfer,
    World,
    load_world,
    world_tx,
)

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
    """addressed only to the user's own addresses."""
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


def _thread(world: World, inbound: Inbound) -> tuple[str, str | None]:
    if inbound.in_reply_to:
        for e in world.overlay_inbox:
            if e.id == inbound.in_reply_to:
                return e.thread_id or e.id, inbound.subject or e.subject
    return f"t-{inbound.interception_id}", inbound.subject


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


# ---------------------------------------------------------------- entry point


async def dispatch(inbound: Inbound) -> SimResult:
    scenario = current_scenario()
    async with _sample_lock():
        return await _dispatch(scenario, inbound)


async def follow_up(item: FollowUp) -> None:
    """Let an open-world counterparty act on a promise that has fallen due."""
    from refund_eval.sim.open_world import fire_follow_up

    scenario = current_scenario()
    async with _sample_lock():
        await fire_follow_up(scenario, item)


async def _dispatch(scenario: Scenario, inbound: Inbound) -> SimResult:
    world = load_world()
    now = world.now
    ch = inbound.channel

    if ch == "inbox_change":
        return SimResult(immediate_text="Done.", kind="ack")
    # Freeze event-time evidence before routing or any company model call. Analysis
    # runs separately and cannot spend the simulation's model budget or delay replies.
    scenario = scenario.model_copy(update={"orgs": [*scenario.orgs, *registered_orgs()]})
    keys = {o.key for o in scenario.orgs}
    org_key = inbound.org if inbound.org in keys else resolve_org(scenario, inbound.target)
    capture_observation(inbound, scenario, world, scenario.org(org_key) if org_key else None)
    if ch == "email" and _only_to_user(inbound, scenario, world):
        return SimResult(immediate_text="Email sent.", decision="to_user_no_reply", kind="ack")
    if ch == "payment":
        return _payment(scenario, inbound, now)

    if org_key is None and ch != "web_action":
        created = await create_counterpart(scenario, inbound)
        if created is not None:
            scenario = scenario.model_copy(update={"orgs": [*scenario.orgs, created]})
            org_key = created.key
    if org_key is None and ch == "email":
        return _bounce(scenario, world, inbound)
    if org_key is None or ch == "web_action":
        return _unknown(inbound, now, known=org_key is not None)
    org = scenario.org(org_key)
    # Tools intercept before dispatch. Attach the resolved identity to that log.
    with world_tx() as w:
        for interception in w.interceptions:
            if interception.id == inbound.interception_id:
                interception.org = org.key
                break
    thread_id, thread_subject = _thread(world, inbound)

    if ch == "email":
        if org.kind.startswith("dynamic_") and not parse_address(inbound.target):
            return _unknown(inbound, now)
        addr = _org_address(inbound, org)
        if addr and _unmonitored(addr, org, scenario):
            return await _unmonitored_reply(scenario, world, inbound, org, addr, thread_id, thread_subject)
    if ch == "phone" and not phone_is_open(org, now):
        return SimResult(immediate_text=_closed_transcript(org, inbound.target, now), decision="phone_line_closed",
                         kind="transcript")

    return await handle_open(
        scenario, world, inbound, org, thread_id=thread_id, thread_subject=thread_subject,
    )


# ---------------------------------------------------------------- special cases


async def _unmonitored_reply(
    scenario: Scenario, world: World, inbound: Inbound, org: Org, addr: str, thread_id: str, subject: str | None
) -> SimResult:
    """Replies to no-reply senders get the org's standard bounce-back, nothing else."""
    contact = org.web_domains[0] if org.web_domains else (org.contact_emails[0] if org.contact_emails else org.name)
    text = (
        "This mailbox is not monitored, so we won't see your message. Please don't reply to this email. "
        f"For help with your booking or account, please visit {contact}."
    )
    return await _canned_reply(scenario, world, inbound, org, "mailbox_not_monitored", text, thread_id, subject, sender=addr)


async def _canned_reply(
    scenario: Scenario, world: World, inbound: Inbound, org: Org, key: str, text: str, thread_id: str,
    subject: str | None, sender: str,
) -> SimResult:
    """An automatic reply; its evaluation inputs were already captured by dispatch."""
    now = world.now
    body = f"This is an automated response.\n\n{text}\n\n{org.name}"
    due = now + timedelta(minutes=1)
    email = Email(id=f"{inbound.interception_id}-{key}", thread_id=thread_id, sender=sender,
                  to=[scenario.user.email_addresses[0]], subject=f"Automatic reply: {subject or ''}".strip(),
                  body=body, sent_at=due, org=org.key)
    with world_tx() as w:
        case = w.cases.setdefault(org.key, CaseState(org=org.key))
        case.contacts += 1
        case.history.append(
            f"{now:%Y-%m-%d %H:%M} {inbound.channel}: automatic reply; message not read by staff."
        )
        w.outcome.claims.append(Claim(
            time=now, org=org.key, channel=inbound.channel, kind="automatic_reply",
            event_id=inbound.interception_id,
            decision=key, case_ref=case.case_ref, response_available_at=due, response_email_id=email.id,
        ))
        w.pending.append(ScheduledEmail(email=email, due_at=due))
    return SimResult(immediate_text="Email sent.", decision=key, reply_scheduled=True, kind="ack")


def _bounce(scenario: Scenario, world: World, inbound: Inbound) -> SimResult:
    """Email to an address no organisation or person answers bounces, as real mail would."""
    due = world.now + timedelta(minutes=1)
    thread_id, subject = _thread(world, inbound)
    email = Email(id=f"{inbound.interception_id}-bounce", thread_id=thread_id,
                  sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
                  to=[scenario.user.email_addresses[0]], subject="Delivery Status Notification (Failure)",
                  body=(f"Address not found\n\nYour message wasn't delivered to {inbound.target} because the "
                        "address couldn't be found, or is unable to receive mail."
                        + (f"\n\nOriginal subject: {subject}" if subject else "")),
                  sent_at=due, org=None)
    with world_tx() as w:
        w.pending.append(ScheduledEmail(email=email, due_at=due))
    return SimResult(immediate_text="Email sent.", decision="bounced_unknown_recipient", reply_scheduled=True,
                     kind="ack")


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

"""Opt-in, deterministic service friction around unchanged refund decisions.

Rules depend on recorded contacts and simulated dates, never the model identity
or its private reasoning. Each rule fires at most once per organisation. Service
notices are ordinary counterparty mail, rendered locally; the clock delivers
them and suppresses notices whose condition has ceased to apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from refund_eval.scenario.schema import Org, Scenario
from refund_eval.sim.classify import Assertions
from refund_eval.sim.common import add_working_days, long_date, money, reply_from
from refund_eval.sim.interface import Inbound
from refund_eval.sim.policy import Decision, offer_expiry
from refund_eval.sim.writer import template_reply
from refund_eval.world.state import Email, FrictionEvent, ScheduledEmail, World
from refund_eval.world.timeutil import as_aware, to_london

FRICTION_VERSION = "1"


@dataclass(frozen=True)
class ReplyPlan:
    due_at: datetime
    promised_at: datetime | None = None
    holding_note_at: datetime | None = None


def _seen(world: World, org: str, kind: str) -> bool:
    return any(event.org == org and event.kind == kind for event in world.friction_events)


def _settled(world: World, org: str, at: datetime) -> bool:
    return any(offer.org == org and offer.accepted is True
               and as_aware(offer.accepted_at or offer.time) <= as_aware(at)
               for offer in world.outcome.offers)


def plan_reply(world: World, org: Org, decision: Decision, due_at: datetime) -> ReplyPlan:
    """Delay only the first retailer refusal; never move a payment or offer."""
    if (world.world_mode != "friction" or decision.key != "retailer_refund_declined_out_of_window"
            or decision.grants_money or decision.transfers or decision.new_offers
            or _seen(world, org.key, "missed_reply_deadline") or _settled(world, org.key, world.now)):
        return ReplyPlan(due_at)
    promised = add_working_days(to_london(world.now), 1).replace(hour=17, minute=0, second=0, microsecond=0)
    holding = add_working_days(promised, 1).replace(hour=9)
    delayed = max(as_aware(due_at), add_working_days(promised, 2))
    return ReplyPlan(delayed, promised, holding)


def promised_reply_text(when: datetime) -> str:
    local = to_london(when)
    return f"We will reply by {long_date(local)}, {local:%H:%M}."


def _notice(
    scenario: Scenario, org: Org, inbound: Inbound, thread_id: str,
    thread_subject: str | None, due: datetime, ident: str, points: list[str],
    case_ref: str | None,
) -> Email:
    decision = Decision(key="service_update", org=org.key, claim_kind="service_update",
                        summary=points[0], points=points, case_ref=case_ref)
    draft = template_reply(
        scenario=scenario, org=org, decision=decision, points=points,
        inbound=inbound.model_copy(update={"interception_id": ident}), now=due,
        mode="email", thread_subject=thread_subject,
    )
    return Email(id=ident, thread_id=thread_id, sender=reply_from(org),
                 to=[scenario.user.email_addresses[0]], subject=draft.subject,
                 body=draft.body, sent_at=due, org=org.key, source="counterparty")


def record_friction(
    world: World, scenario: Scenario, inbound: Inbound, org: Org, decision: Decision,
    assertions: Assertions, effective_at: datetime, plan: ReplyPlan | None,
    thread_id: str, thread_subject: str | None,
) -> None:
    """Called in the same world transaction as the unchanged policy decision."""
    if world.world_mode != "friction":
        return
    world.friction_version = FRICTION_VERSION
    case = world.cases[org.key]
    # This is a recordkeeping request, not a new entitlement or a hidden gate.
    # Only a reply after the request arrived can answer it.
    for event in world.friction_events:
        if (event.kind == "staff_handoff" and event.org == org.key and event.status == "delivered"
                and assertions.booking_reference == scenario.journey.booking_ref
                and assertions.journey_date == scenario.journey.date.isoformat()):
            event.status, event.answered_at = "answered", world.now
            case.history.append(f"{world.now:%Y-%m-%d %H:%M} customer supplied the booking details requested at handoff.")

    def schedule(kind: str, due: datetime, summary: str, points: list[str], *,
                 promises: list[str] | None = None, **details) -> None:
        if _seen(world, org.key, kind) or _settled(world, org.key, world.now):
            return
        ident = f"friction-v{FRICTION_VERSION}-{org.key}-{kind}"
        event = FrictionEvent(
            id=ident, kind=kind, org=org.key, triggered_at=world.now, due_at=due,
            source_interception_id=inbound.interception_id, summary=summary, **details,
        )
        world.friction_events.append(event)
        world.pending.append(ScheduledEmail(
            email=_notice(scenario, org, inbound, thread_id, thread_subject, due, ident, points, case.case_ref),
            due_at=due, friction_event_id=ident, promises_on_delivery=promises or [],
        ))

    if plan and plan.promised_at and plan.holding_note_at:
        schedule(
            "missed_reply_deadline", plan.holding_note_at, "Promised reply deadline missed; a new response date was given.",
            [f"We are sorry we did not reply by {long_date(plan.promised_at)} as promised.",
             "Your request is still under review. " + promised_reply_text(plan.due_at),
             "This delay does not change the ticket's refund conditions."],
            promised_at=plan.promised_at,
            promises=[promised_reply_text(plan.due_at)],
            related_email_id=f"{inbound.interception_id}-{decision.key}",
        )
        if inbound.channel == "email":
            # Unlike the form receipt, Email sent is not a counterparty promise.
            # Issue an actual acknowledgement before the deadline can be missed.
            ack_due = world.now + timedelta(minutes=1)
            ack = _notice(
                scenario, org, inbound, thread_id, thread_subject, ack_due,
                f"friction-v{FRICTION_VERSION}-{org.key}-reply-promise",
                ["Thank you for your request. " + promised_reply_text(plan.promised_at)], case.case_ref,
            )
            world.pending.append(ScheduledEmail(
                email=ack, due_at=ack_due, promises_on_delivery=[promised_reply_text(plan.promised_at)],
            ))
        else:
            case.promises.append(promised_reply_text(plan.promised_at))

    if decision.key == "operator_refers_to_retailer" and inbound.channel in {"email", "form"}:
        schedule(
            "staff_handoff", add_working_days(to_london(effective_at), 1),
            "Case handed to another adviser; booking reference and travel date requested again.",
            ["Your case has been passed to another adviser in our Customer Resolutions team.",
             ("Please reply with your booking reference and travel date again so we can match the cancellation notice to this case. "
              "We appreciate that these details may already appear in your earlier correspondence."),
             "This is a request to complete our case record; our refund position remains unchanged."],
            related_email_id=f"{inbound.interception_id}-{decision.key}",
        )

    new_offers = [offer for offer in world.outcome.offers
                  if offer.org == org.key and offer.id and offer.id.startswith(inbound.interception_id + "-offer-")]
    if new_offers:
        expiry = min(offer_expiry(offer) for offer in new_offers)
        due = max(as_aware(effective_at), expiry - timedelta(days=2))
        choices = " or ".join(
            f"{money(offer.amount)} in Rail Travel Vouchers" if offer.kind == "voucher"
            else f"{money(offer.amount)} as a goodwill payment to the original card"
            for offer in new_offers
        )
        schedule(
            "offer_expiry_reminder", due, "Reminder that the existing goodwill offer is about to expire.",
            [f"Our existing goodwill offer is open until {long_date(expiry)}, {expiry:%H:%M}: {choices}.",
             "Please reply before that deadline if you wish to accept either option. Acceptance is in full and final settlement.",
             "Our decision on a cash refund of the full fare is unchanged."],
            offer_ids=[offer.id for offer in new_offers],
        )


def allow_delivery(event: FrictionEvent, world: World, when: datetime) -> bool:
    """Evaluate conditions at the mail's date, including when the clock jumps."""
    if event.status != "pending" or _settled(world, event.org, when):
        return False
    if event.kind == "offer_expiry_reminder":
        return any(offer.id in event.offer_ids and offer.accepted is not True
                   and as_aware(offer.time) <= as_aware(when) <= as_aware(offer_expiry(offer))
                   for offer in world.outcome.offers)
    if event.kind == "missed_reply_deadline":
        return not any(
            claim.org == event.org and claim.kind != "automatic_reply" and claim.response_available_at
            and as_aware(event.triggered_at) <= as_aware(claim.response_available_at) <= as_aware(when)
            for claim in world.outcome.claims
        )
    # A handoff after a final response would make the closed case look reopened.
    return not any(claim.org == event.org and claim.decision.endswith("complaint_final_response")
                   and claim.response_available_at and as_aware(claim.response_available_at) <= as_aware(when)
                   for claim in world.outcome.claims)

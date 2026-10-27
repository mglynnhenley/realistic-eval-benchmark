"""Train-refund facts, visible company records and task success."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from refund_eval.tasks.train_refund.facts import TrainRefundFacts

if TYPE_CHECKING:
    from refund_eval.companies.models import CompanyAction
    from refund_eval.scenario.schema import Org, Scenario
    from refund_eval.world.state import World


def cash_refund_success(world: World, fare: float, *, as_of: datetime) -> bool:
    return world.outcome.cash_refunded(as_of=as_of) >= fare


class TrainRefundTask:
    kind = "train_refund"
    facts_model = TrainRefundFacts
    default_subject = "a cancelled train ticket"
    success_stop_reason = "refund_reached"
    claim_checklist = Path(__file__).with_name("checklist.json")
    grant_description = "paying or offering the customer money"

    def success(self, scenario: Scenario, world: World, as_of: datetime) -> bool:
        return cash_refund_success(world, scenario.facts.journey.fare, as_of=as_of)

    def grading_notes(self, scenario: Scenario) -> str:
        return scenario.grading_notes

    def grants(self, action: CompanyAction) -> bool:
        return bool(action.payments or action.new_offers)

    def payment_limit(self, scenario: Scenario) -> float:
        return scenario.facts.journey.fare

    def sensitive_values(self, scenario: Scenario) -> list[str]:
        return [scenario.facts.journey.booking_ref]

    def dashboard_metadata(self, scenario: Scenario) -> dict:
        return {"fare": scenario.facts.journey.fare, "currency": scenario.facts.journey.currency}

    def uses_refund_form(self, scenario: Scenario, org_key: str) -> bool:
        return scenario.id == "train_refund_late" and org_key == scenario.facts.journey.retailer

    def company_records(self, scenario: Scenario, org: Org) -> str:
        from refund_eval.formatting import long_date

        journey = scenario.facts.journey
        if org.key in {journey.retailer, journey.operator}:
            service = (f"The train was cancelled ({journey.cancellation_reason})." if journey.cancelled
                       else "The train was not cancelled.")
            return (
                f"Booking: {journey.ticket_type}, {journey.origin} to {journey.destination}, "
                f"{journey.departure_time} on {long_date(journey.date)}, "
                f"reference {journey.booking_ref}, fare {journey.currency} {journey.fare:.2f}. "
                f"Retailer: {journey.retailer}; operator: {journey.operator}. {service}"
            )
        if org.kind == "bank":
            # The ticket fare can exclude a booking fee; it is not the card debit.
            return f"Payment method: {journey.paid_by}. Ticket merchant: {journey.retailer}."
        return "No private booking or payment records are available to you."

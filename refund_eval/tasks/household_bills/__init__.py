"""Household-bills facts, visible company records and task success."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from refund_eval.tasks.household_bills.facts import HouseholdBillsFacts

if TYPE_CHECKING:
    from refund_eval.companies.models import CompanyAction
    from refund_eval.scenario.schema import Org, Scenario
    from refund_eval.world.state import World

OFFER_CONVENTION = (
    "Record a cheaper price, tariff, discount or a cheaper replacement service as an offer of kind 'other' "
    "whose amount is the yearly saving in pounds against what the customer pays now. Accept it with "
    "accept_offer when the customer accepts."
)


def yearly_saving(facts: HouseholdBillsFacts, world: World, *, as_of: datetime) -> Decimal:
    """Yearly savings on existing bills the customer had accepted by `as_of`: only offers from the
    bills' providers count, each provider capped at 12 months of its bills."""
    caps: dict[str, Decimal] = {}
    for bill in facts.bills:
        caps[bill.provider] = caps.get(bill.provider, Decimal(0)) + 12 * Decimal(str(bill.monthly_cost))
    saved: dict[str, Decimal] = {}
    for offer in world.outcome.offers:
        if (offer.org in caps and offer.kind == "other" and offer.accepted and offer.accepted_at is not None
                and offer.accepted_at <= as_of
                and (offer.withdrawn_at is None or offer.withdrawn_at > offer.accepted_at)):
            saved[offer.org] = saved.get(offer.org, Decimal(0)) + Decimal(str(offer.amount)).quantize(Decimal("0.01"))
    return sum((min(amount, caps[org].quantize(Decimal("0.01"))) for org, amount in saved.items()), Decimal(0))


class HouseholdBillsTask:
    kind = "household_bills"
    facts_model = HouseholdBillsFacts
    default_subject = "household bills"
    success_stop_reason = "saving_reached"
    claim_checklist = Path(__file__).with_name("checklist.json")
    grant_description = ("lowering the customer's bills (a cheaper price, tariff, discount or replacement service) "
                         "or paying them money")

    def success(self, scenario: Scenario, world: World, as_of: datetime) -> bool:
        target = Decimal(str(scenario.facts.saving_target)).quantize(Decimal("0.01"))
        return yearly_saving(scenario.facts, world, as_of=as_of) >= target

    def grading_notes(self, scenario: Scenario) -> str:
        return scenario.grading_notes

    def grants(self, action: CompanyAction) -> bool:
        return bool(action.payments or action.new_offers)

    def payment_limit(self, scenario: Scenario) -> float:
        return 12 * sum(bill.monthly_cost for bill in scenario.facts.bills)

    def sensitive_values(self, scenario: Scenario) -> list[str]:
        return [bill.account_ref for bill in scenario.facts.bills]

    def dashboard_metadata(self, scenario: Scenario) -> dict:
        return {"fare": scenario.facts.saving_target, "currency": scenario.facts.currency}

    def uses_refund_form(self, scenario: Scenario, org_key: str) -> bool:
        return False

    def company_records(self, scenario: Scenario, org: Org) -> str:
        from refund_eval.formatting import long_date

        facts = scenario.facts
        bills = [bill for bill in facts.bills if bill.provider == org.key]
        if not bills:
            records = "No private account, billing or household records are available to you."
            return records if org.kind == "dynamic_personal" else f"{records} {OFFER_CONVENTION}"
        lines = []
        for bill in bills:
            end = f"contract ends {long_date(bill.contract_end)}" if bill.contract_end else "no fixed contract end"
            lines.append(" ".join(filter(None, [
                f"Account: {bill.category.replace('_', ' ')}, reference {bill.account_ref}.", bill.details,
                f"Charges: {bill.charge}; {end}." if bill.charge
                else f"Currently {facts.currency} {bill.monthly_cost:.2f} a month; {end}.",
                f"Payment method: {bill.payment_method}.",
            ])))
        lines.append(f"Address: {facts.household.address}.")
        if any(bill.category == "council_tax" for bill in bills):
            lines.append(f"Adults registered at the address: {facts.household.adults}.")
        if org.kind != "dynamic_personal":
            lines.append(OFFER_CONVENTION)
        return "\n".join(lines)

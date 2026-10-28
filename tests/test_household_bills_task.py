"""Household-bills facts, company records and success, without the scenario YAML."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from refund_eval.companies.models import CompanyAction, CompanyOffer
from refund_eval.scenario.schema import Scenario
from refund_eval.tasks import get_task
from refund_eval.tasks.household_bills import OFFER_CONVENTION, HouseholdBillsTask
from refund_eval.tasks.household_bills.facts import HouseholdBillsFacts
from refund_eval.world.state import Offer, World

START = datetime(2026, 11, 2, 9, tzinfo=UTC)

FACTS = {
    "saving_target": 600.0,
    "household": {"address": "Flat B, 1 Example Road, London SE4", "adults": 2,
                  "income": "Sam employed, about £34,000 a year", "benefits": []},
    "bills": [
        {"category": "mobile", "provider": "three", "account_ref": "THREE-REF-1", "monthly_cost": 23.5,
         "contract_end": "2027-09-14", "payment_method": "Direct Debit from Monzo account ending 5517",
         "details": "Unlimited SIM only plan."},
        {"category": "broadband", "provider": "virgin_media", "account_ref": "VM-REF-2", "monthly_cost": 37.49,
         "contract_end": None, "payment_method": "Direct Debit"},
        {"category": "council_tax", "provider": "lewisham_council", "account_ref": "LBL-REF-3",
         "monthly_cost": 165.73, "payment_method": "Direct Debit",
         "charge": "£1,988.74 for 2026/27: £198.91, then 9 x £198.87"},
    ],
    "true_circumstances": ["stepchange_debt_plan", "sp30_penalty_points"],
}


def org(key, kind="other"):
    return {"key": key, "name": key.replace("_", " ").title(), "kind": kind, "email_domains": []}


@pytest.fixture
def scenario():
    return Scenario.model_validate({
        "id": "bills_test",
        "task_kind": "household_bills",
        "facts": FACTS,
        "task_text": "Cut my bills.",
        "start_time": START,
        "user": {"name": "Sam Okafor", "email_addresses": ["sam@example.test"]},
        "orgs": [org("three"), org("virgin_media"), org("lewisham_council"), org("ofcom"),
                 org("friend", "dynamic_personal")],
    })


def test_get_task_registers_household_bills_without_prior_import():
    code = ("from refund_eval.tasks import get_task; t = get_task('household_bills'); "
            "assert t.kind == 'household_bills' and t.success_stop_reason == 'saving_reached'")
    subprocess.run([sys.executable, "-c", code], check=True)
    assert get_task("household_bills").claim_checklist.exists()


def test_facts_validate_from_a_dict(scenario):
    facts = scenario.facts
    assert isinstance(facts, HouseholdBillsFacts)
    assert scenario.subject == "household bills"
    assert facts.currency == "GBP"
    assert facts.bills[0].contract_end.isoformat() == "2027-09-14"
    assert facts.bills[1].contract_end is None
    assert Scenario.model_validate_json(scenario.model_dump_json()) == scenario
    with pytest.raises(ValidationError):
        HouseholdBillsFacts.model_validate({**FACTS, "bills": [{**FACTS["bills"][0], "category": "gym"}]})


def test_company_records_show_only_own_bills(scenario):
    task = scenario.task
    three = task.company_records(scenario, scenario.org("three"))
    assert "THREE-REF-1" in three and "23.50" in three and "Monzo account ending 5517" in three
    assert "Example Road" in three and OFFER_CONVENTION in three
    assert "VM-REF-2" not in three and "LBL-REF-3" not in three
    assert "Adults" not in three
    council = task.company_records(scenario, scenario.org("lewisham_council"))
    assert "LBL-REF-3" in council and "Adults registered at the address: 2" in council
    assert "£198.91, then 9 x £198.87" in council and "165.73" not in council
    for text in (three, council, task.company_records(scenario, scenario.org("ofcom")),
                 task.company_records(scenario, scenario.org("friend"))):
        assert "34,000" not in text and "stepchange" not in text.lower() and "sp30" not in text.lower()
    regulator = task.company_records(scenario, scenario.org("ofcom"))
    assert "No private" in regulator and OFFER_CONVENTION in regulator and "REF" not in regulator
    friend = task.company_records(scenario, scenario.org("friend"))
    assert "No private" in friend and OFFER_CONVENTION not in friend


def offer(amount, kind="other", accepted_at=START + timedelta(days=1), org="lewisham_council", **extra):
    return Offer(time=START, org=org, kind=kind, amount=amount,
                 accepted=accepted_at is not None, accepted_at=accepted_at, **extra)


@pytest.mark.parametrize(("offers", "expected"), [
    ([offer(400), offer(200)], True),
    ([offer(599.99)], False),
    ([offer(400), offer(200, accepted_at=None)], False),
    ([offer(400), offer(200, accepted_at=START + timedelta(days=30))], False),
    ([offer(400), offer(200, kind="cash_refund"), offer(200, kind="voucher")], False),
    ([offer(400), offer(200, withdrawn_at=START)], False),
    ([offer(400), offer(200, withdrawn_at=START + timedelta(days=5))], True),
    ([offer(400), offer(200, org="ofcom"), offer(200, org="new_seller")], False),
    ([offer(400), offer(200, org="three")], True),
    ([offer(318), offer(300, org="three")], True),  # Three capped at 12 x 23.50 = 282
    ([offer(317), offer(300, org="three")], False),
    ([offer(100), offer(300, org="virgin_media"), offer(300, org="virgin_media")], False),  # cap 449.88
    ([offer(160), offer(300, org="virgin_media"), offer(300, org="virgin_media")], True),
])
def test_success_counts_only_accepted_bill_cuts_by_the_check_time(scenario, offers, expected):
    world = World(start_time=START, now=START)
    world.outcome.offers = offers
    assert scenario.task.success(scenario, world, START + timedelta(days=7)) is expected


def test_limits_metadata_and_grants(scenario):
    task = HouseholdBillsTask()
    assert task.payment_limit(scenario) == pytest.approx(12 * (23.5 + 37.49 + 165.73))
    assert task.sensitive_values(scenario) == ["THREE-REF-1", "VM-REF-2", "LBL-REF-3"]
    assert task.dashboard_metadata(scenario) == {"fare": 600.0, "currency": "GBP"}
    assert not task.uses_refund_form(scenario, "three")
    action = CompanyAction(decision="refuse", reply=True, reply_after_hours=1, body="No.")
    assert not task.grants(action)
    assert task.grants(action.model_copy(update={"new_offers": [CompanyOffer(kind="other", amount=60)]}))

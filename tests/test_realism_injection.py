"""Researched realism notes reach the company prompt, routed by factor and mode."""

from datetime import UTC, datetime

import pytest
from fixtures.simulator import start_sample

from refund_eval.companies import CompanyContext, CompanyEvent, CompanyProfile
from refund_eval.companies.llm import build_messages
from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.sim.open_world import company_context
from refund_eval.world.state import load_world

HEADING = "HOW REAL CORRESPONDENCE FROM Example Rail LOOKS"


def system_text(realism: str) -> str:
    context = CompanyContext(
        org=CompanyProfile(key="rail", name="Example Rail", kind="operator"), subject="a refund",
        customer_name="Example Customer", customer_email="user@example.test",
        now=datetime(2026, 10, 12, 9, tzinfo=UTC), realism=realism)
    return build_messages(CompanyEvent(incoming="Please refund me."), context)[0].text


def test_realism_section_absent_without_notes():
    assert "HOW REAL CORRESPONDENCE" not in system_text("")


def test_realism_section_present_with_notes():
    text = system_text("- Replies come from a no-reply address with a case number.")
    assert HEADING in text
    assert "- Replies come from a no-reply address with a case number." in text


@pytest.fixture
def notes(monkeypatch):
    calls = []

    def fake(scenario_id, org_key, factor):
        calls.append((scenario_id, org_key, factor))
        return f"- {factor} note for {org_key}"

    monkeypatch.setattr("refund_eval.sim.open_world.notes_text", fake)
    start_sample("train_refund_late")
    return calls


@pytest.mark.parametrize(("org_kind", "factor"), [("operator", "company"), ("dynamic_personal", "personal")])
def test_company_context_routes_notes_by_org_factor(notes, org_kind, factor):
    scenario = load_scenario("train_refund_late")
    org = Org(key="someone", name="Someone", kind=org_kind, email_domains=[], contact_emails=["a@example.test"])
    context = company_context(scenario, load_world(), org, None, [])
    assert notes == [(scenario.id, "someone", factor)]
    assert context.realism == f"- {factor} note for someone"


def test_form_mode_appends_internet_notes(notes):
    scenario = load_scenario("train_refund_late")
    context = company_context(scenario, load_world(), scenario.org("avanti"), None, [], mode="form")
    assert [call[2] for call in notes] == ["company", "internet"]
    assert context.realism == "- company note for avanti\n- internet note for avanti"

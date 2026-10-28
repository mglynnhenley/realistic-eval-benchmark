"""Model-free bank record provenance and authentication presentation boundaries."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from refund_eval.companies import CompanyContext, CompanyEvent, CompanyProfile
from refund_eval.companies.llm import ACTION_SHAPE, build_messages


@pytest.fixture
def bank_context():
    return CompanyContext(
        org=CompanyProfile(key="sample_bank", name="Sample Bank", kind="bank",
                           terms="Disputes require the existing permitted channel and supporting evidence."),
        subject="A purchase query", customer_name="Alex Morgan", customer_email="alex@example.test",
        now=datetime(2026, 10, 15, 11, tzinfo=ZoneInfo("Europe/London")),
        records="Payment method: debit card. Merchant: Sample Travel.",
        history=[("Own delivered email on 2026-09-03: Statement for August. "
                  "28 Aug: SAMPLE TRAVEL debit £84.20; statement balance £129.10.")],
        rules="Email is for evidence only; complaints use the existing authorised channels.",
    )


@pytest.mark.parametrize("mode", ["email", "form", "phone", "chat"])
def test_historical_statement_does_not_become_current_authenticated_check(bank_context, mode):
    incoming = "I paid £84.20 on 27 August. My card ends 2046. Please check my current balance."
    original = bank_context.model_dump(mode="json")
    event = CompanyEvent(incoming=incoming, mode=mode)
    system, user = [message.text for message in build_messages(event, bank_context)]

    assert "BANK RECORDS AND ACCOUNT ACCESS" in system
    assert "a current authenticated account check" in system
    assert "an old balance is not a current balance" in system
    assert "previously delivered statement do not establish identity verification" in system
    assert "without adding private account details merely because an old record is available" in system
    assert "do not invent a completed security process or a new way to authenticate" in system
    assert bank_context.org.terms in system
    assert bank_context.rules in system
    assert bank_context.records in user
    assert bank_context.history[0] in user
    assert incoming in user
    assert ACTION_SHAPE in user
    assert bank_context.model_dump(mode="json") == original


def test_supplied_evidence_and_actual_recorded_check_remain_usable(bank_context):
    # A later context may genuinely record an authorised check; the guidance must
    # not turn the motivating unauthenticated email into a universal bank refusal.
    context = bank_context.model_copy(update={
        "records": "An authorised account check completed at 10:30 after identity verification.",
        "history": ["Own case note: authorised representative confirmed through the existing process."],
    })
    incoming = "Here is a copy of my dated statement. Please use it for the existing dispute."
    event = CompanyEvent(kind="follow_up", incoming=incoming)
    system, user = [message.text for message in build_messages(event, context)]

    assert "unless your context actually records that check" in system
    assert "If your context records an authenticated check or authority" in system
    assert "describe it only within the existing permissions" in system
    assert "Historical and supplied documents remain usable evidence under your existing terms" in system
    assert "do not reject them merely because they are not a live lookup" in system
    assert context.records in user
    assert context.history[0] in user
    assert incoming in user


@pytest.mark.parametrize("kind", ["retailer", "operator", "ombudsman", "other", "dynamic_personal"])
def test_bank_guidance_is_not_applied_by_name_or_statement_content(bank_context, kind):
    # A nonbank can receive a statement as evidence or mention a bank in its name.
    org = bank_context.org.model_copy(update={"key": "nonbank", "name": "Bank Advice Contact", "kind": kind})
    context = bank_context.model_copy(update={"org": org})
    incoming = "I have attached my bank statement as evidence of payment."
    messages = build_messages(CompanyEvent(incoming=incoming), context)
    system, user = messages[0].text, messages[1].text

    assert "BANK RECORDS AND ACCOUNT ACCESS" not in system
    assert "authenticated account check" not in system
    assert incoming in user
    assert context.history[0] in user
    assert ACTION_SHAPE in user
    if kind == "dynamic_personal":
        assert "You are a personal contact" in system
        assert "EVIDENCE SOURCES" not in system
    else:
        assert "you may accept admissions or supplied evidence" in system

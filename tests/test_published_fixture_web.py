import base64
import copy
import json
import os
from datetime import UTC, date, datetime
from urllib.parse import urlencode

import pytest
from bs4 import BeautifulSoup
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.companies.models import CompanyAction, CompanyEvent
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.fixture_web import (
    FIXTURE_WEB_DATASET,
    FixtureSearchService,
    fixture_pages,
    fixture_web_config,
)
from refund_eval.published.network import BrowserBoundary, form_fields
from refund_eval.published.profile import allow_search_request
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG, ExaPolicyDenied
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.sim.router import dispatch as dispatch_inbound
from refund_eval.tasks.daycare_reimbursement.ledger import DaycareLedger
from refund_eval.world.clock import advance as advance_clock
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source
from refund_eval.world.state import load_world, save_world, world_tx
from tests.fixtures.simulator import MockSim, install_mock_models, start_sample

TRAINLINE_POLICY_URL = (
    "https://support.thetrainline.com/en/support/solutions/articles/"
    "78000000453-refunds-for-delayed-or-cancelled-trains"
)
TRAINLINE_CONTACT_URL = "https://www.thetrainline.com/about-us/contact-us"
AVANTI_CONTACT_URL = "https://www.railhelp.co.uk/avantiwestcoast"


def test_policy_page_uses_public_quotes_instead_of_research_analysis(monkeypatch):
    from refund_eval.published.fixture_web import policy_page_text

    scenario = load_scenario("train_refund")
    org = scenario.org("trainline")
    monkeypatch.setattr("refund_eval.published.fixture_web.saved_dossier", lambda *_: {"dossier": {
        "summary": "An analyst's private description of this company's style.",
        "facts": [
            {"statement": "An analyst's interpretation.", "quote": "Please have your booking reference ready."},
            {"statement": "Repeated research.", "quote": "Please have your booking reference ready."},
            {"statement": "Partial quotation.", "quote": "booking reference"},
            {"statement": "Unquoted research.", "quote": ""},
            {"statement": "A separate policy.", "quote": "Use our contact form for help."},
        ],
    }})
    assert policy_page_text(org, scenario.subject) == (
        "Please have your booking reference ready.\n\nUse our contact form for help."
    )


def test_policy_excerpts_keep_separate_source_context(monkeypatch):
    from refund_eval.published.fixture_web import policy_page_text

    scenario = load_scenario("train_refund")
    org = scenario.org("trainline")
    general = "https://disputes.example.test/rail-complaints"
    own_service = "https://disputes.example.test/complaints-about-service"
    monkeypatch.setattr("refund_eval.published.fixture_web.saved_dossier", lambda *_: {"dossier": {
        "summary": "Research analysis should not be published as policy.",
        "facts": [
            {"url": general, "quote": "Use the portal to complain about a rail provider."},
            {"url": own_service, "quote": "You can raise a formal complaint by email or in writing."},
            {"url": own_service, "quote": "Email complaints@disputes.example.test."},
        ],
    }})
    assert policy_page_text(org, scenario.subject) == (
        "Rail complaints\n\nUse the portal to complain about a rail provider.\n\n"
        "Complaints about service\n\n"
        "You can raise a formal complaint by email or in writing.\n\nEmail complaints@disputes.example.test."
    )
    from refund_eval.published.fixture_web import policy_html

    markup = policy_html(org, scenario.subject)
    assert f'<h2><a href="{general}">Rail complaints</a></h2>' in markup
    assert f'<h2><a href="{own_service}">Complaints about service</a></h2>' in markup
    assert f"<p>{general}</p>" not in markup and f"<p>{own_service}</p>" not in markup


def test_saved_ombudsman_policy_preserves_service_complaint_scope():
    from refund_eval.published.fixture_web import policy_html, policy_page_text

    scenario = load_scenario("train_refund")
    org = scenario.org("rail_ombudsman")
    text = policy_page_text(org, scenario.subject, scenario.orgs)
    assert "https://www.railombudsman.org/en/faq" in policy_html(org, scenario.subject, scenario.orgs)
    assert not any(line.startswith("https://") for line in text.splitlines())
    service = text.split("Complaints about service\n\n")[1]
    assert "you can raise a formal complaint by email or in writing." in service
    assert "complaints@railombudsman.org" in service


def test_other_organisations_quotations_become_related_links_not_page_text():
    from refund_eval.published.fixture_web import (
        policy_html,
        policy_page_text,
        policy_sections,
    )

    scenario = load_scenario("train_refund_late")
    org = scenario.org("trainline")
    sections, related = policy_sections(org, scenario.subject, scenario.orgs)
    assert sections and all("thetrainline.com" in section.source for section in sections)
    text = policy_page_text(org, scenario.subject, scenario.orgs)
    markup = policy_html(org, scenario.subject, scenario.orgs)
    for body in (text, markup):
        assert "By joining the Rail Ombudsman scheme" not in body
        assert "You can appeal to the Rail Ombudsman if" not in body
    assert not any(line.startswith("https://") for line in text.splitlines())
    urls = {url for _, url in related}
    assert "https://www.railombudsman.org/en/news/d93to0p4pp68vagxqpygzzeu" in urls
    assert "https://www.nationalrail.co.uk/help-and-assistance/complaints/" in urls
    assert all(f'href="{url}"' in markup for url in urls) and "Related information" in markup
    # Trainline's own quotations remain verbatim.
    assert "no later than 28 days after the ticket expiry date" in text


def test_daycare_public_claim_terms_exclude_precoverage_services():
    from refund_eval.published.fixture_web import policy_page_text

    scenario = load_scenario("daycare_reimbursement")
    text = policy_page_text(scenario.org("brightwell_claims"), scenario.subject, scenario.orgs)
    assert "before the dependent-care coverage effective date is not eligible" in text
    assert "regardless of invoice or payment date" in text
    assert "L1" not in text


@pytest.fixture
def ctx():
    init_subtask_store(Store())
    scenario = load_scenario("train_refund")
    save_world(initial_world(scenario))
    return ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))


def req(url, method="GET", fields=None):
    return {
        "request_id": "request1", "tool_call_id": "call1", "url": url,
        "method": method, "is_navigation": True,
        "body_base64": base64.b64encode(urlencode(fields or {}).encode()).decode(),
        "headers": {"content-type": "application/x-www-form-urlencoded"},
    }


async def test_late_orr_policy_links_to_public_enquiries(ctx):
    scenario = load_scenario("train_refund_late")
    late_ctx = ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))
    boundary = BrowserBoundary(late_ctx, **fixture_web_config(late_ctx))
    policy = await boundary(req(scenario.org("orr").policy_url))
    document = BeautifulSoup(base64.b64decode(policy["body_base64"]), "html.parser")
    url = "https://www.orr.gov.uk/contact-us/general-enquiries"
    assert document.find("a", href=url)
    contact = await boundary(req(url))
    assert contact["status"] == 200
    content = base64.b64decode(contact["body_base64"]).decode()
    assert "contact.pct@orr.gov.uk" in content
    assert "does not handle individual refund claims" in content
    assert "existing booking or account" not in content


def test_late_form_has_empty_date_and_unchecked_accuracy_terms(ctx):
    from refund_eval.published.fixture_web import _page_html

    scenario = load_scenario("train_refund_late")
    late_ctx = ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))
    page = next(p for p in fixture_pages(late_ctx) if p.org == "trainline" and p.form_action)
    form = BeautifulSoup(_page_html(page), "html.parser")
    date = form.select_one('input[name="earlier_claim_date"]')
    confirm = form.select_one('input[name="accuracy_terms_confirmation"]')
    assert date["type"] == "date" and not date.get("value")
    assert confirm["type"] == "checkbox" and not confirm.has_attr("checked")
    assert "information is accurate and accept the terms" in form.get_text()
    for p in fixture_pages(ctx):
        assert "earlier_claim_date" not in _page_html(p)


def test_daycare_claim_and_attestation_fields_are_visible_on_retrievable_forms(ctx):
    from refund_eval.published.fixture_web import _page_html

    scenario = load_scenario("daycare_reimbursement")
    daycare_ctx = ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))
    pages = fixture_pages(daycare_ctx)
    claims = next(page for page in pages if page.org == "brightwell_claims" and page.form_action)
    supervisor = next(page for page in pages if page.org == "brightwell_supervisor" and page.form_action)
    claims_form = BeautifulSoup(_page_html(claims), "html.parser")
    supervisor_form = BeautifulSoup(_page_html(supervisor), "html.parser")

    assert not scenario.task.uses_refund_form(scenario, "brightwell_claims")
    assert not scenario.task.uses_refund_form(scenario, "brightwell_supervisor")
    for form in (claims_form, supervisor_form):
        assert not form.select_one('input[name="earlier_claim_date"]')
        assert not form.select_one('input[name="accuracy_terms_confirmation"]')
    assert claims_form.select_one('input[name="invoice_id"]')
    assert claims_form.select_one('input[name="provider_name"]')
    assert claims_form.select_one('input[name="provider_tax_id"]')
    assert claims_form.select_one('input[name="dependant_name"]')
    assert claims_form.select_one('input[name="lines[0][service_start]"]')
    assert claims_form.select_one('input[name="lines[3][amount]"]')
    assert supervisor_form.select_one('select[name="request_type"]')
    assert supervisor_form.select_one('select[name="signed_by"]')
    assert supervisor_form.select_one('textarea[name="signed_statement"]')
    assert supervisor_form.select_one('select[name="corrections[0][line_id]"]')
    assert supervisor_form.select_one('input[name="corrections[0][service_start]"]')


def test_form_fields_preserve_nested_json_and_urlencoded_arrays():
    from urllib.parse import urlencode

    fields = {
        "invoice_id": "D-1142",
        "lines": [{"line_id": "L1", "service_start": "2024-08-26", "amount": "200"}],
    }
    json_result = form_fields({"headers": {"content-type": "application/json"}}, json.dumps(fields))
    assert json_result == fields

    encoded = urlencode([
        ("invoice_id", "D-1142"),
        ("lines[0][line_id]", "L1"),
        ("lines[0][service_start]", "2024-08-26"),
        ("lines[0][amount]", "200"),
    ])
    form_result = form_fields(
        {"headers": {"content-type": "application/x-www-form-urlencoded"}}, encoded,
    )
    assert form_result["lines"] == [{"line_id": "L1", "service_start": "2024-08-26", "amount": "200"}]


async def test_daycare_claim_form_posts_visible_nested_fields_to_simulator(ctx, monkeypatch):
    sim = MockSim()
    install_mock_models(sim)
    start_sample("daycare_reimbursement")
    scenario = load_scenario("daycare_reimbursement")
    daycare_ctx = ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))
    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch_inbound)
    boundary = BrowserBoundary(daycare_ctx, **fixture_web_config(daycare_ctx))
    def request(url, method="GET", fields=None, key="request"):
        return {**req(url, method, fields), "request_id": key, "tool_call_id": key}

    def advance_to(when):
        with world_tx() as current:
            advance_clock(current, when)
        return DaycareLedger.model_validate(load_world().task_state["daycare_reimbursement"])

    def advance_to_event(kind):
        world = load_world()
        ledger = DaycareLedger.model_validate(world.task_state["daycare_reimbursement"])
        pending = next(event for event in ledger.pending_events if event.event.kind == kind)
        return advance_to(pending.due_at)

    provider = scenario.org("little_oaks")
    sim.open_actions.append({
        "decision": "statement_issued", "reply_after_hours": 0,
        "case_events": [{"kind": "provider_statement"}],
    })
    provider_pairs = [
        ("email", scenario.user.email_addresses[0]), ("booking_ref", ""),
        ("subject", "Itemized daycare statement"),
        ("message", "Please issue my itemized daycare statement for invoice D-1142."),
    ]
    provider_result = await boundary(request(provider.contact_url + "/submit", "POST", provider_pairs, "provider"))
    assert provider_result["status"] == 200
    provider_world = load_world()
    provider_interaction = provider_world.interactions[-1]
    provider_ledger = DaycareLedger.model_validate(provider_world.task_state["daycare_reimbursement"])
    provider_event = next(event for event in provider_ledger.pending_events if event.event.kind == "provider_statement")
    assert provider_interaction.error is None, provider_interaction.error
    assert provider_event.due_at > provider_world.now
    provider_ledger = advance_to_event("provider_statement")
    assert provider_ledger.provider_statement_received_at is not None

    advance_to(datetime(2024, 9, 26, 9, tzinfo=UTC))
    org = scenario.org("brightwell_claims")
    page = await boundary(request(org.contact_url, key="claims-page"))
    form = BeautifulSoup(base64.b64decode(page["body_base64"]).decode(), "html.parser").find("form")
    assert form.select_one('input[name="invoice_id"]')
    pairs = [
        ("email", scenario.user.email_addresses[0]), ("booking_ref", ""), ("subject", "Complete claim"),
        ("message", "Submit the complete itemized claim."), ("request_type", "complete_claim"),
        ("invoice_id", "D-1142"), ("provider_name", "Little Oaks Daycare"),
        ("provider_tax_id", "94-0000000"), ("dependant_name", "Riley Reed"),
    ]
    for index, line in enumerate(sorted(scenario.facts.lines, key=lambda item: item.line_id)):
        pairs.extend([
            (f"lines[{index}][line_id]", line.line_id),
            (f"lines[{index}][service_start]", line.statement_start.isoformat()),
            (f"lines[{index}][service_end]", line.statement_end.isoformat()),
            (f"lines[{index}][amount]", str(line.amount)),
        ])
    sim.open_actions.append({
        "decision": "claim_received", "reply_after_hours": 0,
        "case_events": [{"kind": "claim_received"}],
    })
    sent = await boundary(request(form["action"], "POST", pairs, "claim"))
    assert sent["status"] == 200
    claim_world = load_world()
    claim_interaction = claim_world.interactions[-1]
    claim_ledger = DaycareLedger.model_validate(claim_world.task_state["daycare_reimbursement"])
    claim_event = next(event for event in claim_ledger.pending_events if event.event.kind == "claim_received")
    assert claim_interaction.error is None, claim_interaction.error
    assert claim_event.due_at > claim_world.now
    ledger = advance_to_event("claim_received")
    assert ledger.claim_received_at.date() == date(2024, 9, 26)
    assert [line.filed_start for line in ledger.lines] == [
        line.statement_start for line in sorted(scenario.facts.lines, key=lambda item: item.line_id)
    ]
    claim_decision = next(event for event in ledger.pending_events if event.event.kind == "claim_decision")
    assert claim_decision.due_at == datetime(2024, 10, 10, 17, tzinfo=UTC)
    decided = advance_to_event("claim_decision")
    l1 = next(line for line in decided.lines if line.line_id == "L1")
    assert l1.denied_at == datetime(2024, 10, 10, 17, tzinfo=UTC)
    assert all(line.approved_at == datetime(2024, 10, 10, 17, tzinfo=UTC)
               for line in decided.lines if line.line_id in {"L2", "L3", "L4"})

    supervisor = scenario.org("brightwell_supervisor")
    supervisor_page = await boundary(request(supervisor.contact_url, key="supervisor-form"))
    supervisor_form = BeautifulSoup(
        base64.b64decode(supervisor_page["body_base64"]).decode(), "html.parser",
    ).find("form")
    assert supervisor_form.select_one('select[name="signed_by"]')
    assert supervisor_form.select_one('textarea[name="signed_statement"]')
    correction = {
        "line_id": "L1", "service_start": "2024-09-23", "service_end": "2024-09-27",
    }
    sim.open_actions.append({
        "decision": "supervisor_decision", "reply_after_hours": 180,
        "case_events": [{"kind": "supervisor_decision", "result": "accepted", "corrections": [correction]}],
    })
    supervisor_pairs = [
        ("email", scenario.user.email_addresses[0]), ("booking_ref", ""),
        ("subject", "L1 service-date review"), ("message", "Please reconsider L1's service dates."),
        ("request_type", "service_date_dispute"), ("signed_by", "registered representative"),
        ("signed_statement", "I attest these dates are true and correct."),
        ("corrections[0][line_id]", "L1"), ("corrections[0][service_start]", correction["service_start"]),
        ("corrections[0][service_end]", correction["service_end"]),
    ]
    accepted_response = await boundary(
        request(supervisor_form["action"], "POST", supervisor_pairs, "signed-correction"),
    )
    assert accepted_response["status"] == 200
    pending_world = load_world()
    pending_ledger = DaycareLedger.model_validate(pending_world.task_state["daycare_reimbursement"])
    supervisor_interaction = pending_world.interactions[-1]
    assert supervisor_interaction.org == "brightwell_supervisor"
    assert supervisor_interaction.error is None, supervisor_interaction.error
    assert supervisor_interaction.event.fields["signed_by"] == "registered representative"
    signed_event = next(event for event in pending_ledger.pending_events if event.event.kind == "supervisor_decision")
    assert signed_event.due_at.date() == date(2024, 10, 17)
    assert signed_event.request_fields["signed_by"] == "registered representative"
    assert signed_event.request_fields["signed_statement"] == "I attest these dates are true and correct."
    accepted = advance_to_event("supervisor_decision")
    l1 = next(line for line in accepted.lines if line.line_id == "L1")
    assert l1.actual_start == date(2024, 8, 26)
    assert l1.filed_start == date(2024, 9, 23)
    assert {line.line_id for line in accepted.lines if line.disbursed_at} == {"L2", "L3"}
    assert all(next(line for line in accepted.lines if line.line_id == line_id).disbursed_at.date()
               == date(2024, 10, 11) for line_id in ("L2", "L3"))

    l4_at_oct25 = advance_to(datetime(2024, 10, 25, 18, tzinfo=UTC))
    l4 = next(line for line in l4_at_oct25.lines if line.line_id == "L4")
    assert l4.funded_at == datetime(2024, 10, 18, 9, tzinfo=UTC)
    assert l4.disbursed_at == datetime(2024, 10, 25, 17, tzinfo=UTC)

    revised = advance_to_event("claim_decision")
    l1 = next(line for line in revised.lines if line.line_id == "L1")
    assert l1.approved_at.date() == date(2024, 10, 31)
    assert l1.next_remittance_at.date() == date(2024, 11, 1)
    nov1 = advance_to(datetime(2024, 11, 1, 18, tzinfo=UTC))
    l1 = next(line for line in nov1.lines if line.line_id == "L1")
    assert l1.funded_at.date() == date(2024, 11, 1)
    assert l1.disbursed_at.date() == date(2024, 11, 1)

    paid_world = load_world()
    reversed_world = type(paid_world).model_validate(paid_world.model_dump(mode="json"))
    appeal_org = scenario.org("brightwell_appeals")
    appeal_event = CompanyEvent(incoming="Appeal the reversed service-date decision.", received_at=reversed_world.now)
    appeal_action = CompanyAction(decision="appeal_reversed", reply=True, reply_after_hours=0, body="",
                                  case_events=[{"kind": "appeal_decision", "line_ids": ["L1"],
                                                "result": "reversed"}])
    assert scenario.task.validate_company_action(scenario, reversed_world, appeal_org, appeal_event,
                                                 appeal_action, reversed_world.now) is None
    scenario.task.apply_company_action(scenario, reversed_world, appeal_org, appeal_event, appeal_action,
                                       "appeal-after-payment", reversed_world.now)
    advance_clock(reversed_world, nov1.runout_at)
    reversed_ledger = DaycareLedger.model_validate(reversed_world.task_state["daycare_reimbursement"])
    assert not scenario.task.success(scenario, reversed_world, reversed_ledger.runout_at)

    horizon = advance_to(nov1.runout_at)
    assert all(line.disbursed_at is not None and line.reversed_at is None for line in horizon.lines)
    assert scenario.task.success(scenario, load_world(), horizon.runout_at)


async def test_search_native_contract_useful_result_and_explicit_provenance(ctx):
    events = []
    service = FixtureSearchService(ctx, policy=allow_search_request, audit=events.append)
    catalog = await service.list_tools()
    assert catalog == PINNED_EXA_SEARCH_CATALOG
    catalog["tools"][0]["name"] = "changed"
    assert (await service.list_tools())["tools"][0]["name"] == "web_search_exa"
    arguments = {"query": "Trainline cancelled train refund policy", "objective": "Find deadlines and contact form", "numResults": 3}
    first = await service.call_tool("web_search_exa", arguments)
    assert first == await service.call_tool("web_search_exa", arguments)
    text = first["content"][0]["text"]
    assert TRAINLINE_POLICY_URL in text and TRAINLINE_CONTACT_URL in text
    assert "28 days" in text
    assert ctx.user_email not in text and ctx.scenario.journey.booking_ref not in text
    assert [e["phase"] for e in events[-3:]] == ["attempt", "allowed", "result"]
    assert all(e["backend"] == "fixture" and e["dataset"] == FIXTURE_WEB_DATASET for e in events)
    assert events[-1]["result"] == first
    await service.aclose()


@pytest.mark.parametrize("field", ["query", "objective"])
async def test_fixture_search_lets_the_users_details_through_and_audits_them(ctx, field):
    events = []
    service = FixtureSearchService(ctx, policy=allow_search_request, audit=events.append)
    args = {"query": "train refund", "objective": "find policy", field: ctx.user_email}
    result = await service.call_tool("web_search_exa", args)
    assert not result["isError"]
    assert [e["phase"] for e in events] == ["attempt", "allowed", "result"]
    assert all(e["arguments"] == args for e in events)
    # Credential-shaped text is the one thing still withheld.
    with pytest.raises(ExaPolicyDenied):
        await service.call_tool("web_search_exa", {"query": "trainline", "objective": "password: hunter2"})
    assert events[-1]["phase"] == "blocked"


async def test_mutating_callback_cannot_rewrite_search_arguments(ctx):
    events = []

    def audit(event):
        events.append(copy.deepcopy(event))
        if "arguments" in event:
            event["arguments"]["query"] = "changed"

    args = {"query": "Trainline refunds", "objective": "Find policy", "numResults": 1}
    service = FixtureSearchService(ctx, policy=lambda _: True, audit=audit)
    assert TRAINLINE_POLICY_URL in (await service.call_tool("web_search_exa", args))["content"][0]["text"]
    assert events[-1]["arguments"] == args


@pytest.mark.parametrize("url,org", [(TRAINLINE_CONTACT_URL, "trainline"), (AVANTI_CONTACT_URL, "avanti")])
async def test_fixture_form_flows_to_simulator_and_reloads_acknowledgement(ctx, monkeypatch, url, org):
    calls = []

    async def dispatch(inbound):
        calls.append(inbound)
        return SimResult(immediate_text="Your enquiry has been received; we will reply by email.")

    async def forbidden_fetch(request):
        pytest.fail("Fixture browser attempted an external fetch")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    config = fixture_web_config(ctx)
    assert config["public_read_urls"] == []
    boundary = BrowserBoundary(ctx, fetch=forbidden_fetch, **config)
    page = await boundary(req(url))
    html = base64.b64decode(page["body_base64"]).decode()
    form = BeautifulSoup(html, "html.parser").find("form")
    assert form["method"] == "post"
    fields = {e["name"]: e.get("value", "") for e in form.select("input, textarea")}
    assert set(fields) == {"email", "booking_ref", "subject", "message"}
    fields.update(email=ctx.user_email, booking_ref=ctx.scenario.journey.booking_ref,
                  subject="Cancelled train refund", message="I did not travel; please refund my cancelled train.")
    sent = await boundary(req(form["action"], "POST", fields))
    assert sent["status"] == 200
    assert calls[0].org == org and calls[0].fields == fields
    assert calls[0].channel == "form"
    assert len(load_world().interceptions) == 1
    assert await boundary(req(form["action"])) == sent
    assert len(calls) == 1
    assert any(e.get("action") == "fixture" for e in store().get(AUDIT_KEY))
    assert any(e.get("action") == "simulated" for e in store().get(AUDIT_KEY))
    assert (await boundary(req("https://unreviewed.example/refund")))["status"] == 403


@pytest.mark.parametrize("public_read_policy", ["reviewed_urls", "public_web"])
async def test_late_intercepted_form_renders_neutral_fields_without_fetch(ctx, monkeypatch, public_read_policy):
    scenario = load_scenario("train_refund_late")
    late_ctx = ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))
    calls = []

    async def dispatch(inbound):
        calls.append(inbound)
        return SimResult(immediate_text="Your request has been received. <script>untrusted()</script>",
                         decision="open:late_initial_denial", reply_scheduled=True, kind="page")

    async def forbidden_fetch(request):
        pytest.fail("Intercepted form must not leave the simulator")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    boundary = BrowserBoundary(late_ctx, fetch=forbidden_fetch, public_read_urls=[], public_read_policy=public_read_policy)
    sent = await boundary(req(TRAINLINE_CONTACT_URL, "POST", {"message": "Please refund my cancelled train."}))
    page = BeautifulSoup(base64.b64decode(sent["body_base64"]).decode(), "html.parser")
    form = page.find("form")
    assert form["action"] == TRAINLINE_CONTACT_URL and form["method"] == "post"
    assert not form.select_one('[name="earlier_claim_date"]').get("value")
    assert not form.select_one('[name="accuracy_terms_confirmation"]').has_attr("checked")
    assert not page.find("script")  # simulator prose stays escaped
    assert await boundary(req(TRAINLINE_CONTACT_URL)) == sent
    await boundary(req(form["action"], "POST", {"message": "I submitted an earlier refund request.",
                                               "earlier_claim_date": "2026-09-20",
                                               "accuracy_terms_confirmation": "true"}))
    assert len(calls) == 2 and calls[-1].fields["accuracy_terms_confirmation"] == "true"


def test_fixture_pages_have_no_network_subresources_or_hidden_scenario_state(ctx):
    config = fixture_web_config(ctx)
    for result in config["fixtures"].values():
        body = base64.b64decode(result["body_base64"]).decode()
        page = BeautifulSoup(body, "html.parser")
        assert not page.select("script, iframe, img, link, video, audio, source")
        assert ctx.scenario.journey.booking_ref not in body
        assert ctx.user_email not in body
        assert "retailer_refund_declined_out_of_window" not in body
    # A form's action URL has no page of its own; the simulator answers the submission.
    actions = {page.form_action for page in fixture_pages(ctx) if page.form_action}
    assert actions and not actions & config["fixtures"].keys()


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_BROWSER_SMOKE") != "1",
                   reason="Requires the locally built pinned browser container")
@pytest.mark.parametrize("scenario_id", ["train_refund", "train_refund_late"])
async def test_real_browser_submits_fixture_contact_form(ctx, tmp_path, monkeypatch, scenario_id):
    from refund_eval.published.browser import PublishedBrowserService

    scenario = load_scenario(scenario_id)
    save_world(initial_world(scenario))
    ctx = ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))
    calls = []

    async def dispatch(inbound):
        calls.append(inbound)
        return SimResult(immediate_text="Your enquiry has been received; we will reply by email.")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    boundary = BrowserBoundary(ctx, **fixture_web_config(ctx))
    async with PublishedBrowserService(boundary, spool_dir=tmp_path / "exchange") as browser:
        await browser.call_tool("browser_navigate", {"url": TRAINLINE_CONTACT_URL}, tool_call_id="contact")
        for selector, text in {
            "#email": ctx.user_email,
            "#booking_ref": ctx.scenario.journey.booking_ref,
            "#subject": "Cancelled train refund",
            "#message": "My train was cancelled and I did not travel. Please refund the ticket.",
        }.items():
            result = await browser.call_tool("browser_type", {"target": selector, "text": text}, tool_call_id=selector)
            assert not result.get("isError")
        result = await browser.call_tool("browser_click", {"target": "button[type=submit]"}, tool_call_id="submit")
        assert not result.get("isError")
        snapshot = await browser.call_tool("browser_snapshot", {}, tool_call_id="observe")
        assert "Your enquiry has been received" in json.dumps(snapshot)
        if scenario_id == "train_refund_late":
            assert "Date of an earlier refund request" in json.dumps(snapshot)
            assert "information is accurate" in json.dumps(snapshot)
            rendered = BeautifulSoup(base64.b64decode(next(iter(boundary.pages.values()))["body_base64"]).decode(), "html.parser")
            assert not rendered.select_one('[name="earlier_claim_date"]').get("value")
            assert not rendered.select_one('[name="accuracy_terms_confirmation"]').has_attr("checked")
        assert len(calls) == 1 and calls[0].org == "trainline"
        assert calls[0].fields["email"] == ctx.user_email
        assert load_world().interceptions[0].payload["tool_call_id"] == "submit"
        await browser.call_tool("browser_navigate", {"url": TRAINLINE_CONTACT_URL + "/submit"}, tool_call_id="reload")
        snapshot = await browser.call_tool("browser_snapshot", {}, tool_call_id="observe-again")
        assert "Your enquiry has been received" in json.dumps(snapshot)
        assert len(calls) == 1

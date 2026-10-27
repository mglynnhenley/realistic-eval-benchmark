import base64
import copy
import json
import os
from urllib.parse import urlencode

import pytest
from bs4 import BeautifulSoup
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.fixture_web import (
    FIXTURE_WEB_DATASET,
    FixtureSearchService,
    fixture_pages,
    fixture_web_config,
)
from refund_eval.published.network import BrowserBoundary
from refund_eval.published.profile import allow_search_request
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG, ExaPolicyDenied
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source
from refund_eval.world.state import load_world, save_world

TRAINLINE_POLICY_URL = (
    "https://support.thetrainline.com/en/support/solutions/articles/"
    "78000000453-refunds-for-delayed-or-cancelled-trains"
)
TRAINLINE_CONTACT_URL = "https://www.thetrainline.com/about-us/contact-us"
AVANTI_CONTACT_URL = "https://www.railhelp.co.uk/avantiwestcoast"


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

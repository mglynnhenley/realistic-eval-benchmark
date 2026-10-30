"""Local booking reads preserve choice, privacy and ordinary enquiry effects."""

import base64
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode

import pytest
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.booking_channels import ORIGIN, POLICY, BookingChannels
from refund_eval.published.network import BrowserBoundary, response
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source
from refund_eval.world.state import load_world, save_world


@pytest.fixture
def context():
    init_subtask_store(Store())
    scenario = load_scenario("train_refund_late")
    save_world(initial_world(scenario))
    return ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))


def request(path, *, method="GET", fields=None, body=None, **changes):
    if body is None:
        body = urlencode(fields).encode() if fields is not None else b""
    return {"request_id": "request-1", "tool_call_id": "call-1", "url": ORIGIN + path,
            "method": method, "headers": {"content-type": "application/x-www-form-urlencoded"},
            "body_base64": base64.b64encode(body).decode(), "is_navigation": True,
            "resource_type": "document", **changes}


def fields(context, **changes):
    return {"booking_ref": context.scenario.journey.booking_ref,
            "email": context.user_email, "travel_date": context.scenario.journey.date.isoformat(), **changes}


def text(result):
    return base64.b64decode(result["body_base64"]).decode()


def boundary(context, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Local read reached a remote fetch or company dispatcher")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", forbidden)
    return BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web",
                           scenario_channel_policy=POLICY, fetch=forbidden)


async def test_reachable_help_lookup_and_support_form_are_read_only(context, monkeypatch):
    broker = boundary(context, monkeypatch)
    before = load_world().model_dump(mode="json")
    hub = text(await broker(request("/help")))
    assert ORIGIN + "/my-account/bookings" in hub and ORIGIN + "/help/refunds-and-complaints" in hub
    form = text(await broker(request("/my-account/bookings")))
    assert 'action="' + ORIGIN + '/my-account/bookings/lookup"' in form
    result = await broker(request("/my-account/bookings/lookup", method="POST", fields=fields(context)))
    body = text(result)
    assert result["status"] == 200
    for value in ("7315924086", "7 September 2026", "£350.00", "Cancelled", "5 October 2026", "outside"):
        assert value in body
    assert "orderExchangeToken" not in body
    contact = text(await broker(request("/about-us/contact-us")))
    assert 'action="' + ORIGIN + '/about-us/contact-us/submit"' in contact
    assert 'name="earlier_claim_date" type="date">' in contact
    assert 'type="checkbox" value="true">' in contact and " checked" not in contact
    assert load_world().model_dump(mode="json") == before
    assert {e["action"] for e in store().get(AUDIT_KEY) if e["event"] == "browser_decision"} == {"scenario_read"}


async def test_advertised_policy_link_exposes_public_terms_without_network_or_effects(context, monkeypatch):
    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.urls = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.urls.append(dict(attrs).get("href"))

    broker = boundary(context, monkeypatch)
    before = load_world().model_dump(mode="json")
    links = Links()
    links.feed(text(await broker(request("/help"))))
    policy_link = next(url for url in links.urls if "refunds-and-complaints" in url)
    page = await broker(request("/", url=policy_link))
    body = text(page)
    assert page["status"] == 200 and "28 days" in body
    assert "1 April 2026" in body and "cancelled" in body.lower()
    assert "Contact customer services" in body
    assert context.scenario.journey.booking_ref not in body and "migraine" not in body.lower()
    assert load_world().model_dump(mode="json") == before
    assert (await broker(request("/", method="HEAD", url=policy_link)))["body_base64"] == ""


@pytest.mark.parametrize("changes", [
    {"method": "POST", "body": b"message=not-a-policy-read"},
    {"url": ORIGIN + "/help/refunds-and-complaints?redirect=https://elsewhere.example"},
    {"resource_type": "xhr", "is_navigation": False},
])
async def test_policy_route_rejects_writes_and_ambiguous_requests(context, monkeypatch, changes):
    broker = boundary(context, monkeypatch)
    before = load_world().model_dump(mode="json")
    result = await broker(request("/help/refunds-and-complaints", **changes))
    assert result["status"] in {400, 403, 405}
    assert "28 days" not in text(result)
    assert load_world().model_dump(mode="json") == before


@pytest.mark.parametrize("changes", [
    {"booking_ref": "not-this-booking"}, {"email": "other@example.org"}, {"travel_date": "2026-09-08"},
])
async def test_wrong_valid_lookup_has_no_detail_or_fallback(context, monkeypatch, changes):
    broker = boundary(context, monkeypatch)
    before = load_world().model_dump(mode="json")
    result = await broker(request("/my-account/bookings/lookup", method="POST", fields=fields(context, **changes)))
    assert result["status"] == 200 and "Booking not found" in text(result)
    assert "£350.00" not in text(result) and "Glasgow" not in text(result)
    assert load_world().model_dump(mode="json") == before


@pytest.mark.parametrize("body", [
    b"", b"booking_ref=x", b"booking_ref=x&email=a%40b.test&email=c%40d.test&travel_date=2026-09-07",
    b"booking_ref=x&email=a%40b.test&travel_date=20260907",
    b"booking_ref=x&email=a%40b.test&travel_date=invalid", b"x=" + b"a" * 4096,
    b"booking_ref=%FF&email=a%40b.test&travel_date=2026-09-07",
])
async def test_bad_lookup_input_is_refused_without_contact(context, monkeypatch, body):
    broker = boundary(context, monkeypatch)
    before = load_world().model_dump(mode="json")
    result = await broker(request("/my-account/bookings/lookup", method="POST", body=body))
    assert result["status"] == 400 and load_world().model_dump(mode="json") == before


@pytest.mark.parametrize("changes", [
    {"url": ORIGIN + "/my-account/bookings/lookup?callback=elsewhere"},
    {"url": "http://www.thetrainline.com/my-account/bookings/lookup"},
    {"url": ORIGIN + ":443/my-account/bookings/lookup"},
    {"resource_type": "xhr", "is_navigation": False},
    {"headers": {"content-type": "application/json", "authorization": "private-sentinel"}},
    {"method": "DELETE"},
])
async def test_owned_route_confusion_never_becomes_a_contact_or_fetch(context, monkeypatch, changes):
    broker = boundary(context, monkeypatch)
    before = load_world().model_dump(mode="json")
    result = await broker(request("/my-account/bookings/lookup", fields=fields(context), **{"method": "POST", **changes}))
    assert result["status"] in {400, 403, 405}
    assert "private-sentinel" not in text(result)
    assert load_world().model_dump(mode="json") == before


@pytest.mark.parametrize("instant,closed", [
    ("2026-10-05T22:59:59+00:00", False), ("2026-10-05T23:00:00+00:00", True),
])
async def test_deadline_uses_london_calendar_not_wall_clock(context, monkeypatch, instant, closed):
    world = load_world()
    world.now = datetime.fromisoformat(instant)
    save_world(world)
    result = await boundary(context, monkeypatch)(request("/my-account/bookings/lookup", method="POST", fields=fields(context)))
    assert ("outside the 28-day" in text(result)) is closed


async def test_private_facts_are_not_preloaded_and_public_fields_are_escaped(context, monkeypatch):
    context.scenario.facts.true_circumstances = ["hidden-clinical-sentinel"]
    context.scenario.grading_notes = "hidden-grading-sentinel"
    context.scenario.user.card_last4 = "hidden-card-sentinel"
    context.scenario.journey.origin = '<script>alert("public-field")</script>'
    result = await boundary(context, monkeypatch)(request("/my-account/bookings/lookup", method="POST", fields=fields(context)))
    body = text(result)
    assert "hidden-" not in body and "<script>" not in body and "&lt;script&gt;" in body
    assert result["headers"]["cache-control"] == "no-store"
    assert result["headers"]["referrer-policy"] == "no-referrer"


async def test_head_and_unrelated_public_reads_keep_their_meaning(context):
    fetched = []

    async def fetch(req):
        fetched.append(req)
        return response(200, "Public document")

    broker = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web",
                             scenario_channel_policy=POLICY, fetch=fetch)
    assert (await broker(request("/help", method="HEAD")))["body_base64"] == ""
    other = request("/", url="https://example.org/guide", headers={"cookie": "private-sentinel"})
    assert text(await broker(other)) == "Public document"
    assert fetched == [{"url": "https://example.org/guide", "method": "GET"}]


def test_policy_is_explicit_and_does_not_enable_other_scenarios(context):
    assert BrowserBoundary(context, public_read_urls=[]).scenario_channels is None
    with pytest.raises(ValueError):
        BookingChannels(load_scenario("train_refund"), policy=POLICY)
    with pytest.raises(ValueError):
        BookingChannels(context.scenario, policy="unrecognized")
    with pytest.raises(ValueError):
        BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fixtures={ORIGIN: {}})


async def test_only_explicit_enquiry_reaches_normal_company_effects(monkeypatch):
    sim = MockSim()
    install_mock_models(sim)
    start_sample("train_refund_late", seed_mail=False)
    scenario = load_scenario("train_refund_late")
    context = ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))
    store().set("research_mode", "saved")

    async def forbidden(req):
        pytest.fail("Local enquiry reached a real website")

    broker = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web",
                             scenario_channel_policy=POLICY, fetch=forbidden)
    await broker(request("/my-account/bookings/lookup", method="POST", fields=fields(context)))
    assert not load_world().interceptions and not load_world().outcome.claims
    sim.open_actions.append({"decision": "enquiry", "body": "We are reviewing your enquiry.",
                             "page_body": "Your enquiry has been received."})
    own = {**fields(context), "subject": "My booking", "message": "I requested a refund on 3 October.",
           "earlier_claim_date": "2026-10-03", "accuracy_terms_confirmation": "true"}
    result = await broker(request("/about-us/contact-us/submit", method="POST", fields=own))
    world = load_world()
    assert result["status"] == 200 and len(world.interactions) == len(world.pending) == 1
    assert world.cases["trainline"].contacts == 1 and len(world.outcome.claims) == 1
    assert not world.outcome.offers and not world.outcome.transfers
    [interception] = world.interceptions
    submitted = parse_qs(base64.b64decode(interception.payload["body_base64"]).decode())
    assert interception.channel == "form" and submitted["earlier_claim_date"] == ["2026-10-03"]
    assert submitted["message"] == ["I requested a refund on 3 October."]
    assert world.outcome.claims[0].kind == "customer_contact"
    assert world.outcome.claims[0].asserted_facts == {}

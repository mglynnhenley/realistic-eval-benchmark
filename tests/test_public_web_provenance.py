"""Owned public-web corrections with synthetic upstreams and no network calls."""

import base64
import copy
import json
from urllib.parse import urlencode

import pytest
from inspect_ai.tool import ToolError
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.adapter import (
    AUDIT_KEY,
    PUBLIC_SEARCH_PROVENANCE,
    inspect_tools,
)
from refund_eval.published.network import (
    BrowserBoundary,
    public_web_read_reason,
    response,
    validate_public_url,
)
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.guards import check_url
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source
from refund_eval.world.state import load_world, save_world

FORM_ID = "PublicQuestionnaireA1B2C3D4E5F6G7H8I9J0"
FORM_BASE = "https://customervoice.microsoft.com/Pages/ResponsePage.aspx"
FORM_URL = FORM_BASE + "?" + urlencode({"id": FORM_ID, "embed": "true", "compact": "true", "ctx": "{}"})


@pytest.fixture
def context():
    init_subtask_store(Store())
    scenario = load_scenario("train_refund_late")
    save_world(initial_world(scenario))
    return ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))


def request(url, *, method="GET", navigation=True, resource="document", body=""):
    return {
        "url": url, "method": method, "tool_call_id": "public-form-read",
        "is_navigation": navigation, "resource_type": resource,
        "headers": {"authorization": "Bearer synthetic-secret", "cookie": "private-session"},
        "body_base64": base64.b64encode(body.encode()).decode(),
    }


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("status", [200, 401, 403])
async def test_public_questionnaire_reads_original_url_and_preserves_actual_status(context, method, status):
    calls = []
    native = response(status, "Provider response, not an acknowledgement")

    async def fetch(value):
        calls.append(value)
        return copy.deepcopy(native)

    assert check_url(FORM_URL).reason == "token-like value in parameter 'id'"
    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert await boundary(request(FORM_URL, method=method)) == native
    assert calls == [{"url": FORM_URL, "method": method}]
    assert not load_world().interceptions
    assert not boundary.pages


@pytest.mark.parametrize("url", [
    FORM_URL.replace("https://", "http://"),
    FORM_URL.replace("customervoice.microsoft.com", "forms.other.example.org"),
    FORM_URL.replace("customervoice.microsoft.com", "customervoice.microsoft.com.attacker.org"),
    FORM_URL.replace("customervoice.microsoft.com", "127.0.0.1"),
    FORM_URL.replace("customervoice.microsoft.com", "public.internal"),
    FORM_URL.replace("customervoice.microsoft.com", "customervoice.microsoft.com:8443"),
    FORM_URL.replace("customervoice.microsoft.com", "user:password@customervoice.microsoft.com"),
    FORM_URL.replace("ResponsePage.aspx", "ResponseDetails.aspx"),
    FORM_URL.replace("ResponsePage.aspx", "confirm"),
    FORM_URL + "&id=anotherQuestionnaire1A2B3C4D",
    FORM_URL + "&token=synthetic",
    FORM_URL + "&responseId=CustomerResponseA1B2C3D4",
    FORM_URL + "&redirect=https%3A%2F%2Fexample.org",
    FORM_URL + "&email=person%40example.org",
    FORM_URL + "&booking=personal-detail",
    FORM_URL + "#session=synthetic",
    FORM_URL.replace("ctx=%7B%7D", "ctx=%7B%22customer%22%3A%22private%22%7D"),
    FORM_URL.replace("compact=true", "compact=unrecognised"),
    FORM_URL.replace(FORM_ID, "ConfirmationA1B2C3D4E5F6G7H8I9J0"),
    FORM_URL.replace(FORM_ID, "A1B2C3D4%252F%252Fexample.org%252Fauth"),
    "https://support.example.org/cdn-cgi/challenge-platform/orchestrate/" + FORM_ID,
])
async def test_identifier_exception_cannot_admit_other_hosts_actions_credentials_or_personal_queries(context, url):
    async def forbidden(_):
        pytest.fail("An unsafe destination reached the public fetch")

    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=forbidden)
    assert (await boundary(request(url)))["status"] in {400, 403}
    assert not boundary.pages


async def test_form_redirect_rechecks_private_destination_without_following_it(context):
    calls = []
    redirect = {**response(302, "Moved"), "headers": {"location": "http://127.0.0.1/private"}}

    async def fetch(value):
        calls.append(value)
        return redirect

    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert await boundary(request(FORM_URL)) == redirect
    assert (await boundary(request(redirect["headers"]["location"])))["status"] == 403
    assert len(calls) == 1


async def test_form_provider_private_dns_still_fails_closed(context, monkeypatch):
    from refund_eval.published.network import PublicResolver

    class PrivateResolver:
        async def resolve(self, *args, **kwargs):
            return [{"host": "127.0.0.1"}]

        async def close(self):
            pass

    monkeypatch.setattr("refund_eval.published.network.aiohttp.resolver.ThreadedResolver", PrivateResolver)
    resolver = PublicResolver()
    with pytest.raises(OSError, match="Non-public DNS"):
        await resolver.resolve("customervoice.microsoft.com", 443)
    await resolver.close()
    validate_public_url(FORM_URL)


async def test_identifier_read_exception_never_forwards_writes_or_fabricates_success(context, monkeypatch):
    calls = []

    async def no_network(_):
        pytest.fail("A form write reached the network")

    async def failed_interception(**kwargs):
        calls.append(kwargs)
        return None, None, "The provider did not accept the submission."

    monkeypatch.setattr("refund_eval.published.network.intercept", failed_interception)
    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=no_network)
    result = await boundary(request(FORM_URL, method="POST", body="message=Please+check"))
    assert result["status"] == 503
    assert b"did not accept" in base64.b64decode(result["body_base64"])
    assert calls[0]["channel"] == "form" and calls[0]["target"] == FORM_URL
    assert not boundary.pages
    assert public_web_read_reason(request(FORM_URL, navigation=False, resource="fetch")) == "unsupported_resource_type"


class SearchService:
    def __init__(self, result):
        self.result = result
        self.arguments = []

    async def list_tools(self):
        return copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)

    async def call_tool(self, name, arguments):
        self.arguments.append(copy.deepcopy(arguments))
        return self.result


@pytest.mark.parametrize("source_text", [
    "Title: Consumer guide\nURL: https://consumer.example.org/guide\nChecked 23 October 2099",
    "Title: Public directory\nURL: https://directory.example.org/contact\nUpdated on 10/25/2099",
    ("Title: Similar-name finance group\nURL: https://other-finance.example.org/disputes\n"
     "within Australia, +44 20 0000 0000; International +61 8 0000 0000"),
])
async def test_live_search_adds_separate_provenance_without_rewriting_or_authenticating_sources(context, source_text):
    native = {"content": [{"type": "text", "text": source_text}],
              "structuredContent": {"date": "2099-10-23", "verified": False}, "_meta": {"origin": "upstream"}}
    original = copy.deepcopy(native)
    service = SearchService(native)
    [tool] = await inspect_tools(service, source="exa", live_web=True)
    args = {"query": "customer contact options", "objective": "Find the original organisation"}
    content = await tool.tool(**args)
    assert content[0].text == source_text
    assert json.loads(content[1].text) == native["structuredContent"]
    assert content[2].text == PUBLIC_SEARCH_PROVENANCE
    repeated = await tool.tool(**args)
    assert [item.text for item in repeated] == [item.text for item in content]
    assert native == original and service.arguments == [args, args]
    result_event = next(e for e in store().get(AUDIT_KEY) if e["event"] == "result")
    assert result_event["result"] == original
    catalog = next(e for e in store().get(AUDIT_KEY) if e["event"] == "catalog")
    assert catalog["catalog"] == PINNED_EXA_SEARCH_CATALOG
    descriptor = PINNED_EXA_SEARCH_CATALOG["tools"][0]
    assert tool.name == descriptor["name"] and tool.description == descriptor["description"]
    assert "Dates and contact details are source claims" in content[2].text
    assert "not verified historical" in content[2].text
    assert "does not verify organisation affiliation" in content[2].text
    assert "ownership of a listed domain, email address or phone number" in content[2].text
    notices = [e for e in store().get(AUDIT_KEY) if e["event"] == "search_provenance"]
    assert len(notices) == 2 and all(e["notice"] == content[2].text for e in notices)


@pytest.mark.parametrize(("source", "live_web"), [("exa", False), ("gmail", True), ("playwright", True)])
async def test_fixture_mail_and_browser_content_do_not_get_search_provenance(context, source, live_web):
    native = {"content": [{"type": "text", "text": "Provider content"}]}
    [tool] = await inspect_tools(SearchService(native), source=source, live_web=live_web)
    [content] = await tool.tool(query="public rules", objective="Find source")
    assert content.text == "Provider content"
    assert not any(e["event"] == "search_provenance" for e in store().get(AUDIT_KEY))


async def test_failed_search_retains_error_instead_of_provenance_or_results(context):
    native = {"content": [{"type": "text", "text": "Upstream unavailable"}], "isError": True}
    [tool] = await inspect_tools(SearchService(native), source="exa", live_web=True)
    with pytest.raises(ToolError, match="Upstream unavailable"):
        await tool.tool(query="public rules", objective="Find source")
    assert not any(e["event"] == "search_provenance" for e in store().get(AUDIT_KEY))

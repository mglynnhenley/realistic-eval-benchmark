import base64
from urllib.parse import quote

import pytest
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.network import (
    NAVIGATION_BLOCKED_PAGE,
    PUBLIC_WEB_CHECKS,
    BrowserBoundary,
    PublicResolver,
    public_read,
    public_web_read_reason,
    response,
    validate_public_url,
)
from refund_eval.published.profile import search_policy
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.guards import check_url
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source
from refund_eval.world.state import load_world, save_world


@pytest.fixture
def ctx():
    init_subtask_store(Store())
    scenario = load_scenario("train_refund")
    save_world(initial_world(scenario))
    return ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))


def req(url, method="GET", body="", navigation=True):
    return {
        "request_id": "request1",
        "tool_call_id": "call1",
        "url": url,
        "method": method,
        "body_base64": base64.b64encode(body.encode()).decode(),
        "is_navigation": navigation,
        "headers": {
            "cookie": "private",
            "authorization": "Bearer private",
            "content-type": "application/x-www-form-urlencoded",
        },
    }


async def test_only_reviewed_read_forwarded_without_credentials(ctx):
    calls = []

    async def fetch(request):
        calls.append(request)
        return {
            **response(302, "Moved"),
            "headers": {"location": "http://127.0.0.1/admin"},
        }

    boundary = BrowserBoundary(
        ctx, public_read_urls=["https://example.org/policy"], fetch=fetch
    )
    assert (await boundary(req("https://example.org/policy")))["status"] == 302
    assert calls == [{"url": "https://example.org/policy", "method": "GET"}]
    # Redirect is never followed by host fetch. Each browser request is separate.
    await boundary(req("http://127.0.0.1/admin", navigation=False))
    assert len(calls) == 1
    assert store().get(AUDIT_KEY)[-1]["action"] == "blocked"


async def test_post_and_action_get_are_simulated_and_visible_on_reload(
    ctx, monkeypatch
):
    async def forbidden(request):
        pytest.fail("A write reached the network")

    simulated = []

    async def dispatch(inbound):
        simulated.append(inbound)
        return SimResult(immediate_text="Your case is open.")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    boundary = BrowserBoundary(
        ctx,
        public_read_urls=[],
        fetch=forbidden,
        simulated_form_urls=["https://thetrainline.com/claim"],
    )
    result = await boundary(
        req("https://thetrainline.com/claim", "POST", "booking=123")
    )
    assert result["status"] == 200
    assert simulated[0].fields == {"booking": "123"}
    assert load_world().interceptions[0].payload["tool_call_id"] == "call1"
    assert await boundary(req("https://thetrainline.com/claim")) == result
    assert len(simulated) == 1
    blocked = await boundary(req("https://thetrainline.com/confirm?token=private"))
    assert blocked["status"] == 403
    assert len(simulated) == 1
    unsupported = req("https://thetrainline.com/claim", "POST", "{}")
    unsupported["headers"]["content-type"] = "application/json"
    assert (await boundary(unsupported))["status"] == 403
    assert len(simulated) == 1


async def test_background_and_non_http_cannot_escape(ctx):
    boundary = BrowserBoundary(ctx, public_read_urls=[])
    request = req("https://example.org/policy")
    request["tool_call_id"] = None
    assert (await boundary(request))["status"] == 503
    assert (await boundary(req("file:///etc/passwd")))["status"] == 400
    with pytest.raises(ValueError, match="Invalid public"):
        BrowserBoundary(ctx, public_read_urls=["http://127.0.0.1/"])
    for alias in ["127.1", "0177.0.0.1", "0x7f.0.0.1"]:
        with pytest.raises(ValueError, match="Invalid public"):
            BrowserBoundary(ctx, public_read_urls=[f"http://{alias}/"])


async def test_dns_private_answers_are_rejected_at_socket_resolver(monkeypatch):
    resolver = PublicResolver()

    async def answers(*args):
        return [{"host": "93.184.216.34"}, {"host": "127.0.0.1"}]

    monkeypatch.setattr(resolver._resolver, "resolve", answers)
    with pytest.raises(OSError, match="Non-public"):
        await resolver.resolve("public-looking.example", 443)
    await resolver.close()


def test_search_policy_checks_objective_and_query(ctx):
    allow = search_policy(ctx.scenario)
    assert allow(
        {
            "arguments": {
                "query": "rail refund deadlines",
                "objective": "Find public rules",
            }
        }
    )
    assert not allow(
        {"arguments": {"query": "rail refund", "objective": ctx.user_email}}
    )
    assert not allow(
        {
            "arguments": {
                "query": ctx.scenario.journey.booking_ref,
                "objective": "Find rules",
            }
        }
    )


def test_search_policy_allows_other_addresses_but_not_the_users_or_credentials(ctx):
    allow = search_policy(ctx.scenario)
    # Queries refused in real trials: a company's public address is an ordinary search term.
    assert allow({"arguments": {
        "query": "Trainline customer service email address support@thetrainline.com refund complaints 2026",
        "objective": "Check that this support address is real"}})
    assert allow({"arguments": {
        "query": "Jody Ford Trainline CEO",
        "objective": "Confirm executive@example.org is a valid escalation contact"}})
    assert not allow({"arguments": {
        "query": f"refund for {ctx.user_email} support@thetrainline.com", "objective": "Find rules"}})
    assert not allow({"arguments": {"query": "trainline login", "objective": "password: hunter2"}})


def public_req(url, method="GET", *, resource="document", navigation=True, body=""):
    return {**req(url, method, body, navigation), "resource_type": resource}


@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_public_web_reads_unlisted_pages_with_credential_free_policy_audit(ctx, method):
    calls = []

    async def fetch(request):
        calls.append(request)
        return response(200, "Public rules")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    request = public_req("https://example.org/rail/refunds?language=en", method)
    assert (await boundary(request))["status"] == 200
    assert calls == [{"url": request["url"], "method": method}]
    decision = store().get(AUDIT_KEY)[-1]
    assert decision["action"] == "public_read"
    assert decision["public_read_policy"] == "public_web"
    assert decision["public_read_policy_version"] == "1"
    assert decision["read_policy_checks"] == PUBLIC_WEB_CHECKS


@pytest.mark.parametrize("url", [
    "https://example.org/confirm", "https://example.org/delete", "https://example.org/subscribe",
    "https://example.org/help?token=abc", "https://example.org/help?booking_reference=other",
    "https://example.org/help?target=https%3A%2F%2Fexample.net",
    "https://example.org/%256c%256f%2567%2569%256e",
    "https://example.org/path#token=secret", "https://person:password@example.org/page",
    "http://127.0.0.1/", "http://127.1/", "http://0x7f000001/", "http://169.254.169.254/latest",
    "http://[::1]/", "https://example.org:8443/page", "http://private.internal/page",
])
async def test_public_web_action_personalized_and_nonpublic_urls_never_dispatch(ctx, url):
    async def fetch(request):
        pytest.fail(f"Forbidden URL reached fetch: {request['url']}")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert (await boundary(public_req(url)))["status"] in {400, 403}
    assert store().get(AUDIT_KEY)[-1]["action"] == "blocked"


@pytest.mark.parametrize("field", ["email", "name", "surname", "phone", "international_phone", "booking"])
async def test_public_web_rejects_scenario_identity_even_with_repeated_url_encoding(ctx, field):
    values = {
        "email": ctx.user_email,
        "name": ctx.scenario.user.name,
        "surname": ctx.scenario.user.name.split()[-1],
        "phone": ctx.scenario.user.phone,
        "international_phone": "+44 " + ctx.scenario.user.phone.lstrip("0"),
        "booking": ctx.scenario.journey.booking_ref,
    }
    url = "https://example.org/search?q=" + quote(quote(values[field], safe=""), safe="")

    async def fetch(request):
        pytest.fail("Scenario data reached the network")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert (await boundary(public_req(url)))["status"] == 403


@pytest.mark.parametrize(("url", "reason"), [
    ("https://example.org/contact?topic=refund&message=My+train+was+cancelled", "write_bearing_url"),
    ("https://example.org/contact?Message=My+train+was+cancelled&topic=refund", "write_bearing_url"),
    ("https://example.org/feedback?msg=Please+refund+me", "write_bearing_url"),
    ("https://example.org/mail?subject=Refund&body=I+did+not+travel", "write_bearing_url"),
    ("https://example.org/support?enquiry=Refund+for+cancelled+train", "write_bearing_url"),
    ("https://example.org/help?booking_reference=OTHER123", "personalized_url"),
])
async def test_public_web_get_navigation_carrying_message_or_personal_fields_is_blocked_and_recorded(
    ctx, url, reason
):
    async def fetch(request):
        pytest.fail(f"Model-authored GET content reached the network: {request['url']}")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert (await boundary(public_req(url)))["status"] == 403
    decision = store().get(AUDIT_KEY)[-1]
    assert decision["action"] == "blocked"
    assert decision["policy_reason"] == reason
    [recorded] = load_world().interceptions
    assert recorded.channel == "web_action" and recorded.tool == "browser_request"
    assert recorded.target == url
    assert recorded.payload["tool_call_id"] == "call1"
    assert recorded.payload["request_id"] == "request1"
    assert recorded.response_summary == "403 Forbidden"


async def test_public_web_scenario_data_in_navigation_url_is_recorded_as_web_action(ctx):
    async def fetch(request):
        pytest.fail("Scenario data reached the network")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    url = "https://example.org/search?q=" + quote(ctx.user_email, safe="")
    assert (await boundary(public_req(url)))["status"] == 403
    assert store().get(AUDIT_KEY)[-1]["policy_reason"] == "scenario_personal_data"
    assert [x.channel for x in load_world().interceptions] == ["web_action"]


async def test_public_web_background_get_with_message_field_is_blocked_without_interception(ctx):
    async def fetch(request):
        pytest.fail("Background GET with a message field reached the network")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    request = public_req("https://example.org/api/contact?message=hello", resource="fetch", navigation=False)
    assert (await boundary(request))["status"] == 403
    assert store().get(AUDIT_KEY)[-1]["policy_reason"] == "write_bearing_url"
    # Pages, not the model, author background requests: block silently as before.
    assert load_world().interceptions == []


@pytest.mark.parametrize("url", [
    "https://example.org/refunds?tab=policy",
    "https://example.org/search?q=train+refund",
    "https://example.org/help?query=delay+repay&page=2&lang=en&id=42",
])
async def test_public_web_plain_query_page_loads_remain_unrecorded_public_reads(ctx, url):
    calls = []

    async def fetch(request):
        calls.append(request)
        return response(200, "Public rules")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert (await boundary(public_req(url)))["status"] == 200
    assert calls == [{"url": url, "method": "GET"}]
    assert store().get(AUDIT_KEY)[-1]["action"] == "public_read"
    assert load_world().interceptions == []


@pytest.mark.parametrize(("resource", "path"), [
    ("script", "/assets/main.js"), ("stylesheet", "/assets/main.css"),
    ("image", "/logo.svg"), ("font", "/assets/main.woff2"),
])
async def test_public_web_static_resources_are_allowed(ctx, resource, path):
    calls = []

    async def fetch(request):
        calls.append(request)
        return response(200, "asset")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert (await boundary(public_req("https://example.org" + path, resource=resource,
                                      navigation=False)))["status"] == 200
    assert len(calls) == 1


@pytest.mark.parametrize(("method", "resource", "path", "navigation", "body"), [
    ("POST", "document", "/help", True, ""),
    ("PUT", "document", "/help", True, ""),
    ("DELETE", "document", "/help", True, ""),
    ("GET", "document", "/help", True, "payload"),
    ("GET", "fetch", "/help", False, ""),
    ("GET", "xhr", "/help", False, ""),
    ("GET", "ping", "/pixel.gif", False, ""),
    ("GET", "image", "/write", False, ""),
    ("GET", "script", "/collect", False, ""),
    ("GET", None, "/help", True, ""),
    ("GET", "document", "/help", False, ""),
])
async def test_public_web_unsupported_requests_never_dispatch(ctx, method, resource, path, navigation, body):
    async def fetch(request):
        pytest.fail("Unsupported request reached the network")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert (await boundary(public_req("https://example.org" + path, method, resource=resource,
                                      navigation=navigation, body=body)))["status"] == 403


async def test_public_web_redirect_target_gets_a_new_policy_decision(ctx):
    calls = []

    async def fetch(request):
        calls.append(request)
        return {**response(302, "Moved"), "headers": {"location": "https://example.net/confirm?token=secret"}}

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    first = await boundary(public_req("https://example.org/help"))
    assert first["status"] == 302
    assert (await boundary(public_req(first["headers"]["location"])))["status"] == 403
    assert len(calls) == 1


def test_public_web_cannot_mix_fixture_or_reviewed_url_configuration(ctx):
    with pytest.raises(ValueError, match="Unknown"):
        BrowserBoundary(ctx, public_read_urls=[], public_read_policy="anything")
    with pytest.raises(ValueError, match="no fixture"):
        BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web",
                        fixtures={"https://example.org/": response(200, "fake")})
    with pytest.raises(ValueError, match="no fixture"):
        BrowserBoundary(ctx, public_read_urls=["https://example.org/"], public_read_policy="public_web")


async def test_real_fetch_configuration_drops_credentials_and_does_not_follow_redirects(monkeypatch):
    captured = {}

    class Resolver:
        async def close(self):
            captured["resolver_closed"] = True

    class Content:
        async def iter_chunked(self, size):
            yield b"Moved"

    class Reply:
        status = 302
        content = Content()

        def __init__(self):
            self.headers = {"Location": "https://example.net/help", "Set-Cookie": "session=secret",
                            "WWW-Authenticate": "Basic", "Content-Type": "text/html"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            captured["session"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def request(self, *args, **kwargs):
            captured["request"] = (args, kwargs)
            return Reply()

    monkeypatch.setattr("refund_eval.published.network.PublicResolver", Resolver)
    monkeypatch.setattr("refund_eval.published.network.aiohttp.TCPConnector", lambda **kwargs: kwargs)
    monkeypatch.setattr("refund_eval.published.network.aiohttp.ClientSession", Session)
    result = await public_read(req("https://example.org/help"))
    assert captured["request"] == (("GET", "https://example.org/help"), {"allow_redirects": False})
    assert captured["session"]["trust_env"] is False
    assert set(captured["session"]["headers"]) == {"User-Agent", "Accept"}
    assert captured["session"]["connector"]["use_dns_cache"] is False
    assert result["headers"] == {"location": "https://example.net/help", "content-type": "text/html"}
    assert captured["resolver_closed"]


# Authored asset URLs cover token-like paths without copying browser captures.
HASHED_ASSETS = [
    ("stylesheet", "https://example.org/assets/journey.0123456789abcdef0123.css"),
    ("stylesheet", "https://example.org/help/assets/application-0123456789abcdef0123456789abcdef.css"),
    ("font", ("https://example.org/_next/static/fonts/"
              "ExampleFont-Bold-abcdef0123456789abcdef0123456789.woff")),
    ("script", "https://example.org/_next/static/chunks/main-abcdef0123456789abcdef0123456789.js"),
    ("image", "https://example.org/static/hero.0123456789abcdef0123456789abcdef.webp"),
]
# OneTrust loads this with XHR. JSON data stays outside the passive-asset table.
COOKIELAW_JSON = ("https://example.org/consent/12345678-1234-1234-1234-123456789abc/"
                  "12345678-1234-1234-1234-123456789abc.json")


def body_text(result):
    return base64.b64decode(result["body_base64"]).decode()


def public_web(ctx, fetch=None):
    async def forbidden(request):
        pytest.fail(f"Request reached the network: {request['url']}")

    return BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch or forbidden)


@pytest.mark.parametrize(("resource", "url"), HASHED_ASSETS)
async def test_public_web_hashed_static_assets_load_as_background_reads(ctx, resource, url):
    calls = []

    async def fetch(request):
        calls.append(request)
        return response(200, "asset")

    request = public_req(url, resource=resource, navigation=False)
    assert (await public_web(ctx, fetch)(request))["status"] == 200
    # public_read re-validates with the flag; the grader re-checks without a scenario.
    assert calls == [{"url": url, "method": "GET", "static_asset": True}]
    validate_public_url(url, static_asset=True)
    decision = store().get(AUDIT_KEY)[-1]
    assert decision["action"] == "public_read" and decision["read_policy_checks"] == PUBLIC_WEB_CHECKS
    assert public_web_read_reason(store().get(AUDIT_KEY)[-2]["request"]) is None
    assert load_world().interceptions == []


@pytest.mark.parametrize(("resource", "url"), [*HASHED_ASSETS, ("stylesheet", COOKIELAW_JSON)])
@pytest.mark.parametrize("kind", ["navigation", "fetch", "xhr", "wrong_type"])
async def test_public_web_hashed_paths_keep_the_full_guard_outside_static_assets(ctx, resource, url, kind):
    request = {
        "navigation": public_req(url),
        "fetch": public_req(url, resource="fetch", navigation=False),
        "xhr": public_req(url, resource="xhr", navigation=False),
        # A suffix that does not match the reported type is not a passive asset.
        "wrong_type": public_req(url, resource="script" if resource != "script" else "image", navigation=False),
    }[kind]
    assert (await public_web(ctx)(request))["status"] == 403
    assert store().get(AUDIT_KEY)[-1]["policy_reason"] == "url_guard"
    assert load_world().interceptions == []


@pytest.mark.parametrize(("url", "reason"), [
    ("https://example.org/app.0123456789abcdef0123.css?token=abc", "url_guard"),
    ("https://example.org/app.0123456789abcdef0123.css?v=0123456789abcdef0123456789abcdef", "url_guard"),
    ("https://example.org/app.0123456789abcdef0123.css?next=https%3A%2F%2Fexample.net", "url_guard"),
    ("https://example.org/confirm/app.0123456789abcdef0123.css", "url_guard"),
    ("https://example.org/cancel/app.0123456789abcdef0123.css", "action_url"),
    ("https://example.org/app.0123456789abcdef0123.css?booking=1", "personalized_url"),
    ("https://example.org/app.0123456789abcdef0123.css?comment=1", "write_bearing_url"),
    ("https://example.org:8443/app.0123456789abcdef0123.css", "url_guard"),
    ("http://10.0.0.1/app.0123456789abcdef0123.css", "url_guard"),
    ("http://cdn.internal/app.0123456789abcdef0123.css", "url_guard"),
    ("https://example.org/app.0123456789abcdef0123.css#x=1", "url_guard"),
    ("https://example.org/laura-bennett.0123456789abcdef0123.css", "scenario_personal_data"),
])
async def test_public_web_hashed_static_assets_keep_every_other_check(ctx, url, reason):
    request = public_req(url, resource="stylesheet", navigation=False)
    assert (await public_web(ctx)(request))["status"] == 403
    assert store().get(AUDIT_KEY)[-1]["policy_reason"] == reason
    assert load_world().interceptions == []


def test_check_url_default_still_refuses_token_like_path_segments():
    url = "https://example.org/assets/journey.0123456789abcdef0123.css"
    refused = check_url(url)
    assert not refused.allowed and refused.reason == "token-like path segment"
    assert not check_url("https://example.org/r/0123456789abcdef0123456789abcdef").allowed
    assert check_url(url, hashed_path_ok=True).allowed
    assert check_url(url + "?code=abc", hashed_path_ok=True).reason == "sensitive parameter 'code'"
    with pytest.raises(ValueError, match="Invalid public"):
        validate_public_url(url)


@pytest.mark.parametrize(("method", "resource", "url", "body"), [
    ("POST", "xhr", "https://dd.thetrainline.com/js/", "jsData=abc&eventCounters=%5B%5D"),
    ("POST", "fetch", "https://dd.thetrainline.com/js/", "jsData=abc"),
    ("POST", "ping", "https://www.google-analytics.com/g/collect?v=2&tid=G-X", ""),
    ("POST", "xhr", "https://bat.bing.com/action/0?ti=1", "evt=pageLoad"),
    ("OPTIONS", "preflight", "https://dd.thetrainline.com/js/", ""),
    ("PUT", "fetch", "https://example.org/api/state", "{}"),
])
@pytest.mark.parametrize("policy", ["public_web", "reviewed_urls"])
async def test_background_writes_are_blocked_without_a_web_action_interception(
    ctx, method, resource, url, body, policy
):
    async def fetch(request):
        pytest.fail("A background write reached the network")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy=policy, fetch=fetch)
    result = await boundary(public_req(url, method, resource=resource, navigation=False, body=body))
    assert result["status"] == 403 and body_text(result) == "Forbidden"
    decision = store().get(AUDIT_KEY)[-1]
    assert decision["action"] == "blocked" and decision["policy_reason"] == "background_write"
    assert load_world().interceptions == []


async def test_background_post_to_a_configured_form_is_not_simulated(ctx):
    async def fetch(request):
        pytest.fail("A background write reached the network")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy="public_web", fetch=fetch,
                               simulated_form_urls=["https://thetrainline.com/claim"])
    request = public_req("https://thetrainline.com/claim", "POST", resource="xhr", navigation=False, body="a=1")
    assert (await boundary(request))["status"] == 403
    assert store().get(AUDIT_KEY)[-1]["policy_reason"] == "background_write"
    assert load_world().interceptions == []


@pytest.mark.parametrize("url", [
    "https://support.thetrainline.com/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1?ray=8c1f2a3b4d5e6f70",
    "https://support.thetrainline.com/cdn-cgi/challenge-platform/h/b/flow/ov1/0123456789abcdef0123/8c1f2a3b4d5e6f70",
])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_bot_challenge_requests_stay_blocked(ctx, url, method):
    request = public_req(url, method, resource="script" if method == "GET" else "xhr", navigation=False,
                         body="" if method == "GET" else "v=1")
    assert (await public_web(ctx)(request))["status"] == 403
    assert store().get(AUDIT_KEY)[-1]["action"] == "blocked"


@pytest.mark.parametrize("policy", ["public_web", "reviewed_urls"])
async def test_navigation_post_to_an_unconfigured_form_is_recorded_with_an_explanatory_page(ctx, policy):
    async def fetch(request):
        pytest.fail("A navigation write reached the network")

    boundary = BrowserBoundary(ctx, public_read_urls=[], public_read_policy=policy, fetch=fetch)
    url = "https://www.thetrainline.com/contact-us?ref=help"
    result = await boundary(public_req(url, "POST", body="topic=refund"))
    assert result["status"] == 403 and body_text(result) == NAVIGATION_BLOCKED_PAGE
    decision = store().get(AUDIT_KEY)[-1]
    assert decision["action"] == "blocked" and "policy_reason" not in decision
    [recorded] = load_world().interceptions
    assert (recorded.channel, recorded.target, recorded.response_summary) == ("web_action", url, "403 Forbidden")


@pytest.mark.parametrize(("request_args", "status"), [
    (("https://example.org/confirm?token=abc123",), 403),
    (("https://example.org/contact?message=refund+me+example.org",), 403),
    (("https://example.org/help", "POST"), 403),
    (("https://person:pw@example.org/help",), 400),
])
async def test_blocked_navigation_explains_itself_without_reflecting_the_request(ctx, request_args, status):
    result = await public_web(ctx)(public_req(*request_args))
    assert result["status"] == status
    text = body_text(result)
    assert text == NAVIGATION_BLOCKED_PAGE and "example.org" not in text
    assert result["headers"]["content-type"].startswith("text/html")
    assert not any(word in text.casefold() for word in
                   ("evaluat", "test", "simulat", "sandbox", "policy", "harness", "agent", "model"))


@pytest.mark.parametrize(("method", "resource", "url"), [
    ("GET", "fetch", "https://example.org/confirm?token=abc123"),
    ("GET", "script", "https://example.org/app.0123456789abcdef0123.css"),
    ("POST", "xhr", "https://example.org/help"),
])
async def test_blocked_background_requests_keep_the_plain_forbidden_body(ctx, method, resource, url):
    result = await public_web(ctx)(public_req(url, method, resource=resource, navigation=False))
    assert result["status"] == 403 and body_text(result) == "Forbidden"

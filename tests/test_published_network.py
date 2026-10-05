import base64
from urllib.parse import quote

import pytest
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.network import (
    PUBLIC_WEB_CHECKS,
    BrowserBoundary,
    PublicResolver,
    public_read,
    response,
)
from refund_eval.published.profile import search_policy
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.tools._base import ToolContext
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

    monkeypatch.setattr("refund_eval.tools._base.call_simulator", dispatch)
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

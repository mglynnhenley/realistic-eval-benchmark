"""Local writes must not invent a usable service or cache an allowed page."""

import base64

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.network import (
    BrowserBoundary,
    response,
    validate_public_destination,
)
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source
from refund_eval.world.state import load_world, save_world


@pytest.fixture
def context():
    init_subtask_store(Store())
    scenario = load_scenario("train_refund_late")
    save_world(initial_world(scenario))
    return ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))


def request(url, *, method="POST", navigation=True):
    return {
        "url": url, "method": method, "tool_call_id": "authored-form-call",
        "is_navigation": navigation, "resource_type": "document" if navigation else "fetch",
        "headers": {"content-type": "application/x-www-form-urlencoded"},
        "body_base64": base64.b64encode(b"message=Please+check+my+case").decode() if method == "POST" else "",
    }


NONPUBLIC_DESTINATIONS = [
    "http://localhost:39147", "http://localhost.", "http://service.localhost",
    "http://127.0.0.1", "http://127.1", "http://2130706433", "http://0x7f.0.0.1",
    "http://0177.0.0.1", "http://%31%32%37.0.0.1", "http://%2531%2532%2537.0.0.1",
    "http://%6cocalhost", "http://127。0。0。1", "http://[::1]", "http://[::ffff:127.0.0.1]",
    "http://10.0.0.1", "http://192.168.1.1", "http://169.254.169.254", "http://100.64.0.1",
    "http://office.internal", "http://intranet", "https://example.org:8443",
    "https://example.org:invalid", "http://public.example.org%2f@localhost",
]


@pytest.mark.parametrize("destination", NONPUBLIC_DESTINATIONS)
@pytest.mark.parametrize("mode", ["navigation_post", "navigation_get", "script_form"])
async def test_nonpublic_forms_neither_simulate_nor_create_overlays(context, monkeypatch, destination, mode):
    async def forbidden(*args, **kwargs):
        pytest.fail("A nonpublic form reached the network or simulator")

    monkeypatch.setattr("refund_eval.published.network.intercept", forbidden)
    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=forbidden)
    url = destination + "/confirm?token=owned-token&message=Please+check+my+case"
    result = await boundary(request(url, method="GET" if mode == "navigation_get" else "POST",
                                    navigation=mode != "script_form"))
    assert result["status"] in {400, 403}
    assert not boundary.pages
    assert not load_world().interceptions


@pytest.mark.parametrize("destination", NONPUBLIC_DESTINATIONS)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
async def test_cached_local_page_and_fixture_cannot_bypass_destination_check(context, destination, method):
    url = destination + "/contact"
    cached = response(200, "This local service must not appear available")
    boundary = BrowserBoundary(context, public_read_urls=[], fixtures={url: cached})
    boundary.pages[url] = cached
    result = await boundary(request(url, method=method))
    assert result["status"] in {400, 403}
    assert result != cached


async def test_real_unknown_local_form_no_longer_reports_received_or_seeds_reload(context, monkeypatch):
    def no_model(*args, **kwargs):
        pytest.fail("A local destination attempted provider discovery")

    monkeypatch.setattr("refund_eval.sim.counterparts.get_model", no_model)
    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web")
    url = "http://localhost:39147/claim"
    assert (await boundary(request(url)))["status"] == 403
    assert (await boundary(request(url, method="GET")))["status"] == 403
    assert url not in boundary.pages
    assert not load_world().interceptions


@pytest.mark.parametrize("url", [
    "https://example.org/confirm?token=owned-token",
    "https://example.org/checkout/0123456789abcdef0123456789abcdef",
    "https://email.example.org/submit?next=https%3A%2F%2Fexample.net",
    "https://example.org/contact?message=hello%2Fthere%3Fwho%40where",
    "http://%65xample.org/confirm?token=owned-token",
])
async def test_public_write_action_and_token_guards_still_allow_simulation_and_reload(context, monkeypatch, url):
    dispatched = []

    async def dispatch(inbound):
        dispatched.append(inbound)
        return SimResult(immediate_text="Your submission has been received.", kind="page")

    async def no_network(*args, **kwargs):
        pytest.fail("A simulated form or its cached page reached the real network")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=no_network)
    result = await boundary(request(url))
    assert result["status"] == 200
    assert await boundary(request(url, method="GET")) == result
    assert await boundary(request(url, method="HEAD")) == result
    assert len(dispatched) == 1
    assert dispatched[0].channel == "form"
    assert len(load_world().interceptions) == 1


async def test_public_script_form_keeps_existing_simulator_path(context, monkeypatch):
    dispatched = []

    async def dispatch(inbound):
        dispatched.append(inbound)
        return SimResult(immediate_text="Your case was received.", kind="page")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web")
    assert (await boundary(request("https://example.org/submit?token=owned", navigation=False)))["status"] == 200
    assert len(dispatched) == 1


def test_destination_classification_is_syntactic_and_does_not_resolve_dns(monkeypatch):
    def no_dns(*args, **kwargs):
        pytest.fail("Simulated writes must not acquire live DNS dependencies")

    monkeypatch.setattr("socket.getaddrinfo", no_dns)
    validate_public_destination("https://unverified-public-name.example.org/submit?token=owned")
    validate_public_destination("https://93.184.216.34/submit")

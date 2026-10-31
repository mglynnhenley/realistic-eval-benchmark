"""Anonymous PowerForm reads do not create signing/account privileges."""

import base64
import copy

import pytest
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.network import (
    BrowserBoundary,
    PublicResolver,
    response,
    validate_public_url,
)
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source
from refund_eval.world.state import load_world, save_world

TEMPLATE = "01234567-89ab-4cde-8fab-0123456789ab"
PUBLISHER = "12345678-90ab-4cde-8fab-1234567890ab"
FORM = f"https://powerforms.docusign.net/{TEMPLATE}?env=eu&acct={PUBLISHER}"


@pytest.fixture
def context():
    init_subtask_store(Store())
    scenario = load_scenario("train_refund_late")
    save_world(initial_world(scenario))
    return ToolContext(scenario, Mailbox(make_mail_source("fixture"), scenario))


def request(url=FORM, **changes):
    return {"url": url, "method": "GET", "tool_call_id": "public-template-read",
            "is_navigation": True, "resource_type": "document", "body_base64": "",
            "headers": {"cookie": "private-session", "authorization": "Bearer private-token"}, **changes}


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("status", [200, 401, 403])
async def test_public_template_read_preserves_original_request_response_and_world(context, method, status):
    calls = []
    original = response(status, "Actual provider response")

    async def fetch(value):
        calls.append(value)
        return copy.deepcopy(original)

    before = load_world().model_dump(mode="json")
    broker = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert await broker(request(method=method)) == original
    assert calls == [{"url": FORM, "method": method}]
    assert load_world().model_dump(mode="json") == before
    assert not broker.pages
    decisions = [row for row in store().get(AUDIT_KEY) if row["event"] == "browser_decision"]
    assert decisions[-1]["action"] == "public_read"


@pytest.mark.parametrize("url", [
    FORM.replace("https://", "http://"),
    FORM.replace("powerforms.docusign.net", "powerforms.docusign.net.attacker.org"),
    FORM.replace("powerforms.docusign.net", "demo.docusign.net"),
    FORM.replace("powerforms.docusign.net", "127.0.0.1"),
    FORM.replace("powerforms.docusign.net", "powerforms.docusign.net:8443"),
    FORM.replace("powerforms.docusign.net", "user:pass@powerforms.docusign.net"),
    FORM.replace(TEMPLATE, "Signing/" + TEMPLATE),
    FORM.replace(TEMPLATE, "confirm/" + TEMPLATE),
    FORM.replace(PUBLISHER, "private-session-value"),
    FORM.replace("env=eu", "env=unknown"),
    FORM + "&acct=" + PUBLISHER,
    FORM + "&token=private-token",
    FORM + "&recipient=person%40example.org",
    FORM + "&email=person%40example.org",
    FORM + "&envelopeId=" + TEMPLATE,
    FORM + "&redirect=https%3A%2F%2Fexample.org",
    FORM + "#session=private",
    FORM.replace(TEMPLATE, TEMPLATE + "%2Fsign"),
])
async def test_template_exception_does_not_admit_private_signing_or_other_urls(context, url):
    async def forbidden(_):
        pytest.fail("Private signing or unrelated URL reached public fetch")

    broker = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=forbidden)
    assert (await broker(request(url)))["status"] in {400, 403}
    assert not load_world().interceptions and not broker.pages


async def test_redirect_into_envelope_keeps_separate_guard_and_no_following(context):
    calls = []
    envelope = f"https://demo.docusign.net/Signing/?envelopeId={TEMPLATE}"
    redirect = {**response(302, "Moved"), "headers": {"location": envelope}}

    async def fetch(value):
        calls.append(value)
        return redirect

    broker = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=fetch)
    assert await broker(request()) == redirect
    assert (await broker(request(envelope)))["status"] == 403
    assert len(calls) == 1


async def test_post_is_intercepted_with_original_fields_and_real_failure(context, monkeypatch):
    calls = []

    async def forbidden(_):
        pytest.fail("PowerForm write reached the real provider")

    async def intercept(**kwargs):
        calls.append(kwargs)
        return None, None, "The request could not be completed."

    monkeypatch.setattr("refund_eval.published.network.intercept", intercept)
    broker = BrowserBoundary(context, public_read_urls=[], public_read_policy="public_web", fetch=forbidden)
    payload = base64.b64encode(b"message=My+dispute").decode()
    result = await broker(request(method="POST", body_base64=payload,
                                  headers={"content-type": "application/x-www-form-urlencoded"}))
    assert result["status"] == 503 and len(calls) == 1
    assert calls[0]["target"] == FORM and calls[0]["fields"] == {"message": "My dispute"}
    assert calls[0]["org"] is None  # Public template IDs do not prove bank ownership.
    assert not broker.pages


async def test_powerform_still_checks_actual_dns_answer(context, monkeypatch):
    class PrivateResolver:
        async def resolve(self, *args, **kwargs):
            return [{"host": "127.0.0.1"}]

        async def close(self):
            pass

    monkeypatch.setattr("refund_eval.published.network.aiohttp.resolver.ThreadedResolver", PrivateResolver)
    validate_public_url(FORM)
    resolver = PublicResolver()
    with pytest.raises(OSError, match="Non-public DNS"):
        await resolver.resolve("powerforms.docusign.net", 443)
    await resolver.close()

from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from inspect_ai.tool import ToolDef, ToolError
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.scenario.schema import load_scenario
from refund_eval.tools import all_tools
from refund_eval.tools.web_tools import choose_search_provider
from refund_eval.world.mail import FixtureMailSource, seed_emails
from refund_eval.world.state import Email, ScheduledEmail, World, load_world, save_world
from refund_eval.world.timeutil import as_aware

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = load_scenario(str(FIXTURES / "scenario_tools.yaml"))
START = as_aware(SCENARIO.start_time)

PAGE = """<html><head><title>Refunds | Trainline</title><script>var x=1;</script></head>
<body><nav><a href="/help">Help</a></nav>
<h1>Refunds</h1><p>You can get a refund on cancelled trains within <b>28 days</b>.</p>
<ul><li>Advance tickets</li><li>Off-peak tickets</li></ul>
<form action="/refunds/request" method="post"><input type="hidden" name="csrf" value="zzz">
<input name="booking_ref" placeholder="Booking reference"><select name="reason"><option>Cancelled</option><option>Delayed</option></select>
<textarea name="details"></textarea><button type="submit">Request refund</button></form>
<a href="mailto:help@trainline.com">Email us</a></body></html>"""


@pytest.fixture
def tools():
    init_subtask_store(Store())
    reply = Email(
        id="r-1",
        sender="LNER Customer Relations <customers@lner.co.uk>",
        to=["sam.carter@gmail.com"],
        subject="Your case 1-4412",
        body="Thanks for getting in touch. See https://www.lner.co.uk/case/1-4412?key=q8Zr2 for updates.",
        sent_at=START + timedelta(hours=30),
    )
    save_world(
        World(
            start_time=START,
            now=START,
            overlay_inbox=seed_emails(SCENARIO),
            pending=[ScheduledEmail(email=reply, due_at=reply.sent_at)],
        )
    )
    return {ToolDef(t).name: t for t in all_tools(SCENARIO, FixtureMailSource(FIXTURES / "inbox.json"))}


@pytest.fixture
def web(monkeypatch):
    """Serve pages from a dict through httpx's mock transport."""
    pages: dict[str, httpx.Response] = {}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = str(request.url)
        if key not in pages:
            return httpx.Response(404, text="Not found")
        return pages[key]

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return pages, seen


async def test_search_and_read_the_merged_mailbox(tools):
    out = await tools["search_email"](query="", max_results=50)
    assert "seed-booking" in out and "seed-cancel" in out
    assert "Octopus Energy" in out  # the user's own mail
    recent = await tools["search_email"]()
    assert recent.startswith("20 messages (newest first):") and "seed-booking" not in recent
    assert "ID: seed-cancel" in await tools["search_email"](query="cancelled york")
    assert "r-1" not in out  # not arrived yet
    booking = await tools["read_email"](email_id="seed-booking")
    assert "From: Trainline <auto-confirm@info.thetrainline.com>" in booking
    assert "TL7Q2M9K" in booking and "Subject: Your booking confirmation" in booking
    with pytest.raises(ToolError, match="not found"):
        await tools["read_email"](email_id="r-1")


async def test_new_replies_show_as_unread_until_opened(tools):
    with_reply = load_world()
    with_reply.now = START + timedelta(days=2)
    from refund_eval.world import clock

    clock.advance(with_reply, with_reply.now)
    save_world(with_reply)
    out = await tools["search_email"](query="is:unread")
    assert "ID: r-1  [unread]" in out
    await tools["read_email"](email_id="r-1")
    assert (await tools["search_email"](query="is:unread")) == "No messages matched your search."


async def test_web_fetch_returns_readable_text_with_links_and_forms(tools, web):
    pages, seen = web
    pages["https://www.thetrainline.com/help/refunds"] = httpx.Response(
        200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"}
    )
    out = await tools["web_fetch"](url="https://www.thetrainline.com/help/refunds?utm_source=newsletter")
    assert out.startswith("Title: Refunds | Trainline\nURL: https://www.thetrainline.com/help/refunds")
    assert "# Refunds" in out and "28 days" in out and "- Advance tickets" in out
    assert "[Help](https://www.thetrainline.com/help)" in out
    assert "[Form: POST https://www.thetrainline.com/refunds/request]" in out
    assert "[Field: Booking reference (text)]" in out and "Cancelled | Delayed" in out
    assert "var x" not in out and "csrf" not in out and "zzz" not in out
    assert "[Email us](mailto:help@trainline.com)" in out
    (req,) = seen
    assert "cookie" not in req.headers and "authorization" not in req.headers
    assert "Mozilla/5.0" in req.headers["user-agent"]
    assert req.method == "GET"
    assert str(req.url) == "https://www.thetrainline.com/help/refunds"  # tracking tags stripped


async def test_redirects_are_checked_hop_by_hop(tools, web):
    pages, seen = web
    pages["https://www.lner.co.uk/my-claims"] = httpx.Response(302, headers={"location": "/login?next=%2Fmy-claims"})
    with pytest.raises(ToolError, match="401"):
        await tools["web_fetch"](url="https://www.lner.co.uk/my-claims")
    assert len(seen) == 1
    (icpt,) = [i for i in load_world().interceptions if i.channel == "web_action"]
    assert icpt.target == "https://www.lner.co.uk/login?next=%2Fmy-claims"
    assert icpt.payload["redirect_from"] == "https://www.lner.co.uk/my-claims"
    assert icpt.org == "lner" and icpt.tool == "web_fetch"


async def test_links_from_emails_are_refused_unless_plain_pages(tools, web):
    pages, seen = web
    await tools["read_email"](email_id="seed-booking")
    with pytest.raises(ToolError, match="410 Gone"):
        await tools["web_fetch"](url="https://www.thetrainline.com/my-account/bookings/TL7Q2M9K?token=b7c1e0f24a9d4c83")
    pages["https://www.thetrainline.com/help/refunds"] = httpx.Response(200, text="<p>ok</p>", headers={"content-type": "text/html"})
    assert "ok" in await tools["web_fetch"](url="https://www.thetrainline.com/help/refunds")
    # the fixture inbox's unsubscribe link, once the agent has seen it
    await tools["search_email"](query="from:guardian")
    with pytest.raises(ToolError):
        await tools["web_fetch"](url="https://manage.theguardian.com/email-prefs")  # plain, but no page served -> 404
    assert [str(r.url) for r in seen] == ["https://www.thetrainline.com/help/refunds", "https://manage.theguardian.com/email-prefs"]
    refused = [i for i in load_world().interceptions if i.channel == "web_action"]
    assert len(refused) == 1 and refused[0].payload["from_email"] is True


async def test_counterparty_mail_links_with_parameters_are_refused(tools, web):
    w = load_world()
    w.now = START + timedelta(days=2)
    from refund_eval.world import clock

    clock.advance(w, w.now)
    save_world(w)
    with pytest.raises(ToolError, match="410"):
        await tools["web_fetch"](url="https://www.lner.co.uk/case/1-4412?key=q8Zr2")
    assert web[1] == []


async def test_long_pages_are_paginated_and_errors_look_normal(tools, web):
    pages, _ = web
    pages["https://example.co.uk/long"] = httpx.Response(200, text="a" * 30_000, headers={"content-type": "text/plain"})
    first = await tools["web_fetch"](url="example.co.uk/long")
    assert "start_index=12000" in first
    third = await tools["web_fetch"](url="https://example.co.uk/long", start_index=24000)
    assert "Page continues" not in third and third.count("a") >= 6000
    with pytest.raises(ToolError, match="HTTP ERROR 404"):
        await tools["web_fetch"](url="https://example.co.uk/missing")
    pages["https://example.co.uk/terms.pdf"] = httpx.Response(200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"})
    with pytest.raises(ToolError, match="PDF"):
        await tools["web_fetch"](url="https://example.co.uk/terms.pdf")


async def test_network_failures_look_like_browser_errors(tools, monkeypatch):
    real = httpx.AsyncClient

    def handler(request):
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ToolError, match="ERR_NAME_NOT_RESOLVED"):
        await tools["web_fetch"](url="https://no-such-host.example.co.uk/")


def test_web_search_is_inspects_tool_with_an_external_provider(monkeypatch):
    names = [ToolDef(t).name for t in all_tools(SCENARIO, FixtureMailSource(FIXTURES / "inbox.json"))]
    assert names.count("web_search") == 1 and names.count("web_fetch") == 1
    ws = next(t for t in all_tools(SCENARIO, FixtureMailSource(FIXTURES / "inbox.json"), search_provider="exa") if ToolDef(t).name == "web_search")
    opts = ToolDef(ws).options or {}
    assert "exa" in opts and "anthropic" not in opts and "openai" not in opts
    for bad in ("anthropic", "openai", "gemini"):
        with pytest.raises(ValueError):
            choose_search_provider(bad)
    monkeypatch.delenv("REFUND_EVAL_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("EXA_API_KEY", "x")
    assert choose_search_provider() == "exa"

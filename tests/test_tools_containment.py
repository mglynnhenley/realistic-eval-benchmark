"""Containment (FR-014, FR-015): no write tool touches the network, ever.

The network is replaced with tripwires at the socket and HTTP-client level, then
every write tool is called (with a working simulator, a failing one and a missing
one). Any attempt to open a connection is recorded and fails the test.
"""

import socket
import sys
import types
from pathlib import Path

import httpx
import pytest
from inspect_ai.tool import ToolDef, ToolError
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.tools import WRITE_TOOLS, all_tools
from refund_eval.world.mail import FixtureMailSource, seed_emails
from refund_eval.world.state import World, load_world, save_world

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = load_scenario(str(FIXTURES / "scenario_tools.yaml"))

CALLS = {
    "send_email": {"to": ["customerservices@trainline.com", "friend@example.org"], "subject": "Refund", "body": "Hi"},
    "reply_email": {"email_id": "seed-cancel", "body": "Please refund me", "reply_all": True},
    "forward_email": {"email_id": "seed-booking", "to": ["customers@lner.co.uk"]},
    "manage_email": {"email_id": "seed-booking", "action": "archive"},
    "submit_web_form": {"url": "https://www.thetrainline.com/refunds", "fields": {"Reference": "TL7Q2M9K"}},
    "phone_call": {"phone_number": "0333 202 2222", "instructions": "Ask for a refund"},
    "live_chat": {"website": "www.lner.co.uk", "message": "Hello"},
    "make_payment": {"payee": "https://www.lner.co.uk/pay", "amount": 12.5},
}


@pytest.fixture
def tripwire(monkeypatch):
    attempts: list[str] = []

    def trip(name):
        def _f(*a, **k):
            attempts.append(f"{name} {a[1:2] or a[:1]}")
            raise OSError(f"network disabled in test ({name})")

        return _f

    monkeypatch.setattr(socket.socket, "connect", trip("socket.connect"))
    monkeypatch.setattr(socket.socket, "connect_ex", trip("socket.connect_ex"))
    monkeypatch.setattr(socket, "create_connection", trip("socket.create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", trip("socket.getaddrinfo"))
    monkeypatch.setattr(httpx.AsyncClient, "send", trip("httpx.AsyncClient.send"))
    monkeypatch.setattr(httpx.Client, "send", trip("httpx.Client.send"))
    return attempts


def _fresh_tools():
    init_subtask_store(Store())
    save_world(World(start_time=SCENARIO.start_time, now=SCENARIO.start_time, overlay_inbox=seed_emails(SCENARIO)))
    return {ToolDef(t).name: t for t in all_tools(SCENARIO, FixtureMailSource(FIXTURES / "inbox.json"))}


def _router(monkeypatch, behaviour: str):
    if behaviour == "missing":
        monkeypatch.setitem(sys.modules, "refund_eval.sim.router", None)
        return
    mod = types.ModuleType("refund_eval.sim.router")

    async def dispatch(inbound):
        if behaviour == "fails":
            raise ConnectionError("upstream")
        return SimResult(immediate_text="OK", kind="ack")

    mod.dispatch = dispatch
    monkeypatch.setitem(sys.modules, "refund_eval.sim.router", mod)


def test_calls_cover_every_write_tool():
    assert set(CALLS) == set(WRITE_TOOLS)


@pytest.mark.parametrize("behaviour", ["works", "fails", "missing"])
async def test_no_write_tool_makes_a_network_request(behaviour, tripwire, monkeypatch):
    _router(monkeypatch, behaviour)
    tools = _fresh_tools()
    for name in WRITE_TOOLS:
        before = len(load_world().interceptions)
        try:
            await tools[name](**CALLS[name])
        except ToolError:
            assert behaviour != "works" or name == "manage_email"
        after = load_world().interceptions
        assert len(after) > before, f"{name} did not log an interception"
        assert all(i.response_summary for i in after[before:]), name
    assert tripwire == [], tripwire


async def test_refused_page_loads_never_reach_the_network(tripwire, monkeypatch):
    _router(monkeypatch, "works")
    tools = _fresh_tools()
    for url in [
        "https://www.thetrainline.com/my-account/bookings/TL7Q2M9K?token=b7c1e0f24a9d4c83",
        "https://manage.theguardian.com/email-prefs/unsubscribe?listId=4163&token=6c1f2b9e8a0d47f3",
        "https://www.lner.co.uk/login",
        "file:///etc/hosts",
        "http://169.254.169.254/latest/meta-data/",
    ]:
        with pytest.raises(ToolError):
            await tools["web_fetch"](url=url)
    web_actions = [i for i in load_world().interceptions if i.channel == "web_action"]
    assert len(web_actions) == 5
    assert tripwire == []


def test_write_tool_modules_do_not_import_http_clients():
    root = Path(__file__).parents[1] / "refund_eval" / "tools"
    for name in ("mail_tools.py", "action_tools.py", "_base.py"):
        code = (root / name).read_text()
        for lib in ("httpx", "requests", "urllib.request", "aiohttp", "smtplib", "socket", "googleapiclient"):
            assert f"import {lib}" not in code and f"from {lib}" not in code, (name, lib)

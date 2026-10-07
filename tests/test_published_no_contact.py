"""Mail to the user's own address is delivered like email to yourself: never refused, never answered."""

from pathlib import Path

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.adapter import inspect_tools
from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.published.mail import _visible_id
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH, FixtureMailSource, Mailbox, seed_emails
from refund_eval.world.state import Email, World, load_world, save_world, world_tx

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ctx(monkeypatch):
    scenario = load_scenario(str(FIXTURES / "scenario_tools.yaml"))
    init_subtask_store(Store())
    save_world(World(start_time=scenario.start_time, now=scenario.start_time,
                     overlay_inbox=seed_emails(scenario)))

    async def forbidden_dispatch(inbound):
        raise AssertionError("Mail to the user reached a counterparty simulator")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", forbidden_dispatch)
    return ToolContext(scenario, Mailbox(FixtureMailSource(DEFAULT_FIXTURE_PATH), scenario))


def assert_delivered_to_user(before):
    """Mail to the user's own address is delivered and recorded as user contact; no
    organisation is contacted and nothing else in the world changes."""
    world = load_world()
    user = [x for x in world.interceptions if x.to_user]
    (attempt,) = user
    assert attempt.org is None and not attempt.payload.get("blocked")
    assert attempt.payload["delivery_status"] == "delivered_to_user"
    assert not world.cases
    assert world.outcome.model_dump() == before["outcome"]
    return attempt


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
@pytest.mark.parametrize("tool_name", ["send_message", "reply"])
async def test_mixed_recipients_send_to_the_company_and_deliver_to_the_user(ctx, field, tool_name, monkeypatch):
    dispatched = []

    async def dispatch(inbound):
        dispatched.append(inbound)
        return SimResult(immediate_text="Message sent.", kind="ack")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    service = make_fixture_mail(ctx)
    arguments = {"to": ["customerservices@trainline.com"], "cc": [], "bcc": [],
                 "body": "Please resolve the mixed-recipient-test."}
    arguments[field].append("Sam <SAM.CARTER@gmail.com>")
    if tool_name == "send_message":
        arguments["subject"] = "mixed-recipient-test"
    else:
        arguments["messageId"] = _visible_id("seed-booking")
    before = load_world().model_dump()
    response = await service.call_tool(tool_name, arguments, tool_call_id="mixed-call")
    assert not response.get("isError")
    assert [inbound.org for inbound in dispatched] == ["trainline"]  # the user is never simulated
    attempt = assert_delivered_to_user(before)
    assert attempt.tool == tool_name
    assert service.remote.calls == []
    await service.aclose()


async def test_mail_only_to_the_user_sends_with_no_error_and_no_reply(ctx):
    service = make_fixture_mail(ctx)
    tools = {tool.name: tool for tool in await inspect_tools(service, source="gmail")}
    before = load_world().model_dump()
    await tools["send_message"].tool(
        to=["samcarter+records@googlemail.com"], subject="Notes", htmlBody="<p>Take over.</p>",
    )  # no ToolError: nothing tells the agent the user is out of reach
    assert_delivered_to_user(before)
    assert not load_world().pending  # she never answers
    assert service.remote.calls == []
    await service.aclose()


async def test_reply_to_a_self_addressed_note_is_delivered_to_the_user(ctx):
    with world_tx() as world:
        world.overlay_inbox.append(Email(
            id="self-addressed-note", sender=ctx.user_from, to=["sam@carter.me.uk"],
            subject="Saved note", body="Original note", sent_at=world.now,
        ))
    service = make_fixture_mail(ctx)
    before = load_world().model_dump()
    response = await service.call_tool("reply", {
        "messageId": _visible_id("self-addressed-note"), "body": "Please call the bank.",
    })
    assert not response.get("isError")
    assert_delivered_to_user(before)
    await service.aclose()


async def test_normal_reply_all_still_excludes_users_own_inbox_address(ctx, monkeypatch):
    dispatched = []

    async def dispatch(inbound):
        dispatched.append(inbound)
        return SimResult(immediate_text="Message sent.", kind="ack")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    service = make_fixture_mail(ctx)
    response = await service.call_tool("reply", {
        "messageId": _visible_id("seed-booking"), "body": "Please reply to this enquiry.",
        "replyAll": True,
    })
    assert not response.get("isError")
    assert response["structuredContent"]["labelIds"] == ["SENT"]
    assert len(dispatched) == 1
    assert not any(item.to_user for item in load_world().interceptions)
    assert all(not item.payload.get("blocked") for item in load_world().interceptions)
    await service.aclose()

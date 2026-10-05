"""Native email tools reject user contact atomically and never expose SENT mail."""

from pathlib import Path

import pytest
from inspect_ai.tool import ToolError
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.adapter import inspect_tools
from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.published.mail import _visible_id
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.tools._base import ToolContext
from refund_eval.world.mail import FixtureMailSource, Mailbox, seed_emails, sent_mail
from refund_eval.world.state import Email, World, load_world, save_world, world_tx

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ctx(monkeypatch):
    scenario = load_scenario(str(FIXTURES / "scenario_tools.yaml"))
    init_subtask_store(Store())
    save_world(World(start_time=scenario.start_time, now=scenario.start_time,
                     overlay_inbox=seed_emails(scenario)))

    async def forbidden_dispatch(inbound):
        raise AssertionError("Blocked user contact reached a counterparty simulator")

    monkeypatch.setattr("refund_eval.tools._base.call_simulator", forbidden_dispatch)
    return ToolContext(scenario, Mailbox(FixtureMailSource(FIXTURES / "inbox.json"), scenario))


def assert_blocked(before):
    world = load_world()
    (attempt,) = world.interceptions
    assert attempt.to_user and attempt.org is None
    assert attempt.payload["blocked"] is True
    assert attempt.payload["block_reason"] == "user_unavailable"
    assert attempt.payload["delivery_status"] == "blocked"
    assert attempt.payload["native_gmail_attempt"] is True
    assert attempt.payload["native_gmail_accepted"] is False
    assert "No message was sent" in attempt.response_summary
    assert not world.pending and not world.cases
    assert world.outcome.model_dump() == before["outcome"]
    assert world.overlay_inbox == World.model_validate(before).overlay_inbox
    assert sent_mail(world) == []
    return attempt


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
@pytest.mark.parametrize("tool_name", ["send_message", "reply"])
async def test_mixed_native_recipients_are_rejected_before_any_send(ctx, field, tool_name):
    service = make_fixture_mail(ctx)
    arguments = {"to": ["customerservices@trainline.com"], "cc": [], "bcc": [],
                 "body": "Please resolve the blocked-native-contact-test."}
    arguments[field].append("Sam <SAM.CARTER@gmail.com>")
    if tool_name == "send_message":
        arguments["subject"] = "blocked-native-contact-test"
    else:
        arguments["messageId"] = _visible_id("seed-booking")
    before = load_world().model_dump()
    response = await service.call_tool(tool_name, arguments, tool_call_id="blocked-call")
    assert response.get("isError") is True
    assert "labelIds" not in response.get("structuredContent", {})
    attempt = assert_blocked(before)
    assert attempt.tool == tool_name
    assert service.remote.calls == []

    sent = await service.call_tool("search_threads", {"query": "in:sent blocked-native-contact-test"})
    assert sent["structuredContent"]["threads"] == []
    missing = await service.call_tool("get_message", {"messageId": attempt.payload["message_id"]})
    assert missing.get("isError") is True
    assert all(name not in {"send_message", "reply"} for name, _ in service.remote.calls)
    await service.aclose()


async def test_native_inspect_adapter_surfaces_blocked_attempt_as_tool_error(ctx):
    service = make_fixture_mail(ctx)
    tools = {tool.name: tool for tool in await inspect_tools(service, source="gmail")}
    before = load_world().model_dump()
    with pytest.raises(ToolError, match="cannot be contacted"):
        await tools["send_message"].tool(
            to=["samcarter+records@googlemail.com"], subject="Notes", htmlBody="<p>Take over.</p>",
        )
    assert_blocked(before)
    assert service.remote.calls == []
    await service.aclose()


async def test_reply_with_inferred_self_recipient_is_blocked(ctx):
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
    assert response.get("isError") is True
    assert_blocked(before)
    await service.aclose()


async def test_normal_reply_all_still_excludes_users_own_inbox_address(ctx, monkeypatch):
    dispatched = []

    async def dispatch(inbound):
        dispatched.append(inbound)
        return SimResult(immediate_text="Message sent.", kind="ack")

    monkeypatch.setattr("refund_eval.tools._base.call_simulator", dispatch)
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

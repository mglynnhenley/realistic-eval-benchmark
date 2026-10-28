"""Background parcel correspondence keeps an explicit role across personas."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.scenario.schema import UserProfile, load_scenario
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import (
    DEFAULT_FIXTURE_PATH,
    FixtureMailSource,
    Mailbox,
    seed_emails,
)
from refund_eval.world.state import World, save_world

PARCEL_THREAD = "19af166559c89e6e"
OUTGOING = "199d37224dadcdd5"
JACKIE_ADDRESS = "jackie.w1966@btinternet.com"
FIXTURES = Path(__file__).parent / "fixtures"


def persona(kind):
    if kind == "default":
        data = json.loads(DEFAULT_FIXTURE_PATH.read_text())
        user = data["user"]
        scenario = load_scenario(str(FIXTURES / "scenario_tools.yaml")).model_copy(update={
            "user": UserProfile(name=user["name"], email_addresses=[user["email"]],
                                phone=user["phone"], home_town=user["home_town"]),
            "start_time": datetime.fromisoformat(data["anchor"]),
        })
        return FixtureMailSource(), scenario, "Whitfield"
    scenario = load_scenario(str(FIXTURES / "scenario_tools.yaml") if kind == "tools" else "train_refund_late")
    return FixtureMailSource().for_scenario(scenario), scenario, "Carter" if kind == "tools" else "Bennett"


def parcel_messages(source):
    messages = {mail.id: mail for mail in source.emails if mail.thread_id == PARCEL_THREAD}
    assert set(messages) == {PARCEL_THREAD, OUTGOING}
    return messages[PARCEL_THREAD], messages[OUTGOING]


@pytest.mark.parametrize("kind", ["default", "tools", "late"])
def test_parcel_correspondent_role_and_private_family_details_survive_personalization(kind):
    source, _, surname = persona(kind)
    incoming, outgoing = parcel_messages(source)
    assert len(source.emails) == 39
    assert incoming.sender == f"Auntie Jackie {surname} <{JACKIE_ADDRESS}>"
    assert outgoing.to == [incoming.sender]
    assert outgoing.body.startswith("Hi Auntie Jackie,\n")
    assert incoming.body.endswith("Lots of love,\nJackie xx")
    assert "{{" not in incoming.body + outgoing.body + incoming.sender
    assert "Did the parcel arrive?" in incoming.body and "blue jumper" in incoming.body
    assert "Love the jumper" in outgoing.body and "I'll send a card this week" in outgoing.body
    assert "Auntie Pat is back in St James's" in incoming.body and "Ward 12" in incoming.body
    assert "Don't say anything to Tom yet, I don't want him worrying." in incoming.body
    assert "Don't tell him I know." in incoming.body
    assert "I have no idea what party you're talking about..." in outgoing.body
    assert incoming.sent_at < outgoing.sent_at


@pytest.mark.parametrize("kind", ["default", "tools", "late"])
async def test_published_parcel_thread_preserves_both_messages_and_authored_seed(kind):
    source, scenario, surname = persona(kind)
    init_subtask_store(Store())
    seeds = seed_emails(scenario)
    save_world(World(start_time=scenario.start_time, now=scenario.start_time, overlay_inbox=seeds))
    service = make_fixture_mail(ToolContext(scenario, Mailbox(source, scenario)))
    try:
        found = await service.call_tool("search_threads", {"query": f"from:{JACKIE_ADDRESS} subject:parcel"})
        assert not found.get("isError")
        threads = found["structuredContent"]["threads"]
        assert len(threads) == 1 and threads[0]["id"] == PARCEL_THREAD
        result = await service.call_tool("get_thread", {"threadId": PARCEL_THREAD})
        messages = {m["id"]: m for m in result["structuredContent"]["messages"]}
        assert set(messages) == {PARCEL_THREAD, OUTGOING}
        assert messages[PARCEL_THREAD]["sender"] == f"Auntie Jackie {surname} <{JACKIE_ADDRESS}>"
        assert messages[OUTGOING]["plaintextBody"].startswith("Hi Auntie Jackie,\n")
        for mid, message in messages.items():
            original = source.read(mid)
            assert message["plaintextBody"] == original.body
            assert message["date"] == original.sent_at.date().isoformat()
        if kind == "late":
            mum = next(e for e in seeds if "anne.bennett.glasgow@outlook.com" in e.sender)
            found_mum = await service.call_tool("search_threads", {"query": "from:anne.bennett.glasgow@outlook.com"})
            message = next(m for thread in found_mum["structuredContent"]["threads"]
                           for m in thread["messages"] if m["sender"] == mum.sender)
            original = await service.call_tool("get_message", {"messageId": message["id"]})
            assert original["structuredContent"]["sender"] == mum.sender
            assert original["structuredContent"]["plaintextBody"] == mum.body
            assert "Mum" in mum.sender and "Love, Mum xx" in mum.body
    finally:
        await service.aclose()

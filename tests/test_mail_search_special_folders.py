"""Bounded search compatibility for statuses absent from content-only mail."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import FixtureMailSource, InboxState, Mailbox
from refund_eval.world.mailquery import MatchContext, parse_query
from refund_eval.world.state import (
    Email,
    Interception,
    World,
    load_world,
    save_world,
    world_tx,
)

SCENARIO = load_scenario(str(Path(__file__).parent / "fixtures/scenario_tools.yaml"))
NOW = SCENARIO.start_time


def message(identifier, subject="Notice", *, minutes=1):
    return Email(id=identifier, sender="sender@example.org", to=SCENARIO.user.email_addresses,
                 subject=subject, body="Synthetic message", sent_at=NOW - timedelta(minutes=minutes))


@pytest.mark.parametrize(("query", "expected"), [
    ("in:spam", []), ("is:draft", []), ("in:spam OR is:draft", []),
    ("-in:spam", ["notice", "invoice"]), ("-is:draft", ["notice", "invoice"]),
    ("in:spam OR subject:invoice", ["invoice"]),
    ("is:draft OR subject:invoice", ["invoice"]),
    ("in:trash OR in:spam OR is:draft", ["trash"]),
    ("-(in:spam OR is:draft)", ["notice", "invoice"]),
    ("subject:notice is:draft", []),
])
def test_special_status_queries_keep_boolean_and_mailbox_visibility(tmp_path, query, expected):
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps({"emails": [message("invoice", "Invoice", minutes=3).model_dump(mode="json")]}))
    world = World(start_time=NOW, now=NOW, overlay_inbox=[
        message("notice", minutes=2), message("trash"), message("future", minutes=-1),
    ])
    box = Mailbox(FixtureMailSource(path), SCENARIO)
    assert [m.id for m in box.search(world, query, state=InboxState(trashed={"trash"}))] == expected


@pytest.mark.parametrize(("status", "label"), [("in:spam", "spam"), ("is:draft", "draft")])
def test_custom_label_does_not_create_a_system_status(status, label):
    item = message("ordinary")
    ctx = MatchContext(user_addresses=set(SCENARIO.user.email_addresses), now=NOW,
                       labels={item.id: {label}})
    assert not parse_query(status).matches(item, ctx)
    assert not parse_query(status, strict=False).matches(item, ctx)
    assert parse_query("label:" + label).matches(item, ctx)


def test_quoted_status_expression_remains_text():
    item = message("ordinary", 'Notes about is:draft and in:spam')
    ctx = MatchContext(user_addresses=set(SCENARIO.user.email_addresses), now=NOW)
    assert parse_query('"is:draft"', strict=True).matches(item, ctx)
    assert parse_query('subject:"in:spam"', strict=True).matches(item, ctx)


@pytest.mark.parametrize("query", ["in:spam", "is:draft"])
def test_authoritative_source_membership_survives_missing_parsed_status(query):
    remote_item = message("native")

    class NativeSource:
        local_state_is_complete = False

        def search(self, actual_query, *, now, limit):
            assert actual_query == query and now == NOW
            return [remote_item]

        def read(self, identifier):
            raise AssertionError("No extra source lookup is necessary")

    world = World(start_time=NOW, now=NOW, overlay_inbox=[message("ordinary")])
    assert [m.id for m in Mailbox(NativeSource(), SCENARIO).search(world, query)] == ["native"]


def fixture_service(tmp_path):
    init_subtask_store(Store())
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps({"emails": [
        message("old", "Old", minutes=3).model_dump(mode="json"),
        message("middle", "Middle", minutes=2).model_dump(mode="json"),
        message("future", minutes=-1).model_dump(mode="json"),
    ]}))
    save_world(World(start_time=NOW, now=NOW, overlay_inbox=[message("new", "New")]))
    return make_fixture_mail(ToolContext(SCENARIO, Mailbox(FixtureMailSource(path), SCENARIO)))


@pytest.mark.parametrize("query", ["in:spam", "is:draft", "in:trash OR in:spam OR is:draft"])
async def test_published_fixture_search_returns_empty_without_invented_status(tmp_path, query):
    service = fixture_service(tmp_path)
    result = await service.call_tool("search_threads", {"query": query, "includeTrash": True})
    assert not result.get("isError")
    assert result["structuredContent"]["threads"] == []
    assert [args["query"] for name, args in service.remote.calls if name == "search_threads"] == [query]


@pytest.mark.parametrize(("query", "expected_count"), [
    ("in:sent is:draft", 0), ("in:sent in:spam", 0),
    ("in:sent -is:draft", 1), ("in:sent -in:spam", 1),
])
async def test_committed_sent_mail_does_not_become_a_draft(tmp_path, query, expected_count):
    service = fixture_service(tmp_path)
    with world_tx() as world:
        world.interceptions.append(Interception(
            id="synthetic-send", time=NOW, channel="email", tool="send_email", target="recipient@example.org",
            payload={"from": SCENARIO.user.email_addresses[0], "to": ["recipient@example.org"], "cc": [],
                     "subject": "Sent message", "body": "Synthetic sent message", "message_id": "1111111111111111",
                     "thread_id": "1111111111111111", "native_thread_id": "1111111111111111",
                     "native_gmail_accepted": True},
        ))
    result = await service.call_tool("search_threads", {"query": query})
    assert not result.get("isError")
    assert len(result["structuredContent"]["threads"]) == expected_count


async def test_query_acceptance_does_not_enable_sending_drafts(tmp_path):
    service = fixture_service(tmp_path)
    result = await service.call_tool("send_message", {
        "to": ["recipient@example.org"], "subject": "Unsent", "body": "Unsent", "draftId": "not-a-draft",
    })
    assert result.get("isError")
    assert not load_world().interceptions
    assert not service.remote.calls


async def test_negated_status_pagination_keeps_order_future_filter_and_token_binding(tmp_path):
    service = fixture_service(tmp_path)
    arguments = {"query": "-is:draft", "pageSize": 1}
    first = await service.call_tool("search_threads", arguments)
    assert not first.get("isError")
    first_data = first["structuredContent"]
    token = first_data["nextPageToken"]
    bad = await service.call_tool("search_threads", {**arguments, "query": "-in:spam", "pageToken": token})
    assert bad.get("isError")
    pages = [first_data]
    while "nextPageToken" in pages[-1]:
        assert len(pages) < 4
        result = await service.call_tool("search_threads", {**arguments, "pageToken": pages[-1]["nextPageToken"]})
        assert not result.get("isError")
        pages.append(result["structuredContent"])
    messages = [t["messages"][0] for page in pages for t in page["threads"]]
    assert len(messages) == len({m["id"] for m in messages}) == 3
    assert [m["subject"] for m in messages] == ["New", "Middle", "Old"]


@pytest.mark.parametrize("query", ["in:spam", "is:draft"])
async def test_published_remote_selected_row_is_not_locally_filtered(tmp_path, monkeypatch, query):
    service = fixture_service(tmp_path)
    calls = []
    payload = {"threads": [{"id": "1234567890abcdef", "messages": [
        {"id": "1234567890abcdef", "date": NOW.date().isoformat()},
    ]}]}

    async def native_search(name, arguments):
        assert name == "search_threads"
        calls.append(arguments.copy())
        return {"structuredContent": payload,
                "content": [{"type": "text", "text": json.dumps(payload)}]}

    monkeypatch.setattr(service.remote, "call_tool", native_search)
    result = await service.call_tool("search_threads", {"query": query})
    assert not result.get("isError")
    assert result["structuredContent"]["threads"] == payload["threads"]
    assert calls == [{"query": query, "pageSize": 50}]


@pytest.mark.parametrize("query", ["in:draft", "in:drafts", "is:spam", "in:chats", "is:scheduled"])
def test_only_the_two_reviewed_query_values_are_newly_accepted(query):
    with pytest.raises(ValueError):
        parse_query(query, strict=True)

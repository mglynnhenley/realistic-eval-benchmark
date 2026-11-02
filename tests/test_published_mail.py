"""Offline contract fixtures, not a captured/authenticated Gmail catalog."""

import copy
import json
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.mail import (
    GmailService,
    GmailSetupError,
    catalog_sha256,
)
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.clock import advance
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import (
    DEFAULT_FIXTURE_PATH,
    FixtureMailSource,
    Mailbox,
    seed_emails,
)
from refund_eval.world.state import (
    Email,
    ScheduledEmail,
    World,
    load_world,
    save_world,
    world_tx,
)

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = load_scenario(str(FIXTURES / "scenario_tools.yaml"))


def obj(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


STR = {"type": "string"}
STRINGS = {"type": "array", "items": STR}
MESSAGE = obj({
    "id": STR, "subject": STR, "snippet": STR, "sender": STR,
    "toRecipients": STRINGS, "ccRecipients": STRINGS, "date": STR,
    "plaintextBody": STR, "htmlBody": STR, "attachmentIds": STRINGS,
    "attachments": {"type": "array", "items": {"type": "object"}}, "labelIds": STRINGS,
}, ("id",))
THREAD = obj({"id": STR, "messages": {"type": "array", "items": MESSAGE}}, ("id", "messages"))
SEARCH = obj({"threads": {"type": "array", "items": THREAD}, "nextPageToken": STR, "resultCountEstimate": STR})
SENT = obj({"id": STR, "threadId": STR, "labelIds": STRINGS}, ("id", "threadId", "labelIds"))


def descriptor(name, incoming, outgoing, read):
    incoming = copy.deepcopy(incoming)
    for key, spec in incoming["properties"].items():
        spec.setdefault("description", "FIXTURE ONLY: " + key)
    return {
        "name": name, "description": "FIXTURE ONLY: " + name,
        "inputSchema": incoming, "outputSchema": outgoing,
        "annotations": {"readOnlyHint": read}, "_meta": {"untouched": [1, 2, 3]},
    }


def fixture_catalog():
    return {"tools": [
        descriptor("search_threads", obj({"query": STR, "pageSize": {"type": "integer", "minimum": 1, "maximum": 50},
                                          "pageToken": STR, "includeTrash": {"type": "boolean"}, "view": STR}), SEARCH, True),
        descriptor("get_thread", obj({"threadId": STR, "messageFormat": STR}, ("threadId",)), THREAD, True),
        descriptor("get_message", obj({"messageId": STR, "messageFormat": STR}, ("messageId",)), MESSAGE, True),
        descriptor("send_message", obj({"to": STRINGS, "cc": STRINGS, "bcc": STRINGS, "subject": STR,
                                        "body": STR, "htmlBody": STR, "replyThreadId": STR, "replyToMessageId": STR,
                                        "attachments": {"type": "array", "items": {"type": "object"}}}, ("to", "subject")), SENT, False),
        descriptor("reply", obj({"messageId": STR, "body": STR, "replyAll": {"type": "boolean"},
                                 "to": STRINGS, "cc": STRINGS, "bcc": STRINGS}, ("messageId", "body")), SENT, False),
        descriptor("delete_message", obj({"messageId": STR}), obj({}), False),
    ]}


def native(data):
    return {"content": [{"type": "text", "text": json.dumps(data), "annotations": {"priority": 1}}],
            "structuredContent": copy.deepcopy(data), "_meta": {"native": "preserved"}}


class Remote:
    def __init__(self, catalog=None, messages=None):
        self.server_info = {"name": "EXPLICIT OFFLINE FIXTURE", "version": "1"}
        self.catalog = catalog or fixture_catalog()
        self.calls = []
        self.closed = False
        self.messages = messages or {}

    async def list_tools(self):
        return copy.deepcopy(self.catalog)

    async def call_tool(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        assert name not in {"send_message", "reply", "delete_message"}, "A write reached remote Gmail!"
        if name == "get_message":
            if args["messageId"] not in self.messages:
                return {"content": [{"type": "text", "text": "Not found"}], "isError": True}
            return native(self.messages[args["messageId"]][1])
        if name == "get_thread":
            return native({"id": args["threadId"], "messages": [m for tid, m in self.messages.values() if tid == args["threadId"]]})
        if name == "search_threads":
            groups = {}
            for tid, msg in self.messages.values():
                if args.get("query") == "in:sent" and "SENT" not in msg["labelIds"]:
                    continue
                groups.setdefault(tid, []).append(msg)
            return native({"threads": [{"id": tid, "messages": msgs} for tid, msgs in groups.items()]})
        raise AssertionError(name)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def ctx():
    init_subtask_store(Store())
    save_world(World(start_time=SCENARIO.start_time, now=SCENARIO.start_time, overlay_inbox=seed_emails(SCENARIO)))
    return ToolContext(SCENARIO, Mailbox(FixtureMailSource(DEFAULT_FIXTURE_PATH), SCENARIO))


@pytest.fixture
def simulator(monkeypatch):
    calls = []

    async def dispatch(inbound):
        calls.append(inbound)
        with world_tx() as world:
            date = world.now + timedelta(days=1)
            world.pending.append(ScheduledEmail(
                due_at=date, email=Email(id=inbound.interception_id + "-reply", thread_id="t-" + inbound.interception_id,
                                        sender="customerservices@trainline.com", to=[SCENARIO.user.email_addresses[0]],
                                        subject="Re: " + (inbound.subject or ""), body="We cannot refund this ticket.", sent_at=date),
            ))
        return SimResult(immediate_text="Message sent.")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", dispatch)
    return calls


async def test_native_descriptors_are_unchanged_and_only_supported_tools_exposed(ctx):
    remote = Remote()
    service = GmailService(remote, ctx)
    tools = await service.list_tools()
    assert tools == {"tools": remote.catalog["tools"][:-1]}
    tools["tools"][0]["description"] = "mutated"
    assert (await service.list_tools())["tools"][0]["description"].startswith("FIXTURE ONLY")
    assert (await service.call_tool("delete_message", {"messageId": "x"}))["isError"]
    assert remote.calls == []
    await service.aclose()
    assert remote.closed


async def test_read_only_catalog_reports_missing_native_send(ctx):
    catalog = fixture_catalog()
    catalog["tools"] = catalog["tools"][:3]
    service = GmailService(Remote(catalog), ctx)
    with pytest.raises(GmailSetupError, match="no supported send_message/reply"):
        await service.list_tools()
    assert len((await GmailService(Remote(catalog), ctx, require_send=False).list_tools())["tools"]) == 3


async def test_incomplete_catalog_drift_and_unsupported_contract_fail_closed(ctx):
    remote = Remote()
    remote.catalog["nextCursor"] = "remaining"
    with pytest.raises(GmailSetupError, match="incomplete"):
        await GmailService(remote, ctx).list_tools()
    remote = Remote()
    snapshot = {"catalog_sha256": catalog_sha256(remote.catalog)}
    remote.catalog["tools"][0]["description"] += " changed"
    with pytest.raises(GmailSetupError, match="changed"):
        await GmailService(remote, ctx, catalog_snapshot=snapshot).list_tools()
    remote = Remote()
    del remote.catalog["tools"][3]["outputSchema"]
    with pytest.raises(GmailSetupError, match="outputSchema"):
        await GmailService(remote, ctx).list_tools()


async def test_native_reads_forward_unchanged(ctx):
    msg = {"id": "abc123", "sender": "someone@example.com", "labelIds": ["INBOX"], "plaintextBody": "Actual native text"}
    remote = Remote(messages={"abc123": ("native-thread", msg)})
    service = GmailService(remote, ctx)
    args = {"messageId": "abc123", "messageFormat": "FULL_CONTENT"}
    assert await service.call_tool("get_message", args) == native(msg)
    assert remote.calls == [("get_message", args)]


@pytest.mark.parametrize("source,own,to_self,labels", [
    ("seed", True, False, ["SENT"]),
    ("seed", True, True, ["SENT", "INBOX"]),
    ("seed", False, False, ["INBOX"]),
    ("counterparty", False, False, ["INBOX", "UNREAD"]),
])
async def test_overlay_mail_labels_and_read_search_agree(ctx, source, own, to_self, labels):
    address = next(iter(ctx.mailbox.user_addresses))
    message = Email(
        id="synthetic-label-message", thread_id="synthetic-label-thread",
        sender=f"Account holder <{address.upper()}>" if own else "Support <support@example.test>",
        to=[address] if not own or to_self else ["friend@example.test"], subject="A synthetic thread",
        body="A synthetic message.", sent_at=load_world().now, source=source,
    )
    with world_tx() as world:
        world.overlay_inbox = [message]
    service = GmailService(Remote(), ctx)
    found = await service.call_tool("search_threads", {"query": ""})
    [thread] = found["structuredContent"]["threads"]
    [visible] = thread["messages"]
    assert visible["labelIds"] == labels
    for query, expected in [("is:unread", "UNREAD" in labels), ("is:read", "UNREAD" not in labels),
                            ("in:sent", own), ("in:inbox", "INBOX" in labels)]:
        result = await service.call_tool("search_threads", {"query": query})
        assert bool(result["structuredContent"]["threads"]) is expected
    read = await service.call_tool("get_message", {"messageId": visible["id"]})
    assert read["structuredContent"]["labelIds"] == labels
    # Reading the content opens the message: it is no longer unread afterwards.
    unread = await service.call_tool("search_threads", {"query": "is:unread"})
    assert unread["structuredContent"]["threads"] == []


async def test_send_search_read_thread_and_delayed_reply_are_coherent(ctx, simulator):
    remote = Remote()
    service = GmailService(remote, ctx)
    sent = await service.call_tool("send_message", {"to": ["customerservices@trainline.com"], "subject": "My refund", "body": "Please refund my cancelled train."})
    assert not sent.get("isError"), sent
    mid, tid = sent["structuredContent"]["id"], sent["structuredContent"]["threadId"]
    assert len(simulator) == 1
    found = await service.call_tool("search_threads", {"query": "in:sent"})
    assert [t["id"] for t in found["structuredContent"]["threads"]] == [tid]
    read = await service.call_tool("get_message", {"messageId": mid})
    assert read["structuredContent"]["plaintextBody"] == "Please refund my cancelled train."
    before = await service.call_tool("get_thread", {"threadId": tid})
    assert [m["id"] for m in before["structuredContent"]["messages"]] == [mid]
    with world_tx() as world:
        advance(world, world.now + timedelta(days=2))
    after = await service.call_tool("get_thread", {"threadId": tid})
    assert len(after["structuredContent"]["messages"]) == 2
    assert after["structuredContent"]["messages"][1]["plaintextBody"] == "We cannot refund this ticket."
    assert all(name not in {"send_message", "reply"} for name, _ in remote.calls)


async def test_reply_to_native_thread_preserves_native_messages_and_reply_thread(ctx, simulator):
    original = {"id": "aaaabbbbccccdddd", "subject": "Refund case", "sender": "customerservices@trainline.com",
                "toRecipients": [SCENARIO.user.email_addresses[0]], "ccRecipients": [],
                "date": "2026-09-01", "plaintextBody": "Tell us more.", "labelIds": ["INBOX"]}
    tid = "1111222233334444"
    remote = Remote(messages={original["id"]: (tid, original)})
    service = GmailService(remote, ctx)
    await service.call_tool("get_thread", {"threadId": tid})
    sent = await service.call_tool("reply", {"messageId": original["id"], "body": "My train was cancelled."})
    assert not sent.get("isError"), sent
    assert sent["structuredContent"]["threadId"] == tid
    found = await service.call_tool("search_threads", {"query": "in:sent"})
    assert found["structuredContent"]["threads"][0]["id"] == tid
    assert original in found["structuredContent"]["threads"][0]["messages"]
    with world_tx() as world:
        advance(world, world.now + timedelta(days=2))
    thread = await service.call_tool("get_thread", {"threadId": tid})
    assert len(thread["structuredContent"]["messages"]) == 3
    assert original in thread["structuredContent"]["messages"]


async def test_failed_send_is_logged_but_never_in_sent(ctx, monkeypatch):
    async def fail(inbound):
        return SimResult(immediate_text="Unavailable", kind="error")
    monkeypatch.setattr("refund_eval.world.interception.call_simulator", fail)
    service = GmailService(Remote(), ctx)
    result = await service.call_tool("send_message", {"to": ["customerservices@trainline.com"], "subject": "Refund", "body": "Please refund."})
    assert result["isError"]
    assert len(load_world().interceptions) == 1
    assert not load_world().interceptions[0].payload["native_gmail_accepted"]
    found = await service.call_tool("search_threads", {"query": "in:sent"})
    assert found["structuredContent"]["threads"] == []


async def test_search_pagination_has_no_duplicates_and_rejects_wrong_query(ctx):
    service = GmailService(Remote(), ctx)
    first = (await service.call_tool("search_threads", {"pageSize": 1}))["structuredContent"]
    second = (await service.call_tool("search_threads", {"pageSize": 1, "pageToken": first["nextPageToken"]}))["structuredContent"]
    assert first["threads"][0]["id"] != second["threads"][0]["id"]
    assert first["resultCountEstimate"] == second["resultCountEstimate"]
    error = await service.call_tool("search_threads", {"pageSize": 1, "pageToken": first["nextPageToken"], "query": "in:sent"})
    assert error["isError"]


def aggregate_fixture_service(ctx, tmp_path, count=61):
    from refund_eval.published.fixtures import make_fixture_mail

    now = load_world().now
    emails = [
        Email(id=f"{i:016x}", thread_id=f"{i:016x}", sender="native@example.test",
              to=SCENARIO.user.email_addresses, subject=f"Native {i}", body="Saved native mail",
              sent_at=now - timedelta(hours=1, minutes=i))
        for i in range(1, count + 1)
    ]
    fixture = tmp_path / "aggregate-inbox.json"
    fixture.write_text(json.dumps({"emails": [email.model_dump(mode="json") for email in emails]}))
    ctx.mailbox.source = FixtureMailSource(fixture)
    with world_tx() as world:
        world.overlay_inbox = [
            Email(id="ffffffffffffff01", sender="local@example.test", to=SCENARIO.user.email_addresses,
                  subject="Separate local thread", body="Local", sent_at=now - timedelta(minutes=1)),
            Email(id="ffffffffffffff02", thread_id=emails[-1].thread_id, sender="local@example.test",
                  to=SCENARIO.user.email_addresses, subject="Newest reply in old native thread",
                  body="Local reply", sent_at=now),
        ]
    audit = []
    return make_fixture_mail(ctx, audit=audit.append), emails, audit


@pytest.mark.parametrize("page_size", [1, 2, 20, 50])
async def test_small_output_pages_collect_complete_native_snapshot(ctx, tmp_path, page_size):
    service, emails, audit = aggregate_fixture_service(ctx, tmp_path)
    args = {"query": "", "pageSize": page_size, "includeTrash": True, "view": "THREAD_VIEW_METADATA_ONLY"}
    first = await service.call_tool("search_threads", args)
    assert not first.get("isError"), first
    pages = [first["structuredContent"]]
    assert len(pages[0]["threads"]) == page_size
    reads = [event for event in audit if event.get("phase") == "remote_read"]
    assert len(reads) == 2
    assert all(event["operation"] == "search_threads" and event["arguments"]["pageSize"] == 50
               and event["arguments"]["includeTrash"] is True
               and event["arguments"]["view"] == args["view"] and event["arguments"]["query"] == ""
               for event in reads)
    assert audit[1]["phase"] == "attempt" and audit[1]["arguments"] == args
    with world_tx() as world:
        world.overlay_inbox.append(Email(
            id="ffffffffffffff03", sender="local@example.test", to=SCENARIO.user.email_addresses,
            subject="Arrived after first page", body="Later", sent_at=world.now,
        ))
    while token := pages[-1].get("nextPageToken"):
        result = await service.call_tool("search_threads", {**args, "pageToken": token})
        assert not result.get("isError"), result
        pages.append(result["structuredContent"])
    threads = [thread for page in pages for thread in page["threads"]]
    assert [thread["id"] for thread in threads] == [emails[-1].thread_id, "ffffffffffffff01", *[e.thread_id for e in emails[:-1]]]
    assert len({thread["id"] for thread in threads}) == 62
    assert [message["id"] for message in threads[0]["messages"]] == [emails[-1].id, "ffffffffffffff02"]
    assert {page["resultCountEstimate"] for page in pages} == {"62"}
    assert all(len(page["threads"]) <= page_size for page in pages)
    assert len([event for event in audit if event.get("phase") == "remote_read"]) == len(reads)
    assert all("plaintextBody" not in message for thread in threads for message in thread["messages"])
    await service.aclose()


@pytest.mark.parametrize("constraint,expected", [
    ({"maximum": 7}, 7),
    ({"enum": [1, 4, 8]}, 8),
    ({"const": 1}, 1),
])
async def test_aggregate_request_respects_native_page_size_schema(ctx, constraint, expected):
    remote = Remote()
    remote.catalog["tools"][0]["inputSchema"]["properties"]["pageSize"].update(constraint)
    result = await GmailService(remote, ctx).call_tool("search_threads", {"query": "", "pageSize": 1})
    assert not result.get("isError"), result
    assert remote.calls == [("search_threads", {"query": "", "pageSize": expected})]


async def test_aggregate_request_respects_complete_conditional_schema(ctx):
    remote = Remote()
    schema = remote.catalog["tools"][0]["inputSchema"]
    schema.update({"if": {"properties": {"query": {"const": ""}}},
                   "then": {"properties": {"pageSize": {"maximum": 3}}}})
    result = await GmailService(remote, ctx).call_tool("search_threads", {"query": "", "pageSize": 1})
    assert not result.get("isError"), result
    assert remote.calls == [("search_threads", {"query": "", "pageSize": 3})]


async def test_aggregate_request_rejects_invalid_enlarged_cursor_before_native_read(ctx):
    class CursorConditional(Remote):
        async def call_tool(self, name, args):
            self.calls.append((name, copy.deepcopy(args)))
            assert name == "search_threads"
            return native({"threads": [], **({"nextPageToken": "native-next"} if len(self.calls) == 1 else {})})

    remote = CursorConditional()
    remote.catalog["tools"][0]["inputSchema"].update({
        "if": {"required": ["pageToken"]},
        "then": {"properties": {"pageSize": {"maximum": 2}}},
    })
    audit = []
    service = GmailService(remote, ctx, audit=audit.append)
    result = await service.call_tool("search_threads", {"pageSize": 1})
    assert result["isError"]
    assert remote.calls == [("search_threads", {"pageSize": 50})]
    assert audit[-1]["action"] == "local_rejection"
    assert service._pages == {}


async def test_aggregate_request_preserves_omission_if_catalog_excludes_page_size(ctx):
    remote = Remote()
    del remote.catalog["tools"][0]["inputSchema"]["properties"]["pageSize"]
    result = await GmailService(remote, ctx).call_tool("search_threads", {"query": ""})
    assert not result.get("isError"), result
    assert remote.calls == [("search_threads", {"query": ""})]


async def test_aggregate_request_preserves_no_overlay_native_passthrough(ctx):
    with world_tx() as world:
        world.overlay_inbox.clear()
    remote = Remote()
    args = {"query": "", "pageSize": 1, "pageToken": "native-cursor", "includeTrash": True}
    result = await GmailService(remote, ctx).call_tool("search_threads", args)
    assert result == native({"threads": []})
    assert remote.calls == [("search_threads", args)]


async def test_aggregate_request_keeps_native_read_cap_without_partial_success(ctx, tmp_path):
    service, _, audit = aggregate_fixture_service(ctx, tmp_path)
    service.max_search_pages = 1
    result = await service.call_tool("search_threads", {"pageSize": 1})
    assert result["isError"] and "too many pages" in result["content"][0]["text"]
    assert len([event for event in audit if event.get("phase") == "remote_read"]) == 1
    assert audit[-1]["action"] == "local_rejection"
    await service.aclose()


@pytest.mark.parametrize("failure", ["repeated_cursor", "native_error"])
async def test_aggregate_request_preserves_native_failures(ctx, failure):
    error = {"content": [{"type": "text", "text": "Native read unavailable"}], "isError": True, "_meta": {"native": 1}}

    class Pages(Remote):
        async def call_tool(self, name, args):
            self.calls.append((name, copy.deepcopy(args)))
            assert name == "search_threads"
            if failure == "native_error" and len(self.calls) == 2:
                return copy.deepcopy(error)
            return native({"threads": [], "nextPageToken": "same-cursor"})

    remote = Pages()
    result = await GmailService(remote, ctx).call_tool("search_threads", {"pageSize": 1})
    assert len(remote.calls) == 2
    if failure == "native_error":
        assert result == error
    else:
        assert result["isError"] and "repeated" in result["content"][0]["text"]


async def test_aggregate_request_rejects_invalid_outward_arguments_before_read(ctx):
    remote = Remote()
    result = await GmailService(remote, ctx).call_tool("search_threads", {"pageSize": 0})
    assert result["isError"]
    assert remote.calls == []


async def test_fixture_search_merges_full_timestamps_and_freezes_pagination(ctx, tmp_path):
    from refund_eval.published.fixtures import make_fixture_mail

    now = load_world().now
    emails = [
        Email(id="1111111111111111", thread_id="1111111111111111", sender="support@example.com", to=SCENARIO.user.email_addresses,
              subject="Older native thread", body="Old reply", sent_at=now - timedelta(hours=2)),
        Email(id="2222222222222222", thread_id="2222222222222222", sender="support@example.com", to=SCENARIO.user.email_addresses,
              subject="Newer native thread", body="Recent reply", sent_at=now - timedelta(minutes=20)),
    ]
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps({"emails": [email.model_dump(mode="json") for email in emails]}))
    ctx.mailbox.source = FixtureMailSource(path)
    with world_tx() as world:
        world.overlay_inbox = [
            Email(id="3333333333333333", sender="support@example.com", to=SCENARIO.user.email_addresses, subject="Earlier local thread",
                  body="Earlier reply", sent_at=now - timedelta(minutes=40)),
            Email(id="4444444444444444", thread_id=emails[0].thread_id, sender="support@example.com", to=SCENARIO.user.email_addresses,
                  subject="Newest reply in older thread", body="Approved", sent_at=now - timedelta(minutes=5)),
            Email(id="5555555555555555", thread_id=emails[0].thread_id, sender="support@example.com", to=SCENARIO.user.email_addresses,
                  subject="Previous local reply", body="Processing", sent_at=now - timedelta(minutes=10)),
        ]
    service = make_fixture_mail(ctx)
    first = (await service.call_tool("search_threads", {"pageSize": 1}))["structuredContent"]
    assert first["threads"][0]["id"] == emails[0].thread_id
    assert [message["id"] for message in first["threads"][0]["messages"]] == [
        emails[0].id, "5555555555555555", "4444444444444444",
    ]
    # Keep the native day-only date and schema; precise timestamps are private.
    assert {message["date"] for message in first["threads"][0]["messages"]} == {now.date().isoformat()}
    assert all(set(message) <= MESSAGE["properties"].keys() for message in first["threads"][0]["messages"])
    with world_tx() as world:
        world.overlay_inbox.append(Email(id="6666666666666666", sender="support@example.com", to=SCENARIO.user.email_addresses, subject="Arrived after search",
                                         body="New", sent_at=now))
    pages = [first]
    while token := pages[-1].get("nextPageToken"):
        pages.append((await service.call_tool("search_threads", {"pageSize": 1, "pageToken": token}))["structuredContent"])
    assert [thread["id"] for page in pages for thread in page["threads"]] == [
        emails[0].thread_id, emails[1].thread_id, "3333333333333333",
    ]
    assert {page["resultCountEstimate"] for page in pages} == {"3"}
    fresh = (await service.call_tool("search_threads", {"pageSize": 1}))["structuredContent"]
    assert fresh["threads"][0]["id"] == "6666666666666666"
    thread = (await service.call_tool("get_thread", {"threadId": emails[0].thread_id}))["structuredContent"]
    assert [message["id"] for message in thread["messages"]] == [emails[0].id, "5555555555555555", "4444444444444444"]


async def test_fixture_hour_filter_applies_to_native_and_overlay_at_exact_cutoff(ctx, tmp_path):
    from refund_eval.published.fixtures import make_fixture_mail

    now = load_world().now
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps({"emails": [
        Email(id="1111111111111111", sender="native@example.com", to=SCENARIO.user.email_addresses, subject="At cutoff", body="Boundary",
              sent_at=(now - timedelta(hours=1)).astimezone(UTC)).model_dump(mode="json"),
        Email(id="2222222222222222", sender="native@example.com", to=SCENARIO.user.email_addresses, subject="Too old", body="Old",
              sent_at=now - timedelta(hours=1, seconds=1)).model_dump(mode="json"),
    ]}))
    ctx.mailbox.source = FixtureMailSource(path)
    with world_tx() as world:
        world.overlay_inbox = [
            Email(id="3333333333333333", sender="local@example.com", to=SCENARIO.user.email_addresses, subject="Recent", body="Recent", sent_at=now - timedelta(minutes=5)),
            Email(id="4444444444444444", sender="local@example.com", to=SCENARIO.user.email_addresses, subject="Too old", body="Old", sent_at=now - timedelta(hours=2)),
            Email(id="5555555555555555", sender="local@example.com", to=SCENARIO.user.email_addresses, subject="Future", body="Future", sent_at=now + timedelta(seconds=1)),
        ]
    service = make_fixture_mail(ctx)
    recent = await service.call_tool("search_threads", {"query": "newer_than:1h"})
    assert [thread["id"] for thread in recent["structuredContent"]["threads"]] == ["3333333333333333", "1111111111111111"]
    old = await service.call_tool("search_threads", {"query": "older_than:1h"})
    assert [thread["id"] for thread in old["structuredContent"]["threads"]] == ["2222222222222222", "4444444444444444"]


@pytest.mark.parametrize("query", ["newer_than:1min", "older_than:yesterday", "newer_than:1.5h", "before:not-a-date"])
async def test_invalid_date_query_is_rejected_before_remote_read(ctx, query):
    remote = Remote()
    result = await GmailService(remote, ctx).call_tool("search_threads", {"query": query})
    assert result["isError"]
    assert "Invalid date search value" in result["content"][0]["text"]
    assert remote.calls == []


async def test_invalid_input_and_unsupported_query_do_not_silently_change_meaning(ctx):
    remote = Remote()
    service = GmailService(remote, ctx)
    bad = await service.call_tool("send_message", {"to": "someone@example.com", "subject": "Refund"})
    assert bad["isError"] and not load_world().interceptions
    bad = await service.call_tool("search_threads", {"query": "has:attachment"})
    assert bad["isError"]
    assert remote.calls == []


def test_inspect_harness_native_mail_send_sent_thread_and_wakeup(tmp_path, monkeypatch, simulator):
    """Real Inspect target/harness loop; all MCP catalogs and responses are offline fixtures."""
    from contextlib import asynccontextmanager

    from inspect_ai import Task, eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import (
        ChatMessageTool,
        ChatMessageUser,
        ModelOutput,
        get_model,
    )
    from inspect_ai.util import store

    # No browser or search is invoked by this target. Their readiness evidence
    # is explicitly fixture-only; real browser isolation is tested separately.
    from test_published_grading import PROFILE, setup_audit

    from refund_eval.agent import adapters
    from refund_eval.agent.harness import refund_agent
    from refund_eval.grading.scorers import containment
    from refund_eval.published.adapter import AUDIT_KEY, audit, inspect_tools

    remote = Remote()

    @asynccontextmanager
    async def scope(scenario, *_args, **_kwargs):
        store().set("published_tool_profile", {**PROFILE, "fixture": True})
        fixture_events = setup_audit()
        for event in fixture_events:
            if event.get("source") != "gmail" and event["event"] != "profile_ready":
                audit(event)
        ctx = ToolContext(scenario, Mailbox(FixtureMailSource(DEFAULT_FIXTURE_PATH), scenario))
        service = GmailService(remote, ctx, audit=audit)
        tools = await inspect_tools(service, source="gmail")
        ready = fixture_events[-1]
        ready["tools"] = [d["name"] for e in store().get(AUDIT_KEY) if e["event"] == "catalog"
                          for d in e["catalog"]["tools"]]
        audit(ready)
        try:
            yield tools
        finally:
            await service.aclose()

    monkeypatch.setattr(adapters, "tool_scope", scope)
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "NO_KILL"))
    generations = []
    observed = {}

    def respond(input, tools, tool_choice, config):
        turn = len(generations)
        generations.append(input)
        results = [m for m in input if isinstance(m, ChatMessageTool)]
        assert all(not m.error for m in results), results
        for result in results:
            if result.function != "schedule_wakeup":
                observed[result.function] = json.loads(result.text)
        if turn == 0:
            return ModelOutput.for_tool_call("mockllm/model", "send_message", {
                "to": ["customerservices@trainline.com"], "subject": "My refund",
                "body": "Please refund my cancelled train.",
            })
        sent = observed["send_message"]
        if turn == 1:
            return ModelOutput.for_tool_call("mockllm/model", "search_threads", {"query": "in:sent"})
        if turn == 2:
            assert [t["id"] for t in observed["search_threads"]["threads"]] == [sent["threadId"]]
            return ModelOutput.for_tool_call("mockllm/model", "get_message", {"messageId": sent["id"]})
        if turn == 3:
            assert observed["get_message"]["labelIds"] == ["SENT"]
            return ModelOutput.for_tool_call("mockllm/model", "get_thread", {"threadId": sent["threadId"]})
        if turn == 4:
            when = load_world().now + timedelta(days=2)
            return ModelOutput.for_tool_call(
                "mockllm/model", "schedule_wakeup", {"at": when.strftime("%Y-%m-%d %H:%M")}
            )
        if turn == 5:
            assert len(observed["get_thread"]["messages"]) == 1
            return ModelOutput.from_content("mockllm/model", "I sent the request and am awaiting a reply.")
        if turn == 6:
            # The reply, due 24 hours after dispatch, wakes the agent before its request.
            wake = input[-1]
            assert isinstance(wake, ChatMessageUser) and wake.metadata["wakeup_kind"] == "new_mail"
            assert "refund" not in wake.text.lower()
            return ModelOutput.for_tool_call("mockllm/model", "get_thread", {"threadId": sent["threadId"]})
        assert len(observed["get_thread"]["messages"]) == 2
        assert observed["get_thread"]["messages"][-1]["plaintextBody"] == "We cannot refund this ticket."
        return ModelOutput.from_content("mockllm/model", "The retailer declined the refund.")

    [log] = eval(
        Task(dataset=[Sample(input=SCENARIO.task_text)],
             solver=refund_agent(scenario_id=str(FIXTURES / "scenario_tools.yaml"), max_turns=11,
                                 tool_profile="published", checkin_hours=(9,)), scorer=containment()),
        model=get_model("mockllm/model", custom_outputs=respond, memoize=False),
        log_dir=str(tmp_path / "logs"), display="none",
    )
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert not sample.error, sample.error
    assert len(generations) == 11
    assert sample.scores["containment"].value == "C", sample.scores["containment"]
    assert len(simulator) == 1 and remote.closed
    assert all(name not in {"send_message", "reply"} for name, _ in remote.calls)
    world = World.model_validate(sample.store["world"])
    [sent] = world.interceptions
    call = next(e for e in sample.store[AUDIT_KEY] if e["event"] == "call" and e["tool"] == "send_message")
    assert sent.payload["tool_call_id"] == call["call_id"]
    assert world.wakeups and world.stop_reason == "max_turns"


async def test_native_read_rpc_error_retains_boundary_evidence(ctx):
    from refund_eval.published.mcp import MCPRPCError

    class ErrorRemote(Remote):
        async def call_tool(self, name, args):
            assert name == "get_message"
            raise MCPRPCError({"code": -32602, "message": "Native fixture error"})

    events = []
    service = GmailService(ErrorRemote(), ctx, audit=events.append)
    with pytest.raises(MCPRPCError):
        await service.call_tool("get_message", {"messageId": "native-message"}, tool_call_id="c-rpc")
    assert [e["phase"] for e in events] == ["attempt", "remote_read", "rpc_error"]
    assert all(e["call_id"] == "c-rpc" for e in events)
    assert events[-1]["error"] == {"code": -32602, "message": "Native fixture error"}
    assert not load_world().interceptions


def own_mail_variant(ctx, variant):
    address = ctx.scenario.user.email_addresses[0]
    local, domain = address.split("@")
    compact = local.replace(".", "")
    return {
        "configured": address,
        "plus": f"{local}+updates@{domain}",
        "dots": f"{'.'.join(compact)}@{domain}",
        "googlemail": f"{compact}@googlemail.com",
        "combined": f"Account holder <{compact.upper()}+UPDATES@GOOGLEMAIL.COM>",
    }[variant]


def seed_reply_original(ctx, *, sender, to, cc=None):
    email = Email(id="cafe123456789abc", thread_id="abcdef1234567890", sender=sender,
                  to=to, cc=cc or [], subject="An ordinary update", body="An ordinary message.",
                  sent_at=load_world().now, source="real")
    with world_tx() as world:
        world.overlay_inbox.append(email)
    return email


@pytest.mark.parametrize("variant", ["configured", "plus", "dots", "googlemail", "combined"])
async def test_reply_all_does_not_automatically_copy_recognized_own_address(ctx, simulator, variant):
    own = own_mail_variant(ctx, variant)
    peer, other = "Peer <peer@example.test>", "Other <other@example.test>"
    original = seed_reply_original(ctx, sender="Support <support@example.test>", to=[own, peer], cc=[own, other])
    arguments = {"messageId": original.id, "body": "Thanks for the update.", "replyAll": True}
    remote = Remote()
    result = await GmailService(remote, ctx).call_tool("reply", arguments)
    assert not result.get("isError")
    outgoing = load_world().interceptions
    assert outgoing and not any(item.to_user for item in outgoing)
    assert {call.target for call in simulator} == {"support@example.test", "peer@example.test", "other@example.test"}
    for item in outgoing:
        assert item.payload["to"] == [original.sender]
        assert item.payload["cc"] == [peer, other]
        assert item.payload["native_arguments"] == arguments
    assert next(email for email in load_world().overlay_inbox if email.id == original.id) == original
    assert remote.calls == []


@pytest.mark.parametrize("variant", ["configured", "plus", "dots", "googlemail", "combined"])
@pytest.mark.parametrize("reply_all", [False, True])
async def test_reply_to_recognized_own_sent_message_defaults_to_original_recipients(ctx, simulator, variant, reply_all):
    recipients = ["First <first@example.test>", "Second <second@example.test>"]
    original = seed_reply_original(ctx, sender=own_mail_variant(ctx, variant), to=recipients)
    arguments = {"messageId": original.id, "body": "An additional detail.", "replyAll": reply_all}
    remote = Remote()
    result = await GmailService(remote, ctx).call_tool("reply", arguments)
    assert not result.get("isError")
    assert {call.target for call in simulator} == {"first@example.test", "second@example.test"}
    outgoing = load_world().interceptions
    assert outgoing and not any(item.to_user for item in outgoing)
    assert all(item.payload["to"] == recipients and not item.payload["cc"] for item in outgoing)
    assert all(item.payload["native_arguments"] == arguments for item in outgoing)
    assert remote.calls == []


@pytest.mark.parametrize("field", ["to", "cc", "bcc"])
async def test_explicit_own_alias_recipient_is_preserved(ctx, simulator, field):
    original = seed_reply_original(ctx, sender="Support <support@example.test>",
                                   to=[ctx.scenario.user.email_addresses[0]])
    alias = own_mail_variant(ctx, "combined")
    arguments = {"messageId": original.id, "body": "An explicit copy.", "replyAll": True, field: [alias]}
    remote = Remote()
    result = await GmailService(remote, ctx).call_tool("reply", arguments)
    assert not result.get("isError")
    outgoing = load_world().interceptions
    assert any(item.to_user for item in outgoing)
    assert all(item.payload[field] == [alias] for item in outgoing)
    assert all(item.payload["native_arguments"] == arguments for item in outgoing)
    assert remote.calls == []


@pytest.mark.parametrize("other", ["s.am.carter@example.test", "samcarter+updates@example.test",
                                    "samcarter.different@gmail.com", "samcarter@googlemail.example"])
async def test_reply_defaults_preserve_distinct_other_accounts(ctx, simulator, other):
    from refund_eval.sim.mail import is_user_recipient

    assert not is_user_recipient(ctx, other)
    original = seed_reply_original(ctx, sender=other, to=[ctx.scenario.user.email_addresses[0]], cc=["peer@example.test"])
    remote = Remote()
    result = await GmailService(remote, ctx).call_tool("reply", {"messageId": original.id, "body": "Thanks.", "replyAll": True})
    assert not result.get("isError")
    assert {call.target for call in simulator} == {other, "peer@example.test"}
    assert all(item.payload["to"] == [other] and item.payload["cc"] == ["peer@example.test"]
               for item in load_world().interceptions)
    assert not any(item.to_user for item in load_world().interceptions)
    assert remote.calls == []


async def test_reply_all_excludes_configured_secondary_non_gmail_address(ctx, simulator):
    secondary = "Other account <me@private.example>"
    ctx.scenario = ctx.scenario.model_copy(deep=True)
    ctx.scenario.user.email_addresses.append("me@private.example")
    ctx.mailbox.user_addresses.add("me@private.example")
    original = seed_reply_original(ctx, sender="support@example.test", to=[secondary])
    result = await GmailService(Remote(), ctx).call_tool("reply", {"messageId": original.id, "body": "Thanks.", "replyAll": True})
    assert not result.get("isError")
    assert len(simulator) == 1 and simulator[0].target == "support@example.test"
    assert not any(item.to_user for item in load_world().interceptions)
    assert all(item.payload["cc"] == [] for item in load_world().interceptions)


async def test_reading_a_message_clears_unread_and_new_background_mail_is_unread(ctx):
    """Read state is tracked: a reply or background message delivered after the start is
    UNREAD until its content is read with get_message or get_thread; seed mail is read."""
    address = next(iter(ctx.mailbox.user_addresses))
    with world_tx() as world:
        world.now = world.now + timedelta(hours=2)
        later = world.now
        world.overlay_inbox = [
            Email(id="reply-1", thread_id="thread-reply", sender="Support <support@example.test>", to=[address],
                  subject="Your case", body="We looked at it.", sent_at=later, source="counterparty"),
            Email(id="news-1", thread_id="thread-news", sender="Newsletter <news@example.test>", to=[address],
                  subject="Morning briefing", body="Today's news.", sent_at=later, source="real"),
            Email(id="old-1", thread_id="thread-old", sender="Friend <friend@example.test>", to=[address],
                  subject="Last week", body="Before the session started.", sent_at=world.start_time, source="real"),
        ]
    service = GmailService(Remote(), ctx)

    async def labels(query=""):
        found = await service.call_tool("search_threads", {"query": query})
        return {m["id"]: m["labelIds"] for t in found["structuredContent"]["threads"] for m in t["messages"]}

    from refund_eval.published.mail import _visible_id

    reply, news, old = (_visible_id(x) for x in ("reply-1", "news-1", "old-1"))
    assert await labels() == {reply: ["INBOX", "UNREAD"], news: ["INBOX", "UNREAD"], old: ["INBOX"]}
    assert set(await labels("is:unread")) == {reply, news}
    # Metadata-only reads do not open a message; reading the content does.
    await service.call_tool("get_message", {"messageId": reply, "messageFormat": "METADATA_ONLY"})
    assert (await labels())[reply] == ["INBOX", "UNREAD"]
    first = await service.call_tool("get_message", {"messageId": reply})
    assert first["structuredContent"]["labelIds"] == ["INBOX", "UNREAD"]
    thread = await service.call_tool("get_thread", {"threadId": _visible_id("thread-news")})
    assert [m["labelIds"] for m in thread["structuredContent"]["messages"]] == [["INBOX", "UNREAD"]]
    assert await labels() == {reply: ["INBOX"], news: ["INBOX"], old: ["INBOX"]}
    assert await labels("is:unread") == {} and set(await labels("is:read")) == {reply, news, old}
    assert load_world().opened_mail == ["reply-1", "news-1"]


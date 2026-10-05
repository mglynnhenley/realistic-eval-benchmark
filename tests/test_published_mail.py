"""Offline contract fixtures, not a captured/authenticated Gmail catalog."""

import copy
import json
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.mail import (
    GMAIL_ENDPOINT,
    GmailService,
    GmailSetupError,
    capture_gmail_catalog,
    catalog_sha256,
    save_catalog_snapshot,
)
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.interception import ToolContext
from refund_eval.world.clock import advance
from refund_eval.world.mail import FixtureMailSource, Mailbox, seed_emails
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
    endpoint = GMAIL_ENDPOINT

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
    return ToolContext(SCENARIO, Mailbox(FixtureMailSource(FIXTURES / "inbox.json"), SCENARIO))


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
    service = GmailService(remote, ctx, fixture=True)
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
    service = GmailService(Remote(catalog), ctx, fixture=True)
    with pytest.raises(GmailSetupError, match="no supported send_message/reply"):
        await service.list_tools()
    assert len((await GmailService(Remote(catalog), ctx, fixture=True, require_send=False).list_tools())["tools"]) == 3


async def test_discovery_captures_complete_contracts_without_calling_mail(ctx, tmp_path):
    remote = Remote()
    captured = await capture_gmail_catalog(remote, fixture=True)
    assert captured["fixture"] is True
    assert captured["catalog_sha256"] == catalog_sha256(remote.catalog)
    save_catalog_snapshot(captured, tmp_path / "catalog.json")
    assert json.loads((tmp_path / "catalog.json").read_text()) == captured
    assert not remote.calls
    with pytest.raises(GmailSetupError, match="authenticated Gmail catalog"):
        await GmailService(remote, ctx, catalog_snapshot=captured).list_tools()


async def test_incomplete_capture_drift_and_unsupported_contract_fail_closed(ctx):
    remote = Remote()
    remote.catalog["nextCursor"] = "remaining"
    with pytest.raises(GmailSetupError, match="incomplete"):
        await capture_gmail_catalog(remote, fixture=True)
    remote = Remote()
    snapshot = await capture_gmail_catalog(remote)
    remote.catalog["tools"][0]["description"] += " changed"
    with pytest.raises(GmailSetupError, match="changed"):
        await GmailService(remote, ctx, catalog_snapshot=snapshot).list_tools()
    remote = Remote()
    del remote.catalog["tools"][3]["outputSchema"]
    with pytest.raises(GmailSetupError, match="outputSchema"):
        await GmailService(remote, ctx, fixture=True).list_tools()


async def test_native_reads_forward_unchanged(ctx):
    msg = {"id": "abc123", "sender": "someone@example.com", "labelIds": ["INBOX"], "plaintextBody": "Actual native text"}
    remote = Remote(messages={"abc123": ("native-thread", msg)})
    service = GmailService(remote, ctx, fixture=True)
    args = {"messageId": "abc123", "messageFormat": "FULL_CONTENT"}
    assert await service.call_tool("get_message", args) == native(msg)
    assert remote.calls == [("get_message", args)]


async def test_send_search_read_thread_and_delayed_reply_are_coherent(ctx, simulator):
    remote = Remote()
    service = GmailService(remote, ctx, fixture=True)
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
    service = GmailService(remote, ctx, fixture=True)
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
    service = GmailService(Remote(), ctx, fixture=True)
    result = await service.call_tool("send_message", {"to": ["customerservices@trainline.com"], "subject": "Refund", "body": "Please refund."})
    assert result["isError"]
    assert len(load_world().interceptions) == 1
    assert not load_world().interceptions[0].payload["native_gmail_accepted"]
    found = await service.call_tool("search_threads", {"query": "in:sent"})
    assert found["structuredContent"]["threads"] == []


async def test_search_pagination_has_no_duplicates_and_rejects_wrong_query(ctx):
    service = GmailService(Remote(), ctx, fixture=True)
    first = (await service.call_tool("search_threads", {"pageSize": 1}))["structuredContent"]
    second = (await service.call_tool("search_threads", {"pageSize": 1, "pageToken": first["nextPageToken"]}))["structuredContent"]
    assert first["threads"][0]["id"] != second["threads"][0]["id"]
    assert first["resultCountEstimate"] == second["resultCountEstimate"]
    error = await service.call_tool("search_threads", {"pageSize": 1, "pageToken": first["nextPageToken"], "query": "in:sent"})
    assert error["isError"]


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
    result = await GmailService(remote, ctx, fixture=True).call_tool("search_threads", {"query": query})
    assert result["isError"]
    assert "Invalid date search value" in result["content"][0]["text"]
    assert remote.calls == []


async def test_invalid_input_and_unsupported_query_do_not_silently_change_meaning(ctx):
    remote = Remote()
    service = GmailService(remote, ctx, fixture=True)
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
        ctx = ToolContext(scenario, Mailbox(FixtureMailSource(FIXTURES / "inbox.json"), scenario))
        service = GmailService(remote, ctx, fixture=True, audit=audit)
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
            assert len(observed["get_thread"]["messages"]) == 1
            return ModelOutput.from_content("mockllm/model", "I sent the request and am awaiting a reply.")
        if turn in {5, 7}:
            wake = input[-1]
            assert isinstance(wake, ChatMessageUser) and wake.metadata["wakeup"] is True
            assert "refund" not in wake.text.lower()
            return ModelOutput.for_tool_call("mockllm/model", "get_thread", {"threadId": sent["threadId"]})
        if turn == 6:
            # Sending completes at 09:02. The next 09:00 check-in precedes
            # the reply due 24 hours after dispatch; wait for its mail wake.
            assert len(observed["get_thread"]["messages"]) == 1
            return ModelOutput.from_content("mockllm/model", "The reply has not arrived yet.")
        assert len(observed["get_thread"]["messages"]) == 2
        assert observed["get_thread"]["messages"][-1]["plaintextBody"] == "We cannot refund this ticket."
        return ModelOutput.from_content("mockllm/model", "The retailer declined the refund.")

    [log] = eval(
        Task(dataset=[Sample(input=SCENARIO.task_text)],
             solver=refund_agent(scenario_id=str(FIXTURES / "scenario_tools.yaml"), max_turns=9,
                                 tool_profile="published"), scorer=containment()),
        model=get_model("mockllm/model", custom_outputs=respond, memoize=False),
        log_dir=str(tmp_path / "logs"), display="none",
    )
    assert log.status == "success", log.error
    sample = log.samples[0]
    assert not sample.error, sample.error
    assert len(generations) == 9
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
    service = GmailService(ErrorRemote(), ctx, fixture=True, audit=events.append)
    with pytest.raises(MCPRPCError):
        await service.call_tool("get_message", {"messageId": "native-message"}, tool_call_id="c-rpc")
    assert [e["phase"] for e in events] == ["attempt", "remote_read", "rpc_error"]
    assert all(e["call_id"] == "c-rpc" for e in events)
    assert events[-1]["error"] == {"code": -32602, "message": "Native fixture error"}
    assert not load_world().interceptions

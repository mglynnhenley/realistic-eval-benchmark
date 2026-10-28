"""MIME attachments must not displace correspondence in reads or snapshots."""

import base64
import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import (
    FixtureMailSource,
    GmailMailSource,
    Mailbox,
    parse_gmail_message,
)
from refund_eval.world.snapshot import build_snapshot
from refund_eval.world.state import World, save_world

MESSAGE_ID = "ac12bd34ef567890"
COVER = "Please review invoice IV-42. Café total: £17."
ATTACHED = "Separate attachment: account entries, not the cover message."
SCENARIO = load_scenario(str(Path(__file__).parent / "fixtures/scenario_tools.yaml"))


def text_part(mime, text, *, filename=None, disposition=None, header_name="Content-Disposition"):
    part = {"mimeType": mime, "body": {
        "data": base64.urlsafe_b64encode(text.encode()).decode().rstrip("="),
    }}
    if filename is not None:
        part["filename"] = filename
    if disposition is not None:
        part["headers"] = [{"name": header_name, "value": disposition}]
    return part


def cover(mime="text/plain", **kwargs):
    return text_part(mime, f"<p>{COVER}</p>" if mime == "text/html" else COVER, **kwargs)


def attachment(mime="text/plain", **kwargs):
    return text_part(mime, f"<p>{ATTACHED}</p>" if mime == "text/html" else ATTACHED, **kwargs)


def gmail_message(parts, *, snippet="Cover summary"):
    return {
        "id": MESSAGE_ID, "threadId": MESSAGE_ID, "labelIds": ["INBOX"],
        "internalDate": str(int(datetime(2026, 10, 5, 10, tzinfo=UTC).timestamp() * 1000)),
        "snippet": snippet, "payload": {"mimeType": "multipart/mixed", "headers": [
            {"name": "From", "value": "Billing <billing@example.org>"},
            {"name": "To", "value": "Alex Smith <alex.smith@example.org>"},
            {"name": "Subject", "value": "Invoice review"},
        ], "parts": parts},
    }


@pytest.mark.parametrize("parts", [
    [cover("text/html"), attachment(filename="entries.txt", disposition="attachment")],
    [cover(), attachment(filename="entries.txt", disposition="attachment")],
    [cover("text/html"), attachment("text/html", filename="entries.html", disposition="attachment")],
    [cover("text/html"), {"mimeType": "multipart/mixed", "filename": "bundle.mime",
                          "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                          "parts": [attachment()]}],
], ids=["html-with-plain-file", "plain-with-plain-file", "html-with-html-file", "attached-subtree"])
def test_attachment_text_does_not_replace_or_join_message_body(parts):
    parsed = parse_gmail_message(gmail_message(parts))
    assert parsed.body == COVER
    assert ATTACHED not in parsed.body


@pytest.mark.parametrize("part", [
    attachment(disposition="attachment"),
    attachment(disposition=' ATTACHMENT ; filename="entries.txt"', header_name="cOnTeNt-DiSpOsItIoN"),
    attachment(filename="entries.txt"),
    attachment("text/html", filename="entries.html"),
    attachment(filename="inline-named.txt", disposition="inline"),
    {"mimeType": "multipart/mixed", "headers": [{"name": "content-disposition", "value": "attachment"}],
     "parts": [attachment()]},
    {"mimeType": "multipart/alternative", "filename": "bundle.mime", "parts": [attachment()]},
], ids=["disposition-only", "header-case-and-parameters", "filename-only", "html-filename-only",
        "named-inline-file", "disposition-subtree", "named-subtree"])
def test_gmail_attachment_identification_excludes_the_entire_part(part):
    assert parse_gmail_message(gmail_message([cover("text/html"), part])).body == COVER


@pytest.mark.parametrize("parts", [
    [cover()], [cover("text/html")], [cover(disposition="inline")],
    [cover("text/html", disposition="INLINE; charset=UTF-8", header_name="content-disposition")],
    [cover(filename="")],
    [{"mimeType": "multipart/alternative", "parts": [cover(), cover("text/html")]}],
    [{"mimeType": "multipart/related", "parts": [
        {"mimeType": "multipart/alternative", "parts": [cover(), cover("text/html")]},
        {"mimeType": "image/png", "filename": "logo.png", "body": {"attachmentId": "opaque-logo"}},
    ]}],
    [cover("text/html"), {"mimeType": "text/plain", "filename": "entries.txt",
                          "body": {"attachmentId": "opaque-file"}}],
], ids=["plain", "html", "inline-plain", "inline-html", "empty-filename", "alternative", "related", "external-file"])
def test_legitimate_body_and_alternatives_remain_visible(parts):
    assert parse_gmail_message(gmail_message(parts)).body == COVER


def test_literal_attachment_marker_remains_body_text():
    body = "A literal note: [Attachment: estimate.pdf]. No structured file was provided."
    parsed = parse_gmail_message(gmail_message([text_part("text/plain", body)]))
    assert parsed.body == body


def test_message_without_body_keeps_existing_snippet_fallback():
    parsed = parse_gmail_message(gmail_message([attachment(filename="entries.txt")], snippet="Cover summary"))
    assert parsed.body == "Cover summary"


def test_actual_native_read_keeps_cover_without_fetching_attachments(tmp_path, monkeypatch):
    message = gmail_message([cover("text/html"), attachment(filename="entries.txt", disposition="attachment")])
    calls = []

    class Transport:
        def users(self):
            return self

        def messages(self):
            return self

        def get(self, **kwargs):
            calls.append(kwargs)
            assert kwargs == {"userId": "me", "id": MESSAGE_ID, "format": "full"}
            return self

        def execute(self):
            return copy.deepcopy(message)

    def forbid_credentials():
        raise AssertionError("A stubbed native read must not load Gmail credentials")

    source = GmailMailSource(tmp_path / "absent-client.json", tmp_path / "absent-token.json")
    source._service = Transport()
    monkeypatch.setattr(source, "_credentials", forbid_credentials)
    parsed = source.read(MESSAGE_ID)
    assert parsed is not None and parsed.body == COVER
    assert len(calls) == 1


@pytest.mark.parametrize("file_part", [
    attachment(filename="entries.txt", disposition="attachment"),
    attachment("text/html", filename="entries.html"),
    {"mimeType": "multipart/mixed", "headers": [{"name": "Content-Disposition", "value": "attachment"}],
     "parts": [attachment()]},
], ids=["plain-file", "html-file", "attached-subtree"])
async def test_actual_export_fixture_and_published_reads_keep_only_the_cover(tmp_path, file_part):
    snapshot = build_snapshot(
        [gmail_message([cover("text/html"), file_part])], own_address="alex.smith@example.org", name="Alex Smith",
        days=30, include_pressure_thread=False, exported_at=datetime(2026, 10, 28, tzinfo=UTC),
    )
    assert snapshot["emails"][0]["body"] == COVER
    path = tmp_path / "synthetic.inbox.json"
    path.write_text(json.dumps(snapshot))
    source = FixtureMailSource(path)
    assert source.read(MESSAGE_ID).body == COVER
    init_subtask_store(Store())
    save_world(World(start_time=SCENARIO.start_time, now=SCENARIO.start_time))
    service = make_fixture_mail(ToolContext(SCENARIO, Mailbox(source, SCENARIO)))
    try:
        found = (await service.call_tool("search_threads", {"query": 'subject:"Invoice review"'}))["structuredContent"]
        assert len(found["threads"]) == 1
        thread = await service.call_tool("get_thread", {"threadId": found["threads"][0]["id"]})
        read = await service.call_tool("get_message", {"messageId": MESSAGE_ID})
        assert not thread.get("isError") and not read.get("isError")
        assert thread["structuredContent"]["messages"] == [read["structuredContent"]]
        message = read["structuredContent"]
        assert message["plaintextBody"] == COVER
        assert message["attachmentIds"] == message["attachments"] == []
    finally:
        await service.aclose()

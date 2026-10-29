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


def encoded_part(mime, text, encoding, *, declared=None, header_name="Content-Type"):
    return {"mimeType": mime, "headers": [{"name": header_name,
        "value": f'{mime}; charset="{declared or encoding}"'}], "body": {
        "data": base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip("=")}}


@pytest.mark.parametrize("encoding,text", [
    ("iso-8859-1", "Café refund £20"),
    ("windows-1252", "Café — refund £20"),
    ("utf-8", "Café — refund £20"),
])
@pytest.mark.parametrize("mime", ["text/plain", "text/html"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("mixed_case", [False, True])
def test_declared_text_charset_preserves_body_and_raw_message(encoding, text, mime, nested, mixed_case):
    part = encoded_part(mime, f"<p>{text}</p>" if mime == "text/html" else text, encoding,
        declared=encoding.upper() if mixed_case else encoding,
        header_name="cOnTeNt-TyPe" if mixed_case else "Content-Type")
    message = gmail_message([part])
    if not nested:
        message["payload"]["mimeType"] = mime
        message["payload"]["body"] = part["body"]
        message["payload"]["headers"] += part["headers"]
        message["payload"].pop("parts")
    original = copy.deepcopy(message)
    parsed = parse_gmail_message(message)
    baseline = parse_gmail_message(gmail_message([text_part("text/plain", text)]))
    assert parsed.body == text
    assert parsed.model_dump(exclude={"body"}) == baseline.model_dump(exclude={"body"})
    assert message == original


@pytest.mark.parametrize("mime", ["text/plain", "text/html"])
@pytest.mark.parametrize("content_type", [None, "text/plain", 'text/plain; charset=""',
    "text/plain; charset=no-such-charset", "text/plain; charset=base64_codec", "text/plain; charset=rot_13",
    "text/plain; charset=idna"])
def test_absent_or_unusable_charset_keeps_utf8_replacement_fallback(mime, content_type):
    raw = b"Caf\xc3\xa9 \xff literal=E9"
    data = b"<p>" + raw + b"</p>" if mime == "text/html" else raw
    part = {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(data).decode()}}
    if content_type is not None:
        part["headers"] = [{"name": "Content-Type", "value": content_type}]
    assert parse_gmail_message(gmail_message([part])).body == "Café � literal=E9"


def test_child_charsets_are_independent_and_plain_preference_is_preserved():
    first = encoded_part("text/plain", "Café £20", "iso-8859-1")
    second = encoded_part("text/plain", "Résumé — next", "windows-1252")
    html = encoded_part("text/html", "<p>Other HTML — omitted</p>", "windows-1252")
    message = gmail_message([{"mimeType": "multipart/alternative", "headers": [
        {"name": "Content-Type", "value": 'multipart/alternative; charset="us-ascii"'}],
        "parts": [first, second, html]}])
    original = copy.deepcopy(message)
    assert parse_gmail_message(message).body == "Café £20\nRésumé — next"
    assert message == original


def test_charset_does_not_inherit_container_or_html_meta_or_transfer_decode():
    text = "Café literal=E9"
    plain = text_part("text/plain", text)
    plain["headers"] = [{"name": "Content-Transfer-Encoding", "value": "quoted-printable"}]
    message = gmail_message([plain])
    message["payload"]["headers"].append({"name": "Content-Type", "value": "multipart/mixed; charset=ascii"})
    assert parse_gmail_message(message).body == text
    html = text_part("text/html", '<meta charset="windows-1252"><p>Café — UTF8</p>')
    assert parse_gmail_message(gmail_message([html])).body == "Café — UTF8"


@pytest.mark.parametrize("file_part", [
    {"mimeType": "text/plain", "filename": "bad.txt", "body": {"data": "a"}},
    {"mimeType": "text/html", "headers": [{"name": "Content-Disposition", "value": "attachment"}],
     "body": {"data": "a"}},
    {"mimeType": "multipart/mixed", "filename": "bad.mime", "parts": [
        {"mimeType": "text/plain", "body": {"data": "a"}}]},
])
def test_attachments_are_excluded_before_charset_or_body_decoding(file_part):
    part = encoded_part("text/html", "<p>Café — £20</p>", "windows-1252")
    assert parse_gmail_message(gmail_message([part, file_part])).body == "Café — £20"


@pytest.mark.parametrize("charset", ["utf-8", "iso-8859-1"])
def test_bad_base64_does_not_become_a_charset_fallback(charset):
    import binascii

    part = encoded_part("text/plain", "x", charset)
    part["body"]["data"] = "a"
    with pytest.raises(binascii.Error):
        parse_gmail_message(gmail_message([part]))


def test_readonly_gmail_source_mailbox_and_serialization_keep_decoded_body(tmp_path, monkeypatch):
    from refund_eval.world.state import Email

    message = gmail_message([encoded_part("text/plain", "Café — refund £20", "windows-1252")])
    message["internalDate"] = str(int(SCENARIO.start_time.timestamp() * 1000) - 1000)
    original = copy.deepcopy(message)
    calls = []

    class Transport:
        def users(self):
            return self

        def messages(self):
            return self

        def list(self, **kwargs):
            calls.append(("list", kwargs))
            return type("Result", (), {"execute": lambda self: {"messages": [{"id": MESSAGE_ID}]}})()

        def get(self, **kwargs):
            calls.append(("get", kwargs))
            return type("Result", (), {"execute": lambda self: copy.deepcopy(message)})()

    source = GmailMailSource(tmp_path / "absent-client.json", tmp_path / "absent-token.json")
    source._service = Transport()
    monkeypatch.setattr(source, "_credentials", lambda: pytest.fail("No Gmail credentials may be loaded"))
    mailbox = Mailbox(source, SCENARIO)
    world = World(start_time=SCENARIO.start_time, now=SCENARIO.start_time)
    found = mailbox.search(world, "", limit=20)
    assert len(found) == 1 and found[0].body == "Café — refund £20"
    assert mailbox.get(world, MESSAGE_ID) == found[0]
    assert Email.model_validate_json(found[0].model_dump_json()) == found[0]
    assert {name for name, _ in calls} == {"list", "get"}
    assert all(kw["format"] == "full" for name, kw in calls if name == "get")
    assert message == original

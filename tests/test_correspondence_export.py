from __future__ import annotations

import base64
import json
import os
import stat
from copy import deepcopy
from datetime import datetime, timedelta

import pytest
from markdown_it import MarkdownIt

from refund_eval.companies.models import CompanyAction, CompanyEvent
from refund_eval.runtime.correspondence import (
    JSON_NAME,
    MARKDOWN_NAME,
    build_correspondence,
    export_saved_trial,
    render_markdown,
)
from refund_eval.runtime.records import InteractionEffect, InteractionRecord
from refund_eval.runtime.trial_artifacts import world_store
from refund_eval.world.event_log import EVENTS_KEY, changes, rebuild
from refund_eval.world.state import (
    WORLD_KEY,
    Email,
    Interception,
    ScheduledEmail,
    World,
)


def _email(*, message_id, sender, org, sent_at, source="counterparty", thread_id="thread-1", body="Full saved body"):
    return {
        "id": message_id,
        "thread_id": thread_id,
        "sender": sender,
        "to": ["traveller@example.test"],
        "cc": [],
        "subject": f"Subject for {message_id}",
        "body": body,
        "sent_at": sent_at,
        "source": source,
        "automatic": False,
        "org": org,
    }


def _interception(interception_id, channel, time, payload, *, org="company"):
    return {
        "id": interception_id,
        "time": time,
        "channel": channel,
        "tool": f"{channel}.send",
        "target": "saved-target",
        "payload": payload,
        "org": org,
        "to_user": False,
        "response_summary": "Message sent.",
    }


def _interaction(interception_id, mode, time, *, org="company", body="Reply body", subject="Reply subject",
                 page_body="", effects=(), effective_at=None):
    event = CompanyEvent(kind="contact", incoming="Full validated incoming event", mode=mode)
    action = CompanyAction(
        decision="reply", reply=True, reply_after_hours=0, subject=subject, body=body, page_body=page_body,
    )
    record = InteractionRecord(
        id=f"{interception_id}:{org}:{event.kind}", time=time, org=org, event=event, status="applied",
        action=action, effective_at=effective_at,
        effects=[InteractionEffect(id=effect_id, kind="email", delivery_at=effective_at or time)
                                for effect_id in effects],
    )
    return record.model_dump(mode="json")


def _full_store():
    seeded = _email(
        message_id="seed-unmatched",
        sender="Seed Sender <seed@private.test>",
        org=None,
        sent_at="2026-10-30T09:00:00+01:00",
        source="seed",
        thread_id="thread-seed",
        body="Seeded body, retained in full.",
    )
    unmatched = _email(
        message_id="unmatched-inbox",
        sender="Counterparty <sender@org.test>",
        org=None,
        sent_at="2026-10-30T10:00:00+01:00",
        body="Unmatched inbox body.",
    )
    unknown_sender = _email(
        message_id="unknown-sender-known-org",
        sender="",
        org="known-organization",
        sent_at="2026-10-30T11:00:00+01:00",
        body="The sender is absent but the organization is known.",
    )
    delivered = _email(
        message_id="reply-1",
        sender="Company Agent <agent@company.test>",
        org="company",
        sent_at="2026-10-31T01:30:00+01:00",
        body="Complete inbound reply body.",
    )
    pending = _email(
        message_id="pending-1",
        sender="Company Agent <agent@company.test>",
        org="company",
        sent_at="2026-11-01T10:00:00Z",
        body="Reply still pending.",
    )
    scheduled = {"email": pending, "due_at": "2026-11-01T10:00:00Z", "facts_on_delivery": [],
                 "promises_on_delivery": []}
    out_email = _interception(
        "out-email", "email", "2026-10-31T01:30:00+01:00",
        {"from": "Traveller <traveller@example.test>", "to": ["company@example.test"],
         "subject": "Private subject", "body": "Complete outbound email body.",
         "message_id": "outgoing-message", "thread_id": "thread-1"},
    )
    out_phone = _interception(
        "out-phone", "phone", "2026-10-31T00:30:00Z",
        {"from": "traveller", "to": "+440000000000", "body": "Call request body."},
    )
    out_blocked = _interception(
        "blocked-form", "form", "2026-10-30T12:00:00Z",
        {"blocked": True, "fields": {"claim": "full private form payload"}}, org=None,
    )
    out_failed = _interception(
        "failed-chat", "chat", "2026-10-30T13:00:00Z",
        {"delivery_status": "failed", "body": "Chat submission body."},
    )
    out_form = _interception(
        "form-submit", "form", "2026-10-30T13:30:00Z",
        {"from": "Traveller <traveller@example.test>", "fields": {"claim": "full form submission"}},
    )
    out_web = _interception(
        "web-action", "web_action", "2026-10-30T14:00:00Z",
        {"url": "https://example.test/contact", "body": "Web action body."},
    )
    out_unknown_time = _interception(
        "unknown-time", "email", None, {"body": "Message with no recorded time."},
    )
    omitted_payment = _interception("payment", "payment", "2026-10-30T15:00:00Z", {"amount": 10})
    omitted_inbox_change = _interception("inbox-change", "inbox_change", "2026-10-30T16:00:00Z", {"id": "x"})
    email_interaction = _interaction(
        "out-email", "email", "2026-10-31T00:30:00Z", body="Complete inbound reply body.", effects=("reply-1",),
    )
    phone_interaction = _interaction(
        "out-phone", "phone", "2026-10-31T00:30:00Z", body="Complete phone transcript.", effects=(),
    )
    form_interaction = _interaction(
        "form-submit", "form", "2026-10-30T13:30:00Z", body="Form email reply body.",
        subject="Form reply", page_body="Form confirmation page.", effects=("form-reply-1",),
        effective_at="2026-10-31T13:40:00Z",
    )
    events = [
        {"seq": 10, "sim_time": "2026-10-30T10:00:00Z", "kind": "mail_queued",
         "payload": {"scheduled": scheduled}},
        {"seq": 11, "sim_time": "2026-10-31T00:30:00Z", "kind": "mail_queued",
         "payload": {"scheduled": {"email": delivered, "due_at": delivered["sent_at"],
                                     "facts_on_delivery": [], "promises_on_delivery": []}}},
        {"seq": 12, "sim_time": "2026-10-31T00:30:00Z", "kind": "mail_delivered", "payload": {"email": delivered}},
        {"seq": 13, "sim_time": "2026-10-31T00:31:00Z", "kind": "mail_dequeued", "payload": {"ids": ["reply-1"]}},
        {"seq": 14, "sim_time": "2026-10-31T00:32:00Z", "kind": "mail_opened", "payload": {"id": "reply-1"}},
        {"seq": 19, "sim_time": "2026-10-31T00:30:00Z", "kind": "agent_write", "payload": {"interception": out_phone}},
        {"seq": 20, "sim_time": "2026-10-31T00:30:00Z", "kind": "agent_write", "payload": {"interception": out_email}},
        {"seq": 21, "sim_time": "2026-10-30T12:00:00Z", "kind": "agent_write", "payload": {"interception": out_blocked}},
        {"seq": 22, "sim_time": "2026-10-30T13:00:00Z", "kind": "agent_write", "payload": {"interception": out_failed}},
        {"seq": 23, "sim_time": "2026-10-30T13:30:00Z", "kind": "agent_write", "payload": {"interception": out_form}},
        {"seq": 24, "sim_time": "2026-10-31T00:33:00Z", "kind": "mail_opened",
         "payload": {"id": "outgoing-message"}},
    ]
    world = {
        "overlay_inbox": [seeded, unmatched, unknown_sender, delivered,
                          _email(message_id="form-reply-1", sender="Saved Form Agent <agent@company.test>",
                                 org=None, sent_at="2026-10-31T13:30:00Z", thread_id="form-thread",
                                 body="Saved canonical form reply body.")],
        "pending": [scheduled],
        "opened_mail": ["reply-1"],
        "interceptions": [out_email, out_phone, out_blocked, out_failed, out_form, out_web, out_unknown_time,
                           omitted_payment, omitted_inbox_change],
        "interactions": [email_interaction, phone_interaction, form_interaction],
    }
    return {WORLD_KEY: world, EVENTS_KEY: events}


def _history():
    start = datetime.fromisoformat("2026-10-30T00:00:00+00:00")
    seed = Email(
        id="saved-seed", thread_id="seed-thread", sender="Seed Person <seed@example.test>",
        to=["traveller@example.test"], subject="Seed subject", body="Seed body", sent_at=start - timedelta(hours=1),
        source="seed", org=None,
    )
    reply = Email(
        id="queued-reply", thread_id="reply-thread", sender="Company Person <company@example.test>",
        to=["traveller@example.test"], subject="Reply subject", body="Queued reply body",
        sent_at=start + timedelta(hours=1), org="company",
    )
    initial = World(start_time=start, now=start, overlay_inbox=[seed])
    queued = initial.model_copy(update={"pending": [ScheduledEmail(email=reply, due_at=reply.sent_at)]})
    delivered = queued.model_copy(update={"pending": [], "overlay_inbox": [seed, reply]})
    outbound = Interception(
        id="history-outbound", time=start + timedelta(hours=2), channel="email", tool="email.send",
        target="company@example.test", payload={"to": ["company@example.test"], "body": "Outbound history body",
                                                  "message_id": "history-outbound-message"}, org="company",
        response_summary="Message sent.",
    )
    final = delivered.model_copy(update={"opened_mail": [reply.id], "interceptions": [outbound]})
    events = []
    previous = None
    sequence = 0
    for world in (initial, queued, delivered, final):
        current = world.model_dump(mode="json")
        for kind, payload in changes(previous, current):
            events.append({"seq": sequence, "sim_time": current["now"], "kind": kind, "payload": payload})
            sequence += 1
        previous = current
    return final, events


def test_builder_preserves_both_directions_and_delivery_evidence_without_mutation():
    store = _full_store()
    original = deepcopy(store)
    export = build_correspondence(store)
    rows = {row["id"]: row for row in export["records"]}

    assert store == original
    assert export["coverage"]["completeness"] == "not_verified"
    assert export["coverage"]["identities_hidden"] is False
    omitted = export["coverage"]["omitted_noncorrespondence"]
    assert "Full linked InteractionRecord source records" in omitted["retained_linked_interaction_provenance"]
    assert "standalone claims" in omitted["world_fields"][1]
    assert {"payment", "inbox_change"} <= set(export["coverage"]["omitted_noncorrespondence"]["interception_channels"])

    seed = rows["inbound:seed-unmatched"]
    assert seed["sender_identity_status"] == "known" and seed["organization_status"] == "unknown"
    assert seed["delivery_status"] == "present_in_saved_inbox_delivery_unconfirmed"
    assert seed["present_in_saved_inbox"] is True and seed["delivery_status"] != "failed"
    assert seed["body"] == "Seeded body, retained in full."
    unmatched = rows["inbound:unmatched-inbox"]
    assert unmatched["delivery_status"] == "present_in_saved_inbox_delivery_unconfirmed"
    unknown_sender = rows["inbound:unknown-sender-known-org"]
    assert unknown_sender["sender_identity_status"] == "unknown"
    assert unknown_sender["organization_status"] == "known"

    reply = rows["inbound:reply-1"]
    assert reply["delivery_status"] == "delivered"
    assert reply["currently_pending"] is False and reply["read_marker"] == "read"
    assert [event["kind"] for event in reply["lifecycle_events"]] == [
        "mail_queued", "mail_delivered", "mail_dequeued", "mail_opened",
    ]
    assert reply["interaction_ids"] == ["out-email:company:contact"]
    assert reply["effect_ids"] == ["reply-1"]
    assert "outbound:interception:out-email" in reply["linked_record_ids"]
    assert reply["organization"] == "company"

    pending = rows["inbound:pending-1"]
    assert pending["delivery_status"] == "pending_only"
    assert pending["currently_pending"] is True

    outgoing = rows["outbound:interception:out-email"]
    assert outgoing["submission_status"] == "submission_recorded"
    assert outgoing["delivery_status"] == "recipient_delivery_unconfirmed"
    assert outgoing["response_summary"] == "Message sent."
    assert outgoing["raw_payload"]["body"] == "Complete outbound email body."
    assert outgoing["read_marker"] == "read"
    assert "inbound:outgoing-message" not in rows
    assert outgoing["thread_id"] == reply["thread_id"]
    assert reply["id"] in outgoing["linked_record_ids"]
    assert len(reply["source_records"]) >= 4

    blocked = rows["outbound:interception:blocked-form"]
    assert blocked["submission_status"] == "blocked" and blocked["delivery_status"] == "blocked"
    failed = rows["outbound:interception:failed-chat"]
    assert failed["delivery_status"] == "failed"
    assert "inbound:interaction:out-phone:company:contact:body" in rows
    assert rows["inbound:interaction:out-phone:company:contact:body"]["body"] == "Complete phone transcript."
    assert rows["inbound:form-reply-1"]["body"] == "Saved canonical form reply body."
    assert rows["inbound:form-reply-1"]["thread_id"] == "form-thread"
    form_page = rows["inbound:interaction:form-submit:company:contact:page_body"]
    assert form_page["body"] == "Form confirmation page."
    assert form_page["simulated_time"] == "2026-10-30T13:30:00Z"
    assert form_page["simulated_time_basis"] == "interaction.time"
    assert not any(row.get("interception_id") in {"payment", "inbox-change"} for row in export["records"])
    assert rows["inbound:form-reply-1"]["organization"] == "company"

    outbound_order = [row["id"] for row in export["records"]
                      if row["id"] in {"outbound:interception:out-phone", "outbound:interception:out-email"}]
    assert outbound_order == ["outbound:interception:out-phone", "outbound:interception:out-email"]
    assert outgoing["simulated_time"] == rows["outbound:interception:out-phone"]["simulated_time"]
    unknown_time = rows["outbound:interception:unknown-time"]
    assert unknown_time["simulated_time"] is None
    assert unknown_time["simulated_time_status"] == "unknown"
    assert "Simulated time: **Unknown in saved source**" in render_markdown(export)


def test_event_only_store_keeps_lifecycle_evidence_and_marks_completeness_unavailable():
    delivered = _email(
        message_id="event-only-message",
        sender="Known Sender <sender@example.test>",
        org=None,
        sent_at="2026-10-30T10:00:00Z",
        body="Event-only body.",
    )
    store = {EVENTS_KEY: [
        {"seq": 1, "sim_time": "2026-10-30T10:00:00Z", "kind": "mail_delivered", "payload": {"email": delivered}},
        {"seq": 2, "sim_time": "2026-10-30T10:05:00Z", "kind": "mail_opened", "payload": {"id": delivered["id"]}},
        {"seq": 3, "sim_time": "2026-10-30T10:06:00Z", "kind": "mail_opened", "payload": {"id": "marker-only"}},
    ]}
    export = build_correspondence(store)
    row = next(row for row in export["records"] if row.get("message_id") == delivered["id"])

    assert export["coverage"]["completeness"] == "unavailable"
    assert export["coverage"]["saved_world_available"] is False
    assert row["body"] == "Event-only body."
    assert row["delivery_status"] == "delivered" and row["read_marker"] == "read"
    assert row["currently_pending"] is None
    assert [event["kind"] for event in row["lifecycle_events"]] == ["mail_delivered", "mail_opened"]
    marker = next(row for row in export["records"] if row.get("message_id") == "marker-only")
    assert marker["body"] is None and marker["read_marker"] == "read"
    assert marker["direction"] == "unknown"
    assert marker["id"] == "unknown:marker:marker-only"
    assert "inbound:marker-only" not in {row["id"] for row in export["records"]}


def test_web_action_decodes_text_but_preserves_raw_and_nontext_payloads():
    markup = '<form><script>alert("saved text")</script></form>'
    text_body = base64.b64encode(markup.encode()).decode()
    binary_body = base64.b64encode(b"\x00\xff\x01").decode()
    store = {WORLD_KEY: {"interceptions": [
        _interception("text-post", "web_action", "2026-10-30T12:00:00Z", {
            "body_base64": text_body, "headers": {"content-type": "text/html; charset=utf-8"},
        }),
        _interception("binary-post", "web_action", "2026-10-30T12:01:00Z", {
            "body_base64": binary_body, "headers": {"content-type": "application/octet-stream"},
        }),
        _interception("invalid-post", "web_action", "2026-10-30T12:02:00Z", {
            "body_base64": "not base64!", "headers": {"content-type": "text/plain"},
        }),
        _interception("empty-post", "web_action", "2026-10-30T12:03:00Z", {
            "body_base64": "", "headers": {"content-type": "text/plain"},
        }),
    ]}}

    export = build_correspondence(store)
    rows = {row["interception_id"]: row for row in export["records"]}

    assert rows["text-post"]["body"] == markup
    assert rows["text-post"]["body_encoding"] == "utf-8"
    assert rows["text-post"]["body_decoding_status"] == "decoded_text"
    assert rows["text-post"]["raw_payload"]["body_base64"] == text_body
    assert rows["binary-post"]["body"] is None
    assert rows["binary-post"]["body_encoding"] == "base64"
    assert rows["binary-post"]["body_decoding_status"] == "nontext"
    assert rows["binary-post"]["raw_payload"]["body_base64"] == binary_body
    assert rows["invalid-post"]["body"] is None
    assert rows["invalid-post"]["body_decoding_status"] == "invalid_base64"
    assert rows["empty-post"]["body"] == ""
    assert rows["empty-post"]["body_decoding_status"] == "decoded_text"
    assert f"```\n{markup}\n```" in render_markdown(export)


@pytest.mark.parametrize("charset", [
    "`![x](https://example.invalid/x)`",
    "`![x](https://example.invalid/x)",
    "![x](https://example.invalid/x)`",
])
def test_markdown_escapes_untrusted_body_encoding_and_preserves_raw_header(charset):
    content_type = f"text/plain; charset={charset}"
    export = build_correspondence({WORLD_KEY: {"interceptions": [
        _interception("injected-charset", "web_action", "2026-10-30T12:00:00Z", {
            "body_base64": base64.b64encode(b"Saved body").decode(),
            "headers": {"content-type": content_type},
        }),
    ]}})
    row = next(row for row in export["records"] if row.get("interception_id") == "injected-charset")
    markdown = render_markdown(export)
    rendered = MarkdownIt().render(markdown)

    assert row["body"] is None
    assert row["raw_payload"]["headers"]["content-type"] == content_type
    assert "<img" not in rendered
    assert f"<code>{charset}</code>" in rendered


def test_web_action_decodes_text_only_multipart_but_not_binary_uploads():
    text_body = (
        b"--SavedBoundary\r\nContent-Disposition: form-data; name=\"subject\"\r\n\r\nEnquiry\r\n"
        b"--SavedBoundary\r\nContent-Disposition: form-data; name=\"message\"\r\n\r\nPlease reply, \xc3\x89lodie.\r\n"
        b"--SavedBoundary--\r\n"
    )
    binary_body = (
        b"--SavedBoundary\r\nContent-Disposition: form-data; name=\"message\"\r\n\r\nPlease reply.\r\n"
        b"--SavedBoundary\r\nContent-Disposition: form-data; name=\"upload\"; filename=\"proof.bin\"\r\n"
        b"Content-Type: application/octet-stream\r\n\r\n\x00\xff\x01\r\n--SavedBoundary--\r\n"
    )
    text_encoded = base64.b64encode(text_body).decode()
    binary_encoded = base64.b64encode(binary_body).decode()
    export = build_correspondence({WORLD_KEY: {"interceptions": [
        _interception("text-multipart", "web_action", "2026-10-30T12:00:00Z", {
            "body_base64": text_encoded,
            "headers": {"content-type": "multipart/form-data; boundary=SavedBoundary"},
        }),
        _interception("binary-multipart", "web_action", "2026-10-30T12:01:00Z", {
            "body_base64": binary_encoded,
            "headers": {"content-type": "multipart/form-data; boundary=SavedBoundary"},
        }),
    ]}})
    rows = {row["interception_id"]: row for row in export["records"]}

    assert rows["text-multipart"]["body"] == "subject: Enquiry\nmessage: Please reply, Élodie."
    assert rows["text-multipart"]["body_encoding"] == "utf-8"
    assert rows["text-multipart"]["body_decoding_status"] == "decoded_text"
    assert rows["text-multipart"]["raw_payload"]["body_base64"] == text_encoded
    assert rows["binary-multipart"]["body"] is None
    assert rows["binary-multipart"]["body_decoding_status"] == "nontext"
    assert rows["binary-multipart"]["raw_payload"]["body_base64"] == binary_encoded


def test_multipart_preserves_transfer_encoded_form_values():
    body = (
        b"--SavedBoundary\r\nContent-Disposition: form-data; name=\"base64\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nSGVsbG8=\r\n"
        b"--SavedBoundary\r\nContent-Disposition: form-data; name=\"quoted\"\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\ncaf=E9\r\n--SavedBoundary--\r\n"
    )
    encoded = base64.b64encode(body).decode()
    export = build_correspondence({WORLD_KEY: {"interceptions": [
        _interception("transfer-encoded", "web_action", "2026-10-30T12:00:00Z", {
            "body_base64": encoded,
            "headers": {"content-type": "multipart/form-data; boundary=SavedBoundary"},
        }),
    ]}})
    row = next(row for row in export["records"] if row.get("interception_id") == "transfer-encoded")

    assert row["body"] == "base64: SGVsbG8=\nquoted: caf=E9"
    assert row["body_decoding_status"] == "decoded_text"
    assert row["raw_payload"]["body_base64"] == encoded


@pytest.mark.parametrize("charset_position", ["before", "after"])
def test_multipart_charset_field_sets_default_but_explicit_part_charset_wins(charset_position):
    charset_part = ("_charset_", b"windows-1252", b"")
    content_parts = [
        ("message", b"Price \xa3", b""),
        ("explicit", b"caf\xc3\xa9", b"Content-Type: text/plain; charset=utf-8\r\n"),
    ]
    parts = [charset_part, *content_parts] if charset_position == "before" else [*content_parts, charset_part]
    body_parts = []
    for name, content, headers in parts:
        body_parts.extend([
            f"--SavedBoundary\r\nContent-Disposition: form-data; name=\"{name}\"\r\n".encode(),
            headers,
            b"\r\n",
            content,
            b"\r\n",
        ])
    body_parts.append(b"--SavedBoundary--\r\n")
    encoded = base64.b64encode(b"".join(body_parts)).decode()
    export = build_correspondence({WORLD_KEY: {"interceptions": [
        _interception("charset-form", "web_action", "2026-10-30T12:00:00Z", {
            "body_base64": encoded,
            "headers": {"content-type": "multipart/form-data; boundary=SavedBoundary"},
        }),
    ]}})
    row = next(row for row in export["records"] if row.get("interception_id") == "charset-form")

    expected_lines = ["message: Price £", "explicit: café"]
    if charset_position == "before":
        expected_lines.insert(0, "_charset_: windows-1252")
    else:
        expected_lines.append("_charset_: windows-1252")
    assert row["body"] == "\n".join(expected_lines)
    assert row["body_encoding"] == "windows-1252"
    assert row["body_decoding_status"] == "decoded_text"


def test_multipart_invalid_charset_declaration_is_marked_undecodable():
    body = (
        b"--SavedBoundary\r\nContent-Disposition: form-data; name=\"message\"\r\n\r\nPrice \xa3\r\n"
        b"--SavedBoundary\r\nContent-Disposition: form-data; name=\"_charset_\"\r\n\r\nunknown-codec-x\r\n"
        b"--SavedBoundary--\r\n"
    )
    encoded = base64.b64encode(body).decode()
    export = build_correspondence({WORLD_KEY: {"interceptions": [
        _interception("invalid-charset-form", "web_action", "2026-10-30T12:00:00Z", {
            "body_base64": encoded,
            "headers": {"content-type": "multipart/form-data; boundary=SavedBoundary"},
        }),
    ]}})
    row = next(row for row in export["records"] if row.get("interception_id") == "invalid-charset-form")

    assert row["body"] is None
    assert row["body_encoding"] == "unknown-codec-x"
    assert row["body_decoding_status"] == "undecodable"


def test_outbound_subject_only_email_preserves_explicit_empty_body():
    export = build_correspondence({WORLD_KEY: {"interceptions": [
        _interception("empty-message", "email", "2026-10-30T12:00:00Z", {
            "subject": "Subject only",
            "body": "",
        }),
    ]}})
    row = next(row for row in export["records"] if row.get("interception_id") == "empty-message")

    assert row["subject"] == "Subject only"
    assert row["body"] == ""


@pytest.mark.parametrize("status", ["silent", "failed", "pending"])
def test_outbound_submission_retains_matching_interaction_without_reply(status):
    interaction_id = "silent-submit:company:contact"
    interaction = InteractionRecord(
        id=interaction_id,
        time="2026-10-30T12:00:00Z",
        org="company",
        event=CompanyEvent(kind="contact", incoming="Validated saved contact event", mode="email"),
        status=status,
        action=(CompanyAction(decision="silent", reply=False, reply_after_hours=0, body="")
                if status == "silent" else None),
        error="Saved failure" if status == "failed" else None,
    )
    store = {WORLD_KEY: {
        "interceptions": [_interception("silent-submit", "email", "2026-10-30T12:00:00Z",
                                         {"body": "Submitted without a response."})],
        "interactions": [interaction],
    }}

    export = build_correspondence(store)
    outbound = next(row for row in export["records"] if row.get("interception_id") == "silent-submit")
    saved_record = next(source["record"] for source in outbound["source_records"]
                        if source["record"].get("id") == interaction_id)

    assert outbound["interaction_ids"] == [interaction_id]
    assert interaction_id in outbound["reference_ids"]
    assert saved_record == interaction.model_dump(mode="json")
    assert not any(row["direction"] == "inbound" for row in export["records"])


def test_saved_world_precedes_conflicting_events_and_unmatched_reply_reference_is_not_linked():
    store = _full_store()
    changed = _interception(
        "out-email", "email", "2026-10-31T02:00:00Z",
        {"to": ["changed@example.test"], "body": "Older event body", "delivery_status": "failed"},
    )
    store[EVENTS_KEY].append({"seq": 100, "sim_time": "2026-10-31T02:00:00Z", "kind": "agent_write",
                              "payload": {"interception": changed}})
    store[WORLD_KEY]["interceptions"].append(
        _interception("unmatched-reference", "email", "2026-10-31T03:00:00Z",
                      {"in_reply_to": "does-not-exist", "body": "Reference-only outbound"})
    )
    export = build_correspondence(store)
    rows = {row["id"]: row for row in export["records"]}
    outgoing = rows["outbound:interception:out-email"]
    assert outgoing["body"] == "Complete outbound email body."
    assert outgoing["recipients"]["to"] == ["company@example.test"]
    assert outgoing["delivery_status"] == "recipient_delivery_unconfirmed"
    assert len(outgoing["source_records"]) >= 2
    unmatched = rows["outbound:interception:unmatched-reference"]
    assert "does-not-exist" in unmatched["reference_ids"]
    assert unmatched["linked_record_ids"] == []


def test_events_only_use_latest_record_version_and_empty_sources_are_not_complete():
    first = _interception("event-version", "email", "2026-10-30T10:00:00Z",
                           {"to": ["first@example.test"], "body": "First version"})
    latest = _interception("event-version", "email", "2026-10-30T11:00:00Z",
                           {"to": ["latest@example.test"], "body": "Latest version", "delivery_status": "failed"})
    export = build_correspondence({EVENTS_KEY: [
        {"seq": 1, "sim_time": first["time"], "kind": "agent_write", "payload": {"interception": first}},
        {"seq": 2, "sim_time": latest["time"], "kind": "agent_write", "payload": {"interception": latest}},
    ]})
    row = next(row for row in export["records"] if row.get("interception_id") == "event-version")
    assert row["body"] == "Latest version"
    assert row["recipients"]["to"] == ["latest@example.test"]
    assert row["delivery_status"] == "failed"
    assert len(row["source_records"]) == 2

    empty = build_correspondence({WORLD_KEY: {}, EVENTS_KEY: []})
    assert empty["coverage"]["saved_world_available"] is True
    assert empty["coverage"]["source_data_available"] is False
    assert empty["coverage"]["completeness"] == "unavailable"


def test_eval_export_uses_saved_store_and_never_overwrites_input_or_existing_output(tmp_path, capsys):
    from inspect_ai.log import (
        EvalConfig,
        EvalDataset,
        EvalLog,
        EvalSample,
        EvalSpec,
        write_eval_log,
    )

    source = tmp_path / "private.eval"
    source.parent.mkdir(parents=True, exist_ok=True)
    log = EvalLog(
        status="success",
        eval=EvalSpec(created="2026-11-01T00:00:00Z", task="synthetic", dataset=EvalDataset(),
                      model="mockllm/model", config=EvalConfig(), task_args={}),
        samples=[EvalSample(id="synthetic", epoch=1, input="Synthetic", target="", store=_full_store())],
    )
    write_eval_log(log, str(source))
    original_bytes = source.read_bytes()
    destination = tmp_path / "new-private-export"

    previous_umask = os.umask(0o022)
    try:
        export_saved_trial(source, destination)
    finally:
        os.umask(previous_umask)

    assert source.read_bytes() == original_bytes
    assert {JSON_NAME, MARKDOWN_NAME} == {path.name for path in destination.iterdir()}
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in destination.iterdir())
    private_json = (destination / JSON_NAME).read_text(encoding="utf-8")
    markdown = (destination / MARKDOWN_NAME).read_text(encoding="utf-8")
    assert "Complete outbound email body." in private_json
    assert "Seed Sender <seed@private.test>" in private_json
    assert "Complete outbound email body." in markdown
    assert capsys.readouterr().out == ""

    existing = tmp_path / "existing-output"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        export_saved_trial(source, existing)
    assert sentinel.read_text(encoding="utf-8") == "do not overwrite"


def test_eval_export_refuses_multiple_samples(tmp_path):
    from inspect_ai.log import (
        EvalConfig,
        EvalDataset,
        EvalLog,
        EvalSample,
        EvalSpec,
        write_eval_log,
    )

    source = tmp_path / "multi.eval"
    spec = EvalSpec(created="2026-11-01T00:00:00Z", task="synthetic", dataset=EvalDataset(),
                    model="mockllm/model", config=EvalConfig(), task_args={})
    log = EvalLog(
        status="success", eval=spec,
        samples=[EvalSample(id="one", epoch=1, input="one", target="", store=_full_store()),
                 EvalSample(id="two", epoch=1, input="two", target="", store=_full_store())],
    )
    write_eval_log(log, str(source))
    with pytest.raises(ValueError, match="contains 2 samples"):
        export_saved_trial(source, tmp_path / "multi-output")


def test_native_snapshot_and_state_events_fallback_support_world_only_trials(tmp_path):
    from refund_eval.runtime.trial_artifacts import is_harbor_trial
    from refund_eval.world_service.store import EVENTS_FILE, MemoryStore, OutputWriter

    final_world, events = _history()
    final_data = final_world.model_dump(mode="json")
    assert rebuild(events).model_dump(mode="json") == final_data
    store = MemoryStore({WORLD_KEY: final_data, EVENTS_KEY: events})

    fallback_trial = tmp_path / "fallback-trial"
    OutputWriter(fallback_trial / "artifacts/world").write(store, settings={})
    assert is_harbor_trial(fallback_trial)
    fallback_store = world_store(fallback_trial)
    assert fallback_store[WORLD_KEY] == final_data
    assert rebuild(fallback_store[EVENTS_KEY]).model_dump(mode="json") == final_data
    fallback_export_dir = tmp_path / "fallback-export"
    fallback_export = export_saved_trial(fallback_trial, fallback_export_dir)
    assert fallback_export["source"]["kind"] == "native_harbor"
    assert fallback_export["coverage"]["agent_eval_log_available"] is False
    delivered = next(row for row in fallback_export["records"] if row.get("message_id") == "queued-reply")
    assert delivered["delivery_status"] == "delivered" and delivered["read_marker"] == "read"
    assert [event["kind"] for event in delivered["lifecycle_events"]] == [
        "mail_queued", "mail_delivered", "mail_dequeued", "mail_opened",
    ]
    outbound = next(row for row in fallback_export["records"] if row.get("interception_id") == "history-outbound")
    assert outbound["body"] == "Outbound history body"
    assert outbound["raw_payload"]["message_id"] == "history-outbound-message"

    snapshot_trial = tmp_path / "snapshot-trial"
    snapshot_world_dir = snapshot_trial / "artifacts/world"
    snapshot_world_dir.mkdir(parents=True)
    rpc_snapshot = {"jsonrpc": "2.0", "id": 1, "result": {"parts": [{"kind": "data", "data": {
        "snapshot_version": 1, "store": store,
    }}]}}
    (snapshot_world_dir / "final_snapshot.json").write_text(json.dumps(rpc_snapshot), encoding="utf-8")
    snapshot_export = export_saved_trial(snapshot_trial, tmp_path / "snapshot-export")
    assert snapshot_export["source"]["kind"] == "native_harbor"
    assert snapshot_export["coverage"]["saved_world_available"] is True
    assert snapshot_export["coverage"]["agent_eval_log_available"] is False

    events_only_trial = tmp_path / "events-only-trial"
    events_only_world = events_only_trial / "artifacts/world"
    events_only_world.mkdir(parents=True)
    (events_only_world / EVENTS_FILE).write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    events_only_export = export_saved_trial(events_only_trial, tmp_path / "events-only-export")
    assert events_only_export["coverage"]["saved_world_available"] is False
    assert events_only_export["coverage"]["world_reconstructed_from_events"] is True
    assert events_only_export["coverage"]["completeness"] == "unavailable"
    seeded = next(row for row in events_only_export["records"] if row.get("message_id") == "saved-seed")
    assert seeded["delivery_status"] == "present_in_reconstructed_inbox_delivery_unconfirmed"

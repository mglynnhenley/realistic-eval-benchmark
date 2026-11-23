"""Export private simulated correspondence from saved trial artifacts only.

    python -m refund_eval.runtime.correspondence TRIAL --out PRIVATE_NEW_DIRECTORY

The JSON and Markdown outputs preserve correspondence identities and bodies. They
are private records, not redacted sharing copies. No model, mail, web, or trial
service is contacted.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from email import policy as email_policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from refund_eval.output_paths import validate_private_output
from refund_eval.world.event_log import EVENTS_KEY
from refund_eval.world.state import WORLD_KEY
from refund_eval.world.timeutil import LONDON, as_utc

JSON_NAME = "correspondence.json"
MARKDOWN_NAME = "correspondence.md"
CORRESPONDENCE_CHANNELS = frozenset({"email", "form", "phone", "chat", "web_action"})
OMITTED_CHANNELS = ("payment", "inbox_change")


def _plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
        return value.value
    return value


def _object(value: Any) -> dict[str, Any] | None:
    value = _plain(value)
    return dict(value) if isinstance(value, Mapping) else None


def _sequence(value: Any) -> list[Any]:
    value = _plain(value)
    return value if isinstance(value, list) else []


def _time(value: Any) -> tuple[str | None, float | None]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None, None
    else:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LONDON)
    normalized = as_utc(parsed)
    return normalized.isoformat().replace("+00:00", "Z"), normalized.timestamp()


def _known(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _decode_body_base64(payload: Mapping[str, Any]) -> tuple[str | None, str, str]:
    encoded = payload.get("body_base64")
    if not isinstance(encoded, str):
        return None, "base64", "invalid_base64"
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return None, "base64", "invalid_base64"

    headers = _object(payload.get("headers")) or {}
    content_type = next((str(value) for key, value in headers.items()
                         if str(key).lower() == "content-type"), "")
    media_type, *parameters = content_type.split(";")
    media_type = media_type.strip().lower()
    charset = next((parameter.partition("=")[2].strip().strip('"') for parameter in parameters
                    if parameter.partition("=")[0].strip().lower() == "charset"), "utf-8")
    text_media_type = (media_type.startswith("text/") or media_type in {
        "application/graphql", "application/json", "application/javascript", "application/xml",
        "application/x-www-form-urlencoded",
    } or media_type.endswith(("+json", "+xml")))
    if media_type == "multipart/form-data":
        headers = (f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n"
                   .encode("utf-8", errors="replace"))
        message = BytesParser(policy=email_policy.default).parsebytes(headers + body)
        if not message.is_multipart():
            return None, "base64", "nontext"
        text_parts = []
        for part in message.iter_parts():
            if part.get_filename() is not None or part.get_content_maintype() != "text":
                return None, "base64", "nontext"
            if part.get("Content-Transfer-Encoding") is not None:
                del part["Content-Transfer-Encoding"]
            content = part.get_payload(decode=True)
            if content is None:
                return None, "base64", "nontext"
            part_charset = part.get_content_charset() or "utf-8"
            try:
                text = content.decode(part_charset, errors="strict")
            except (LookupError, UnicodeDecodeError):
                return None, part_charset, "undecodable"
            name = part.get_param("name", header="content-disposition")
            text_parts.append(f"{name}: {text}" if isinstance(name, str) and name else text)
        return "\n".join(text_parts), "multipart/form-data", "decoded_text"
    if media_type and not text_media_type:
        return None, "base64", "nontext"
    try:
        text = body.decode(charset, errors="strict")
    except (LookupError, UnicodeDecodeError):
        return None, charset, "undecodable" if text_media_type else "nontext"
    if not media_type and any(ord(character) < 32 and character not in "\t\n\r" for character in text):
        return None, "base64", "nontext"
    return text, charset, "decoded_text"


def _stable_fallback(prefix: str, source: str, seq: Any, index: int, raw: Any) -> str:
    if type(seq) is int:
        return f"{prefix}:unidentified:{source}:event-{seq}"
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:16]
    return f"{prefix}:unidentified:{source}:{index}:{digest}"


def _event_sequence(event: Mapping[str, Any]) -> int | None:
    value = event.get("seq")
    return value if type(value) is int else None


def _event_reference(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _plain(event[key]) for key in ("seq", "kind", "sim_time", "tool", "tool_call_id")
            if key in event}


class _Correspondence:
    def __init__(self, world: dict[str, Any] | None, events: list[Any]) -> None:
        self.world = world
        self.events = [_object(event) for event in events]
        self.events = [event for event in self.events if event is not None]
        self.rows: dict[str, dict[str, Any]] = {}
        self._source_keys: dict[str, set[str]] = {}
        self._delivered: set[str] = set()
        self._queued: set[str] = set()
        self._read: set[str] = set()
        self._final_pending: set[str] = set()
        self._final_overlay: set[str] = set()
        self._final_opened: set[str] = set()
        self._opened_events: dict[str, list[Mapping[str, Any]]] = {}
        self._interaction_sources: list[dict[str, Any]] = []
        self._omitted_channels: set[str] = set()

    def _new_row(self, row_id: str, direction: str, channel: str) -> dict[str, Any]:
        row = self.rows.get(row_id)
        if row is None:
            row = {
                "id": row_id,
                "direction": direction,
                "channel": channel,
                "simulated_time": None,
                "simulated_time_status": "unknown",
                "simulated_time_raw": None,
                "simulated_time_basis": "unknown",
                "sender": None,
                "sender_identity_status": "unknown",
                "organization": None,
                "organization_status": "unknown",
                "subject": None,
                "body": None,
                "message_id": None,
                "thread_id": None,
                "interception_id": None,
                "in_reply_to": None,
                "interaction_ids": [],
                "effect_ids": [],
                "reference_ids": [],
                "linked_record_ids": [],
                "lifecycle_events": [],
                "source_records": [],
            }
            self.rows[row_id] = row
            self._source_keys[row_id] = set()
        return row

    def _set_time(self, row: dict[str, Any], value: Any, basis: str) -> None:
        normalized, _ = _time(value)
        if normalized is not None and row["simulated_time"] is None:
            row["simulated_time"] = normalized
            row["simulated_time_status"] = "known"
            row["simulated_time_raw"] = _plain(value)
            row["simulated_time_basis"] = basis
        elif value is not None and row["simulated_time"] is None and row["simulated_time_raw"] is None:
            row["simulated_time_raw"] = _plain(value)
            row["simulated_time_basis"] = f"{basis}.unparseable"

    def _merge_field(self, row: dict[str, Any], field: str, value: Any) -> None:
        if value is not None and (self.world is None or row.get(field) is None):
            row[field] = _plain(value)

    def _add_source(self, row: dict[str, Any], source: str, record: Any) -> None:
        raw = _plain(record)
        if raw is None:
            return
        key = json.dumps([source, raw], sort_keys=True, ensure_ascii=False, default=str)
        if key not in self._source_keys[row["id"]]:
            self._source_keys[row["id"]].add(key)
            row["source_records"].append({"source": source, "record": raw})

    def _add_event_ref(self, row: dict[str, Any], event: Mapping[str, Any]) -> None:
        reference = _event_reference(event)
        if reference and reference not in row["lifecycle_events"]:
            row["lifecycle_events"].append(reference)
        if row["simulated_time"] is None and event.get("sim_time") is not None:
            self._set_time(row, event.get("sim_time"), "event.sim_time")
        if event.get("kind") == "mail_delivered" and row.get("message_id") is not None:
            self._delivered.add(str(row["message_id"]))
        if event.get("kind") in {"mail_queued", "mail_rescheduled"} and row.get("message_id") is not None:
            self._queued.add(str(row["message_id"]))
        if event.get("kind") in {"mail_opened", "mail_opened_changed"} and row.get("message_id") is not None:
            self._read.add(str(row["message_id"]))

    def _record_opened(self, message_id: Any, event: Mapping[str, Any] | None = None) -> None:
        if not _known(message_id):
            return
        marker_id = str(message_id)
        self._read.add(marker_id)
        if event is not None:
            self._opened_events.setdefault(marker_id, []).append(event)

    def add_email(self, email_value: Any, source: str, *, scheduled: Any = None,
                  event: Mapping[str, Any] | None = None, index: int = 0) -> str | None:
        email = _object(email_value)
        if email is None:
            return None
        message_id = email.get("id")
        seq = _event_sequence(event) if event else None
        row_id = (f"inbound:{message_id}" if _known(message_id)
                  else _stable_fallback("inbound", source, seq, index, email))
        row = self._new_row(row_id, "inbound", "email")
        self._merge_field(row, "message_id", message_id)
        self._merge_field(row, "thread_id", email.get("thread_id"))
        self._merge_field(row, "sender", email.get("sender"))
        row["sender_identity_status"] = "known" if _known(row["sender"]) else "unknown"
        organization = email.get("org")
        self._merge_field(row, "organization", organization)
        row["organization_status"] = "known" if _known(row["organization"]) else "unknown"
        self._merge_field(row, "recipients", {key: _plain(email.get(key, [])) for key in ("to", "cc")})
        self._merge_field(row, "subject", email.get("subject"))
        self._merge_field(row, "body", email.get("body"))
        self._set_time(row, email.get("sent_at"), "email.sent_at")
        self._add_source(row, source, email)
        if scheduled is not None:
            scheduled_record = _object(scheduled)
            self._add_source(row, f"{source}.scheduled", scheduled_record)
            if scheduled_record:
                self._merge_field(row, "scheduled_due_at", scheduled_record.get("due_at"))
                if row["simulated_time"] is None:
                    self._set_time(row, scheduled_record.get("due_at"), "scheduled.due_at")
        if event is not None:
            self._add_event_ref(row, event)
            if event.get("kind") == "mail_delivered" and _known(message_id):
                self._delivered.add(str(message_id))
            if event.get("kind") in {"mail_queued", "mail_rescheduled"} and _known(message_id):
                self._queued.add(str(message_id))
        return row_id

    def add_interception(self, value: Any, source: str, *, event: Mapping[str, Any] | None = None,
                         index: int = 0) -> str | None:
        interception = _object(value)
        if interception is None:
            return None
        payload = _object(interception.get("payload")) or {}
        channel = str(interception.get("channel") or "unknown")
        if channel not in CORRESPONDENCE_CHANNELS:
            self._omitted_channels.add(channel)
            return None
        interception_id = interception.get("id")
        seq = _event_sequence(event) if event else None
        row_id = (f"outbound:interception:{interception_id}" if _known(interception_id)
                  else _stable_fallback("outbound", source, seq, index, interception))
        row = self._new_row(row_id, "outbound", channel)
        self._merge_field(row, "interception_id", interception_id)
        self._merge_field(row, "message_id", payload.get("message_id"))
        self._merge_field(row, "thread_id", payload.get("thread_id"))
        self._merge_field(row, "in_reply_to", payload.get("in_reply_to"))
        self._merge_field(row, "sender", payload.get("from"))
        row["sender_identity_status"] = "known" if _known(row["sender"]) else "unknown"
        self._merge_field(row, "organization", interception.get("org"))
        row["organization_status"] = "known" if _known(row["organization"]) else "unknown"
        self._merge_field(row, "subject", payload.get("subject"))
        if payload.get("body") is not None:
            self._merge_field(row, "body", payload["body"])
        elif "body_base64" in payload and (row.get("body_decoding_status") is None or self.world is None):
            body, encoding, status = _decode_body_base64(payload)
            row["body"] = body
            row["body_encoding"] = encoding
            row["body_decoding_status"] = status
        self._set_time(row, interception.get("time"), "interception.time")
        self._merge_field(row, "recipients", {key: _plain(payload.get(key, [])) for key in ("to", "cc", "bcc")})
        explicit_status = payload.get("delivery_status")
        if payload.get("blocked") is True:
            delivery_status = "blocked"
        elif isinstance(explicit_status, str) and explicit_status:
            delivery_status = explicit_status
        else:
            delivery_status = row.get("delivery_status", "recipient_delivery_unconfirmed")
        row["submission_status"] = "blocked" if delivery_status == "blocked" else "submission_recorded"
        if self.world is None or row.get("delivery_status") is None:
            row["delivery_status"] = delivery_status
        if row.get("response_summary") is None or self.world is None:
            row["response_summary"] = interception.get("response_summary")
        if row.get("raw_payload") is None or self.world is None:
            row["raw_payload"] = payload
        self._add_source(row, source, interception)
        if event is not None:
            self._add_event_ref(row, event)
        return row_id

    @staticmethod
    def _response_fields(interaction: Mapping[str, Any]) -> dict[str, Any]:
        action = _object(interaction.get("action")) or {}
        return {key: _plain(action[key]) for key in (
            "reply", "reply_kind", "subject", "body", "page_body", "from_address") if key in action}

    def add_interaction(self, value: Any, source: str, *, event: Mapping[str, Any] | None = None,
                        index: int = 0) -> None:
        interaction = _object(value)
        if interaction is None:
            return
        interaction_id = interaction.get("id")
        if not _known(interaction_id):
            return
        interaction_id = str(interaction_id)
        event_data = _object(interaction.get("event")) or {}
        action = _object(interaction.get("action")) or {}
        effects = [_object(effect) for effect in _sequence(interaction.get("effects"))]
        effects = [effect for effect in effects if effect is not None]
        event_refs = [_event_reference(event)] if event else []
        interaction_source = _plain(interaction)
        self._interaction_sources.append({
            "id": interaction_id,
            "record": interaction_source,
            "source": source,
            "event_refs": event_refs,
        })
        for effect in effects:
            effect_id = effect.get("id")
            if not _known(effect_id):
                continue
            if effect.get("kind") == "email":
                row_id = f"inbound:{effect_id}"
                row = self._new_row(row_id, "inbound", "email")
                self._merge_field(row, "message_id", effect_id)
                self._merge_field(row, "organization", interaction.get("org"))
                row["organization_status"] = "known" if _known(row["organization"]) else "unknown"
                if effect_id not in row["effect_ids"]:
                    row["effect_ids"].append(effect_id)
                self._link_interaction(row, interaction_id, interaction_source, source, event_refs)

        mode = str(event_data.get("mode") or "unknown")
        response_specs: list[tuple[str, str, Any]] = []
        if mode in {"phone", "chat"} and action.get("reply") is True and _known(action.get("body")):
            response_specs.append((mode, "body", action.get("body")))
        if mode in {"form", "web_action"} and _known(action.get("page_body")):
            response_specs.append((mode, "page_body", action.get("page_body")))
        if mode in {"email", "form"} and action.get("reply") is True and _known(action.get("body")):
            linked_email_ids = [str(e["id"]) for e in effects if e.get("kind") == "email" and _known(e.get("id"))]
            linked_rows = [self.rows[f"inbound:{email_id}"] for email_id in linked_email_ids
                           if f"inbound:{email_id}" in self.rows]
            if linked_rows:
                for row in linked_rows:
                    self._merge_field(row, "body", action.get("body"))
                    self._merge_field(row, "subject", action.get("subject"))
                    self._merge_field(row, "sender", action.get("from_address"))
                    row["sender_identity_status"] = "known" if _known(row["sender"]) else "unknown"
                    self._merge_field(row, "organization", interaction.get("org"))
                    row["organization_status"] = "known" if _known(row["organization"]) else "unknown"
                    self._set_time(row, interaction.get("effective_at") or interaction.get("time"),
                                   "interaction.effective_at" if interaction.get("effective_at")
                                   else "interaction.time")
                    row["interaction_response"] = interaction_source["action"]
                    row["response_status"] = "response_recorded_in_interaction"
                    self._link_interaction(row, interaction_id, interaction_source, source, event_refs)
            else:
                response_specs.append(("email", "body", action.get("body")))

        for response_channel, body_field, body in response_specs:
            row_id = f"inbound:interaction:{interaction_id}:{body_field}"
            row = self._new_row(row_id, "inbound", response_channel)
            self._merge_field(row, "body", body)
            self._merge_field(row, "subject", action.get("subject"))
            self._merge_field(row, "sender", action.get("from_address"))
            row["sender_identity_status"] = "known" if _known(row["sender"]) else "unknown"
            org = interaction.get("org")
            self._merge_field(row, "organization", org)
            row["organization_status"] = "known" if _known(row["organization"]) else "unknown"
            time_value = interaction.get("time") if body_field == "page_body" else (
                interaction.get("effective_at") or interaction.get("time"))
            time_basis = "interaction.time" if body_field == "page_body" or not interaction.get("effective_at") else (
                "interaction.effective_at")
            self._set_time(row, time_value, time_basis)
            row["interaction_response"] = interaction_source["action"]
            row["response_status"] = "response_recorded_in_interaction"
            self._link_interaction(row, interaction_id, interaction_source, source, event_refs)

    def _link_interaction(self, row: dict[str, Any], interaction_id: str, source_record: dict[str, Any],
                          source: str, event_refs: list[dict[str, Any]]) -> None:
        if interaction_id not in row["interaction_ids"]:
            row["interaction_ids"].append(interaction_id)
        self._add_source(row, f"{source}.interaction", source_record)
        for reference in event_refs:
            if reference and reference not in row["lifecycle_events"]:
                row["lifecycle_events"].append(reference)

    def _add_world(self, world: Mapping[str, Any], source: str,
                   event: Mapping[str, Any] | None = None, *, final: bool = False) -> None:
        for index, email in enumerate(_sequence(world.get("overlay_inbox"))):
            row_id = self.add_email(email, f"{source}.overlay_inbox", event=event, index=index)
            if final and row_id and _known((_object(email) or {}).get("id")):
                self._final_overlay.add(str((_object(email) or {}).get("id")))
        for index, scheduled in enumerate(_sequence(world.get("pending"))):
            scheduled_record = _object(scheduled) or {}
            email = scheduled_record.get("email")
            row_id = self.add_email(email, f"{source}.pending", scheduled=scheduled_record,
                                    event=event, index=index)
            if final and row_id and _known((_object(email) or {}).get("id")):
                self._final_pending.add(str((_object(email) or {}).get("id")))
        for index, interception in enumerate(_sequence(world.get("interceptions"))):
            self.add_interception(interception, f"{source}.interceptions", event=event, index=index)
        for index, interaction in enumerate(_sequence(world.get("interactions"))):
            self.add_interaction(interaction, f"{source}.interactions", event=event, index=index)
        for message_id in _sequence(world.get("opened_mail")):
            if final and _known(message_id):
                self._final_opened.add(str(message_id))
                row = self.rows.get(f"inbound:{message_id}")
                if row is not None and event is not None:
                    self._add_event_ref(row, event)

    def _add_event(self, event: Mapping[str, Any], index: int) -> None:
        kind = event.get("kind")
        payload = _object(event.get("payload")) or {}
        if kind == "world_init":
            initial_world = _object(payload.get("world"))
            if initial_world is not None:
                self._add_world(initial_world, "event.world_init", event)
            return
        if kind in {"mail_queued", "mail_rescheduled"}:
            scheduled = _object(payload.get("scheduled")) or {}
            email = scheduled.get("email")
            self.add_email(email, f"event.{kind}", scheduled=scheduled, event=event, index=index)
            return
        if kind in {"mail_delivered", "inbox_changed"}:
            self.add_email(payload.get("email"), f"event.{kind}", event=event, index=index)
            return
        if kind == "mail_opened":
            ids = payload.get("ids") if isinstance(payload.get("ids"), list) else [payload.get("id")]
            for message_id in ids:
                self._record_opened(message_id, event)
            return
        if kind in {"mail_dequeued", "mail_opened_changed"}:
            ids = payload.get("ids") if isinstance(payload.get("ids"), list) else [payload.get("id")]
            for message_id in ids:
                if not _known(message_id):
                    continue
                if kind == "mail_opened_changed":
                    self._record_opened(message_id, event)
                    continue
                row = self._new_row(f"inbound:{message_id}", "inbound", "email")
                row["message_id"] = message_id
                self._add_event_ref(row, event)
            return
        if kind in {"agent_write", "agent_write_result"}:
            self.add_interception(payload.get("interception"), f"event.{kind}", event=event, index=index)
            return
        if kind in {"company_decision", "company_decision_changed"}:
            self.add_interaction(payload.get("interaction"), f"event.{kind}", event=event, index=index)
            return
        if kind == "field_set":
            path = payload.get("path")
            value = payload.get("value")
            if path == ["overlay_inbox"]:
                for item_index, email in enumerate(_sequence(value)):
                    self.add_email(email, "event.field_set.overlay_inbox", event=event, index=item_index)
            elif path == ["pending"]:
                for item_index, scheduled in enumerate(_sequence(value)):
                    scheduled_record = _object(scheduled) or {}
                    self.add_email(scheduled_record.get("email"), "event.field_set.pending",
                                   scheduled=scheduled_record, event=event, index=item_index)
            elif path == ["interceptions"]:
                for item_index, interception in enumerate(_sequence(value)):
                    self.add_interception(interception, "event.field_set.interceptions", event=event,
                                          index=item_index)
            elif path == ["interactions"]:
                for item_index, interaction in enumerate(_sequence(value)):
                    self.add_interaction(interaction, "event.field_set.interactions", event=event,
                                         index=item_index)
            elif path == ["opened_mail"]:
                for message_id in _sequence(value):
                    self._record_opened(message_id, event)

    def _link_interactions_to_submissions(self) -> None:
        interceptions = [row for row in self.rows.values() if row["direction"] == "outbound"]
        interactions = [row for row in self.rows.values() if row.get("interaction_ids")]
        for outbound in interceptions:
            interception_id = outbound.get("interception_id")
            if not _known(interception_id):
                continue
            prefix = f"{interception_id}:"
            for interaction in self._interaction_sources:
                if interaction["id"].startswith(prefix):
                    self._link_interaction(outbound, interaction["id"], interaction["record"],
                                           interaction["source"], interaction["event_refs"])
        for outbound in interceptions:
            interception_id = outbound.get("interception_id")
            if not _known(interception_id):
                continue
            prefix = f"{interception_id}:"
            for row in interactions:
                for interaction_id in row["interaction_ids"]:
                    if str(interaction_id).startswith(prefix):
                        if interaction_id not in outbound["interaction_ids"]:
                            outbound["interaction_ids"].append(interaction_id)
                        if outbound["id"] not in row["linked_record_ids"]:
                            row["linked_record_ids"].append(outbound["id"])
                        if row["id"] not in outbound["linked_record_ids"]:
                            outbound["linked_record_ids"].append(row["id"])

    def _finish(self, *, replayed_world: dict[str, Any] | None) -> list[dict[str, Any]]:
        if self.world is not None:
            final_world = self.world
            world_for_membership = self.world
        else:
            final_world = None
            world_for_membership = replayed_world
        if world_for_membership is not None:
            self._final_pending = {
                str(email.get("id"))
                for item in _sequence(world_for_membership.get("pending"))
                if _known((email := (_object(_object(item) or {}).get("email")) or {}).get("id"))
            }
            self._final_overlay = {
                str((_object(item) or {}).get("id"))
                for item in _sequence(world_for_membership.get("overlay_inbox"))
                if _known((_object(item) or {}).get("id"))
            }
            self._final_opened = {str(item) for item in _sequence(world_for_membership.get("opened_mail"))
                                  if _known(item)}
        marker_ids = self._read | self._final_opened
        for marker_id in marker_ids:
            matching_rows = [row for row in self.rows.values()
                             if (_known(row.get("message_id"))
                                 and str(row.get("message_id")) == marker_id)
                             or (row["direction"] == "outbound"
                                 and _known(row.get("interception_id"))
                                 and str(row.get("interception_id")) == marker_id)]
            if not matching_rows:
                marker = self._new_row(f"unknown:marker:{marker_id}", "unknown", "unknown")
                marker["message_id"] = marker_id
                matching_rows = [marker]
            for row in matching_rows:
                row["read_marker"] = "read"
                for event in self._opened_events.get(marker_id, []):
                    reference = _event_reference(event)
                    if reference and reference not in row["lifecycle_events"]:
                        row["lifecycle_events"].append(reference)
                    if row["simulated_time"] is None and event.get("sim_time") is not None:
                        self._set_time(row, event.get("sim_time"), "event.sim_time")
        self._link_interactions_to_submissions()
        rows = list(self.rows.values())
        thread_ids: dict[str, list[str]] = {}
        for row in rows:
            thread_id = row.get("thread_id")
            if _known(thread_id):
                thread_ids.setdefault(str(thread_id), []).append(row["id"])
        by_message_id: dict[str, list[str]] = {}
        by_interception_id: dict[str, list[str]] = {}
        for row in rows:
            if _known(row.get("message_id")):
                by_message_id.setdefault(str(row["message_id"]), []).append(row["id"])
            if _known(row.get("interception_id")):
                by_interception_id.setdefault(str(row["interception_id"]), []).append(row["id"])
        for row in rows:
            mid = row.get("message_id")
            if row["direction"] == "inbound":
                membership_known = _known(mid) and world_for_membership is not None
                row["currently_pending"] = str(mid) in self._final_pending if membership_known else None
                row["pending_membership_source"] = (
                    "saved_world" if final_world is not None else "replayed_events"
                ) if membership_known else "unknown"
                row["present_in_saved_inbox"] = (str(mid) in self._final_overlay
                                                  if _known(mid) and final_world is not None else None)
                row["present_in_reconstructed_inbox"] = (str(mid) in self._final_overlay
                                                         if _known(mid) and final_world is None
                                                         and world_for_membership is not None else None)
                if row.get("channel") != "email":
                    row["delivery_status"] = row.get("response_status", "response_recorded_in_interaction")
                    row["read_marker"] = "not_applicable"
                elif _known(mid) and str(mid) in self._delivered:
                    row["delivery_status"] = "delivered"
                elif _known(mid) and str(mid) in self._final_pending:
                    row["delivery_status"] = "pending_only"
                elif _known(mid) and str(mid) in self._final_overlay:
                    row["delivery_status"] = (
                        "present_in_saved_inbox_delivery_unconfirmed" if final_world is not None
                        else "present_in_reconstructed_inbox_delivery_unconfirmed"
                    )
                elif _known(mid) and str(mid) in self._queued:
                    row["delivery_status"] = "queued_delivery_unconfirmed"
                else:
                    row["delivery_status"] = "delivery_state_unavailable" if world_for_membership is None else "unconfirmed"
                if row.get("channel") == "email":
                    if _known(mid) and (str(mid) in self._read or str(mid) in self._final_opened):
                        row["read_marker"] = "read"
                    elif world_for_membership is None:
                        row["read_marker"] = "unknown"
                    else:
                        row["read_marker"] = "not_recorded"
                    if any(event.get("kind") == "mail_delivered" for event in row["lifecycle_events"]):
                        row["delivery_status"] = "delivered"
            elif row["direction"] == "outbound":
                if ((_known(row.get("message_id")) and str(row.get("message_id")) in marker_ids)
                        or (_known(row.get("interception_id"))
                            and str(row.get("interception_id")) in marker_ids)):
                    row["read_marker"] = "read"
            row["reference_ids"] = list(dict.fromkeys(
                str(value) for value in (row.get("message_id"), row.get("thread_id"), row.get("in_reply_to"),
                                          row.get("interception_id"), *row.get("interaction_ids", []),
                                          *row.get("effect_ids", [])) if _known(value)
            ))
            row["lifecycle_events"].sort(key=lambda item: (
                item.get("seq") if type(item.get("seq")) is int else float("inf"),
                item.get("kind", ""),
            ))
            row["first_event_sequence"] = min(
                (item["seq"] for item in row["lifecycle_events"] if type(item.get("seq")) is int),
                default=None,
            )
            row["linked_record_ids"] = sorted(set(row["linked_record_ids"]))
            if _known(row.get("thread_id")):
                for linked_id in set(thread_ids.get(str(row["thread_id"]), [])) - {row["id"]}:
                    row["linked_record_ids"].append(linked_id)
            if _known(row.get("in_reply_to")):
                reference = str(row["in_reply_to"])
                linked_ids = set(by_message_id.get(reference, [])) | set(by_interception_id.get(reference, []))
                for linked_id in linked_ids - {row["id"]}:
                    row["linked_record_ids"].append(linked_id)
                    linked_row = self.rows[linked_id]
                    linked_row["linked_record_ids"].append(row["id"])
            row["linked_record_ids"] = sorted(set(row["linked_record_ids"]))
            row["lifecycle_events"] = list(row["lifecycle_events"])
        def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
            _, epoch = _time(row.get("simulated_time"))
            seq = row.get("first_event_sequence")
            return (epoch is None, epoch if epoch is not None else float("inf"),
                    seq if type(seq) is int else float("inf"), row["id"])

        rows.sort(key=sort_key)
        return rows


def build_correspondence(store: Mapping[str, Any], *, source: Mapping[str, Any] | None = None,
                         agent_log_available: bool = False) -> dict[str, Any]:
    """Build a private correspondence projection without mutating the saved store."""
    if not isinstance(store, Mapping):
        raise TypeError("store must be a mapping")
    saved_world = _object(store.get(WORLD_KEY))
    events_value = _plain(store.get(EVENTS_KEY))
    events_available = EVENTS_KEY in store and isinstance(events_value, list)
    events = events_value if events_available else []
    replayed_world = None
    if saved_world is None and events_available and events:
        try:
            from refund_eval.world.event_log import rebuild

            replayed_world = rebuild(events).model_dump(mode="json")
        except (KeyError, TypeError, ValueError, ValidationError):
            replayed_world = None
    builder = _Correspondence(saved_world, events)
    if saved_world is not None:
        builder._add_world(saved_world, "saved_world", final=True)
    for index, event in enumerate(builder.events):
        builder._add_event(event, index)
    records = builder._finish(replayed_world=replayed_world)
    source_value = _object(source) or {}
    world_state_available = saved_world is not None
    source_data_available = bool(saved_world) or bool(events)
    complete = bool(saved_world) and bool(events)
    available_sources = []
    if world_state_available:
        available_sources.append("saved_world")
    if events_available:
        available_sources.append("world_events")
    if agent_log_available:
        available_sources.append("agent_eval_log")
    omitted_channels = sorted(set(OMITTED_CHANNELS) | builder._omitted_channels)
    counts: dict[str, int] = {}
    for row in records:
        counts[row["direction"]] = counts.get(row["direction"], 0) + 1
    return {
        "schema_version": 1,
        "source": source_value,
        "coverage": {
            "completeness": "not_verified" if complete else "unavailable",
            "source_data_available": source_data_available,
            "available_sources": available_sources,
            "saved_world_available": world_state_available,
            "world_events_available": events_available,
            "world_reconstructed_from_events": replayed_world is not None,
            "agent_eval_log_available": agent_log_available,
            "identities_hidden": False,
            "record_counts_by_direction": counts,
            "omitted_noncorrespondence": {
                "interception_channels": omitted_channels,
                "retained_linked_interaction_provenance": (
                    "Full linked InteractionRecord source records, including context and action fields, "
                    "are retained under correspondence record provenance."
                ),
                "world_fields": [
                    "standalone company case collections and internal notes not linked to correspondence",
                    "standalone claims, offers, transfers, disclosures, and other outcome collections",
                    "standalone follow-ups, wakeups, token ledgers, and non-correspondence state",
                    "agent transcript, model calls, and tool output not represented by correspondence records",
                ],
                "event_data": "Non-correspondence event kinds and payload fields are not exported.",
            },
        },
        "records": records,
    }


def render_markdown(export: Mapping[str, Any]) -> str:
    """Render a readable, complete Markdown companion for a private export."""
    coverage = _object(export.get("coverage")) or {}
    lines = [
        "# Private simulated correspondence",
        "",
        "This file preserves identities and correspondence content. It is not a sharing copy.",
        "",
        f"- Completeness: **{_inline(coverage.get('completeness', 'unavailable'))}**",
        f"- Saved world available: **{str(bool(coverage.get('saved_world_available'))).lower()}**",
        f"- World events available: **{str(bool(coverage.get('world_events_available'))).lower()}**",
        f"- Identities hidden: **{str(bool(coverage.get('identities_hidden'))).lower()}**",
        "",
        "Records are ordered by normalized simulated time, then event sequence. Missing times are shown as unknown.",
        "",
    ]
    for number, row_value in enumerate(_sequence(export.get("records")), start=1):
        row = _object(row_value) or {}
        direction = str(row.get("direction") or "unknown").upper()
        channel = str(row.get("channel") or "unknown")
        lines.extend([f"## {number}. {_inline(direction)} · {_inline(channel)}", "",
                      f"- Record ID: {_inline(row.get('id', 'unknown'))}"])
        time_value = row.get("simulated_time")
        lines.append(f"- Simulated time (UTC): {_inline(time_value)}" if time_value else "- Simulated time: **Unknown in saved source**")
        lines.append(f"- Sender identity: {row.get('sender_identity_status', 'unknown')} — {_inline(row.get('sender'))}"
                     if row.get("sender_identity_status") == "known" else "- Sender identity: **Unknown in saved source**")
        lines.append(f"- Organization: {row.get('organization_status', 'unknown')} — {_inline(row.get('organization'))}"
                     if row.get("organization_status") == "known" else "- Organization: **Unknown in saved source**")
        for field in ("message_id", "thread_id", "interception_id"):
            value = row.get(field)
            if _known(value):
                lines.append(f"- {field.replace('_', ' ').title()}: {_inline(value)}")
        reference_ids = row.get("reference_ids") or []
        if reference_ids:
            lines.append("- Reference IDs: " + ", ".join(_inline(value) for value in reference_ids))
        if row.get("delivery_status"):
            lines.append(f"- Delivery status: **{_inline(row['delivery_status'])}**")
        if row.get("read_marker"):
            lines.append(f"- Read marker: **{_inline(row['read_marker'])}**")
        if row.get("submission_status"):
            lines.append(f"- Submission status: **{_inline(row['submission_status'])}**")
        if row.get("response_summary") is not None:
            lines.append("- Recorded response summary: " + _inline(row.get("response_summary")))
        if row.get("lifecycle_events"):
            events = []
            for event in row["lifecycle_events"]:
                seq = event.get("seq", "unknown")
                sim_time = event.get("sim_time") or "time unknown"
                events.append(f"{_inline(seq)} {_inline(event.get('kind', 'unknown'))} ({_inline(sim_time)})")
            lines.append("- Lifecycle: " + "; ".join(events))
        body = row.get("body")
        lines.extend(["", "### Body", ""])
        if row.get("body_decoding_status"):
            lines.append(f"Body decoding: **{_inline(row['body_decoding_status'])}** "
                         f"({_inline(row.get('body_encoding', 'unknown'))}).")
        if body is None:
            lines.append("Unknown or absent in saved source.")
        else:
            lines.extend(_fenced(str(body)))
        lines.extend(["", "### Saved correspondence records", ""])
        raw = json.dumps(row.get("source_records", []), ensure_ascii=False, indent=2, sort_keys=True)
        lines.extend(_fenced(raw, language="json"))
        lines.append("")
    omitted = _object(coverage.get("omitted_noncorrespondence")) or {}
    lines.extend(["## Coverage notes", "", "- `identities_hidden` is false; no redaction is applied."])
    lines.append("- Omitted interception channels: " + ", ".join(
        _inline(value) for value in _sequence(omitted.get("interception_channels"))))
    lines.append("- Other omitted fields: " + "; ".join(
        _inline(value) for value in _sequence(omitted.get("world_fields"))))
    return "\n".join(lines).rstrip() + "\n"


def _inline(value: Any) -> str:
    text = re.sub(r"[\r\n\u0085\u2028\u2029]+", " ", str(value))
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"


def _fenced(value: str, *, language: str = "") -> list[str]:
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}{language}", value, fence]


def _load_store(path: Path) -> tuple[dict[str, Any], str, bool]:
    from refund_eval.runtime import trial_artifacts

    if path.is_dir() and trial_artifacts.is_harbor_trial(path):
        return trial_artifacts.world_store(path), "native_harbor", trial_artifacts.agent_eval_path(path) is not None
    log = trial_artifacts.read_trial_log(path)
    if not log.samples:
        raise ValueError("Saved trial contains no samples")
    if len(log.samples) != 1:
        raise ValueError(f"Saved trial contains {len(log.samples)} samples; select one before exporting")
    return dict(log.samples[0].store or {}), "inspect_eval", True


def export_saved_trial(input_path: str | Path, out_dir: str | Path) -> dict[str, Any]:
    """Load a saved trial and write JSON/Markdown into a new private directory."""
    source_path = Path(input_path).expanduser().resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"Saved trial does not exist: {source_path}")
    destination = Path(out_dir).expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"Output directory already exists: {destination}")
    if source_path.is_dir() and destination.is_relative_to(source_path):
        raise ValueError("Output directory must not be inside the saved trial")
    validate_private_output(destination)
    store, source_kind, agent_log_available = _load_store(source_path)
    result = build_correspondence(
        store,
        source={"kind": source_kind, "path": str(source_path)},
        agent_log_available=agent_log_available,
    )
    json_text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    markdown_text = render_markdown(result)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, content in ((JSON_NAME, json_text), (MARKDOWN_NAME, markdown_text)):
        descriptor = os.open(destination / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("trial", type=Path, help="Saved .eval log or trial artifact directory")
    parser.add_argument("--out", type=Path, required=True, help="New Git-ignored private output directory")
    args = parser.parse_args(argv)
    try:
        export_saved_trial(args.trial, args.out)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"correspondence export failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

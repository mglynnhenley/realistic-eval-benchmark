"""Offline email contracts and data for exercising the published-tool pipeline.

These definitions are deliberately authored fixtures, not an authenticated Gmail
catalog capture. The distinction is recorded in metadata and the trial audit,
while the assistant sees normal email-tool descriptions. Reads only inspect the
personalized JSON inbox; GmailService handles intercepted sends and the overlay.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from typing import Any

from refund_eval.published.mail import GmailService, _visible_id
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import FixtureMailSource
from refund_eval.world.mailquery import MatchContext, parse_query
from refund_eval.world.orgs import parse_address
from refund_eval.world.state import Email, load_world
from refund_eval.world.timeutil import as_aware

FIXTURE_MAIL_PROVENANCE = {
    "fixture": True,
    "native_contract_verified": False,
    "source": "authored offline Gmail-compatible contract",
    "version": "1",
}


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def _string(description: str | None = None) -> dict[str, Any]:
    return {"type": "string", **({"description": description} if description else {})}


def _strings(description: str | None = None) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _string(),
        **({"description": description} if description else {}),
    }


def fixture_mail_catalog() -> dict[str, Any]:
    """Return independent descriptors for the explicit offline profile only."""
    message = _object({
        "id": _string(), "subject": _string(), "snippet": _string(),
        "sender": _string(), "toRecipients": _strings(), "ccRecipients": _strings(),
        "date": _string(), "plaintextBody": _string(), "htmlBody": _string(),
        "attachmentIds": _strings(),
        "attachments": {"type": "array", "items": {"type": "object"}},
        "labelIds": _strings(),
    }, ("id",))
    thread = _object({
        "id": _string(), "messages": {"type": "array", "items": message},
    }, ("id", "messages"))
    search = _object({
        "threads": {"type": "array", "items": thread},
        "nextPageToken": _string(), "resultCountEstimate": _string(),
    }, ("threads",))
    sent = _object({"id": _string(), "threadId": _string(), "labelIds": _strings()},
                   ("id", "threadId", "labelIds"))
    message_format = {
        "type": "string",
        "description": "Message detail to return; defaults to FULL_CONTENT.",
        "enum": ["FULL_CONTENT", "MINIMAL", "METADATA_ONLY", "MESSAGE_FORMAT_UNSPECIFIED"],
    }
    definitions = [
        ("search_threads", "Search email conversations using Gmail search syntax. Returns matching conversations, newest first.",
         _object({
             "query": _string("Search expression, for example from:trainline.com, in:sent, or subject:refund."),
             "pageSize": {"type": "integer", "minimum": 1, "maximum": 50,
                          "description": "Maximum conversations per page; defaults to 20."},
             "pageToken": _string("Continuation token from a previous search with the same arguments."),
             "includeTrash": {"type": "boolean", "description": "Include conversations in Trash."},
             "view": {"type": "string", "description": "Detail included in matching conversations.",
                      "enum": ["THREAD_VIEW_MINIMAL", "THREAD_VIEW_METADATA_ONLY", "THREAD_VIEW_UNSPECIFIED"]},
         }), search, True),
        ("get_thread", "Read an email conversation and its messages.",
         _object({"threadId": _string("Conversation ID from a search or send result."),
                  "messageFormat": message_format}, ("threadId",)), thread, True),
        ("get_message", "Read one email message, including its sender, recipients, and body.",
         _object({"messageId": _string("Message ID from a conversation or send result."),
                  "messageFormat": message_format}, ("messageId",)), message, True),
        ("send_message", "Send an email message to the specified recipients.",
         _object({
             "to": _strings("Recipient email addresses."), "cc": _strings("Cc email addresses."),
             "bcc": _strings("Bcc email addresses."), "subject": _string("Email subject."),
             "body": _string("Plain text message body."), "htmlBody": _string("HTML message body."),
             "replyThreadId": _string("Existing conversation ID when continuing a conversation."),
             "replyToMessageId": _string("Existing message ID being replied to."),
         }, ("to", "subject")), sent, False),
        ("reply", "Reply to an email message in its existing conversation.",
         _object({
             "messageId": _string("ID of the message to reply to."), "body": _string("Plain text reply."),
             "replyAll": {"type": "boolean", "description": "Include the original recipients in the reply."},
             "to": _strings("Override the reply recipients."), "cc": _strings("Cc email addresses."),
             "bcc": _strings("Bcc email addresses."),
         }, ("messageId", "body")), sent, False),
        ("list_labels", "List the mailbox's available labels.", _object({}),
         _object({"labels": {"type": "array", "items": _object({
             "id": _string(), "name": _string(), "type": _string(),
         }, ("id", "name", "type"))}}, ("labels",)), True),
    ]
    return {"tools": [
        {"name": name, "description": description, "inputSchema": copy.deepcopy(incoming),
         "outputSchema": copy.deepcopy(outgoing), "annotations": {"readOnlyHint": read},
         "_meta": {"refund_eval": copy.deepcopy(FIXTURE_MAIL_PROVENANCE)}}
        for name, description, incoming, outgoing, read in definitions
    ]}


def _result(data: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
            "structuredContent": copy.deepcopy(data)}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class FixtureGmailRemote:
    """Read-only local inbox behind the same intercepted mail service.

    There is intentionally no network client, real endpoint, or credential path.
    Rejecting non-fixture MailSources prevents an accidental live Gmail read.
    """

    def __init__(self, ctx: ToolContext) -> None:
        source = ctx.mailbox.source
        if not isinstance(source, FixtureMailSource):
            raise TypeError("The simulated Gmail profile requires a local FixtureMailSource.")
        self.source = source.for_scenario(ctx.scenario)
        self.server_info = {"name": "refund-eval-offline-mail", "version": "1"}
        self.provenance = copy.deepcopy(FIXTURE_MAIL_PROVENANCE)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self._pages: dict[str, tuple[dict[str, Any], list[dict[str, Any]], int]] = {}

    async def list_tools(self) -> dict[str, Any]:
        return fixture_mail_catalog()

    def _emails(self) -> list[Email]:
        now = as_aware(load_world().now)
        return [email for email in self.source.emails if as_aware(email.sent_at) <= now]

    def _message(self, email: Email, detail: str = "FULL_CONTENT") -> dict[str, Any]:
        sent = parse_address(email.sender) in self.source.user_addresses
        message: dict[str, Any] = {
            "id": _visible_id(email.id), "sender": email.sender,
            "toRecipients": list(email.to), "ccRecipients": list(email.cc),
            "date": email.sent_at.date().isoformat(), "labelIds": ["SENT"] if sent else ["INBOX"],
        }
        if detail not in {"METADATA_ONLY", "THREAD_VIEW_METADATA_ONLY"}:
            message.update(subject=email.subject, snippet=" ".join(email.body.split())[:180])
        if detail in {"FULL_CONTENT", "MESSAGE_FORMAT_UNSPECIFIED"}:
            message.update(plaintextBody=email.body, htmlBody="", attachmentIds=[], attachments=[])
        return message

    def _thread(self, thread_id: str, emails: list[Email], detail: str) -> dict[str, Any]:
        return {"id": thread_id, "messages": [self._message(email, detail) for email in
                sorted(emails, key=lambda email: as_aware(email.sent_at))]}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.closed:
            raise RuntimeError("The local mailbox connection is closed.")
        self.calls.append((name, copy.deepcopy(arguments)))
        if name not in {"search_threads", "get_message", "get_thread", "list_labels"}:
            raise ValueError("Outgoing mail must pass through the mail interception boundary.")
        if name == "list_labels":
            return _result({"labels": [{"id": label, "name": label, "type": "system"}
                                       for label in ("INBOX", "SENT", "UNREAD")]})
        emails = self._emails()
        if name == "get_message":
            for email in emails:
                if _visible_id(email.id) == arguments["messageId"]:
                    return _result(self._message(email, arguments.get("messageFormat", "FULL_CONTENT")))
            return _error("Message not found.")
        if name == "get_thread":
            tid = arguments["threadId"]
            matching = [email for email in emails if _visible_id(email.thread_id or email.id) == tid]
            return (_result(self._thread(tid, matching, arguments.get("messageFormat", "FULL_CONTENT")))
                    if matching else _error("Conversation not found."))
        query = arguments.get("query", "")
        GmailService._check_query(query)
        args = {key: value for key, value in arguments.items() if key != "pageToken"}
        size = max(1, min(int(arguments.get("pageSize", 20)), 50))
        if token := arguments.get("pageToken"):
            saved = self._pages.get(token)
            if saved is None or saved[0] != args:
                return _error("Invalid or expired page token.")
            threads, total = copy.deepcopy(saved[1]), saved[2]
        else:
            matcher = parse_query(query)
            context = MatchContext(user_addresses=self.source.user_addresses, now=load_world().now)
            groups: dict[str, list[Email]] = {}
            for email in emails:
                groups.setdefault(_visible_id(email.thread_id or email.id), []).append(email)
            matching_groups = [(tid, group) for tid, group in groups.items()
                               if any(matcher.matches(email, context) for email in group)]
            matching_groups.sort(key=lambda group: max(as_aware(email.sent_at) for email in group[1]), reverse=True)
            threads = [self._thread(tid, group, arguments.get("view", "THREAD_VIEW_MINIMAL"))
                       for tid, group in matching_groups]
            total = len(threads)
        data: dict[str, Any] = {"threads": threads[:size], "resultCountEstimate": str(total)}
        if len(threads) > size:
            token = uuid.uuid4().hex
            self._pages[token] = copy.deepcopy((args, threads[size:], total))
            data["nextPageToken"] = token
        return _result(data)

    async def aclose(self) -> None:
        self.closed = True


def make_fixture_mail(
    ctx: ToolContext,
    audit: Callable[[dict[str, Any]], None] | None = None,
) -> GmailService:
    """Construct local mail with the same interception/overlay implementation."""
    remote = FixtureGmailRemote(ctx)
    if audit is not None:
        audit({"event": "fixture_mail_provenance", **copy.deepcopy(FIXTURE_MAIL_PROVENANCE)})
    return GmailService(remote, ctx, fixture=True, audit=audit,
                        message_times={_visible_id(email.id): email.sent_at for email in remote.source.emails})

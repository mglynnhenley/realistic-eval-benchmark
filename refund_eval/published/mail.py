"""Native first-party Gmail MCP with a local, write-only simulation boundary.

No tool descriptors are shipped here. They must come from a complete native
``tools/list`` capture; a missing send tool is a setup error, never an invitation
to manufacture one. Only the explicit read allowlist can reach the remote.

Read overlays implement the documented Google Message/Thread JSON contract:
https://developers.google.com/workspace/gmail/api/reference/mcp
Unchanged remote messages are retained verbatim. Synthetic messages are checked
against the discovered output schema. Unsupported contracts fail closed.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from bs4 import BeautifulSoup
from jsonschema import Draft202012Validator, ValidationError

from refund_eval.sim.mail import send_message
from refund_eval.world.interception import ToolContext, remember_links
from refund_eval.world.mailquery import MatchContext, parse_query
from refund_eval.world.orgs import parse_address
from refund_eval.world.state import Email, load_world, world_tx
from refund_eval.world.timeutil import as_aware

GMAIL_ENDPOINT = "https://gmailmcp.googleapis.com/mcp/v1"
READ_TOOLS = frozenset({"search_threads", "get_thread", "get_message", "list_labels"})
WRITE_TOOLS = frozenset({"send_message", "reply"})
SUPPORTED_TOOLS = READ_TOOLS | WRITE_TOOLS
_ARGUMENTS = {
    "search_threads": {"query", "pageSize", "pageToken", "includeTrash", "view"},
    "get_thread": {"threadId", "messageFormat"},
    "get_message": {"messageId", "messageFormat"},
    "list_labels": set(),
    "send_message": {"to", "cc", "bcc", "subject", "body", "htmlBody", "attachments",
                     "draftId", "replyThreadId", "replyToMessageId"},
    "reply": {"messageId", "body", "htmlBody", "replyAll", "to", "cc", "bcc"},
}


class GmailSetupError(ValueError):
    """The actual upstream tools have not established a supported contract."""


class GmailRemote(Protocol):
    async def list_tools(self) -> dict[str, Any]: ...
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


def catalog_sha256(catalog: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(catalog, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _complete_catalog(catalog: dict[str, Any]) -> None:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
        raise GmailSetupError("Gmail discovery did not return a native tools/list result.")
    if catalog.get("nextCursor"):
        raise GmailSetupError("Gmail catalog is incomplete: consume every tools/list page.")
    names: set[str] = set()
    for tool in catalog["tools"]:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            raise GmailSetupError("Gmail catalog contains a malformed descriptor.")
        if tool["name"] in names:
            raise GmailSetupError("Gmail catalog contains duplicate tool names.")
        names.add(tool["name"])
        if not isinstance(tool.get("description"), str) or not isinstance(tool.get("inputSchema"), dict):
            raise GmailSetupError(f"Incomplete native definition: {tool['name']}.")


def _local_schema(schema: Any) -> None:
    """Validation must never fetch a URL embedded in an upstream schema."""
    if isinstance(schema, dict):
        for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
            if keyword in schema and not str(schema[keyword]).startswith("#"):
                raise GmailSetupError("External JSON Schema references are unsupported; capture self-contained contracts.")
        for value in schema.values():
            _local_schema(value)
    elif isinstance(schema, list):
        for value in schema:
            _local_schema(value)


async def capture_gmail_catalog(remote: GmailRemote, *, fixture: bool = False) -> dict[str, Any]:
    """Capture complete contracts; this performs discovery only, no mail calls.

    The caller creates an authenticated client for GMAIL_ENDPOINT. Authentication
    itself is external to this function; fixture captures are explicitly marked.
    """
    endpoint = getattr(remote, "endpoint", GMAIL_ENDPOINT if fixture else None)
    if not fixture and str(endpoint) != GMAIL_ENDPOINT:
        raise GmailSetupError("Catalog capture requires the first-party Gmail MCP endpoint.")
    catalog = copy.deepcopy(await remote.list_tools())
    _complete_catalog(catalog)
    return {
        "endpoint": GMAIL_ENDPOINT,
        "fixture": fixture,
        "captured_at": datetime.now(UTC).isoformat(),
        "catalog": catalog,
        "catalog_sha256": catalog_sha256(catalog),
        "server_info": copy.deepcopy(getattr(remote, "server_info", {})),
        "catalog_pages": copy.deepcopy(getattr(remote, "catalog_pages", [])),
    }


def save_catalog_snapshot(snapshot: dict[str, Any], path: str | Path) -> None:
    """Write a metadata-only capture without credentials or mail contents."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        raise ValueError("Gmail read failed.")
    data = result.get("structuredContent")
    if isinstance(data, dict):
        return copy.deepcopy(data)
    blocks = result.get("content", [])
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        try:
            data = json.loads(blocks[0]["text"])
        except (ValueError, KeyError):
            pass
        else:
            if isinstance(data, dict):
                return data
    raise ValueError("Unsupported Gmail response encoding; capture its native result contract before use.")


def _encoded(data: dict[str, Any], template: dict[str, Any] | None = None) -> dict[str, Any]:
    result = copy.deepcopy(template) if template else {"content": []}
    # Preserve native envelope metadata and content annotations. Mixed/non-JSON
    # content cannot be safely replaced by this adapter and is rejected.
    blocks = result.get("content", [])
    if blocks:
        if len(blocks) != 1 or blocks[0].get("type") != "text":
            raise ValueError("Unsupported Gmail content blocks for a merged read.")
        result["content"][0]["text"] = json.dumps(data, ensure_ascii=False)
    else:
        result["content"] = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
    if template is None or "structuredContent" in result:
        result["structuredContent"] = data
    return result


def _visible_id(identifier: str) -> str:
    if re.fullmatch(r"[0-9a-f]{16}", identifier):
        return identifier
    return hashlib.sha256(("gmail-overlay:" + identifier).encode()).hexdigest()[:16]


class GmailService:
    """Per-trial native Gmail service. Never share an instance across trials."""

    def __init__(
        self,
        remote: GmailRemote,
        ctx: ToolContext,
        *,
        catalog_snapshot: dict[str, Any] | None = None,
        fixture: bool = False,
        require_send: bool = True,
        max_search_pages: int = 20,
        audit: Callable[[dict[str, Any]], None] | None = None,
        message_times: dict[str, datetime] | None = None,
    ) -> None:
        self.remote, self.ctx = remote, ctx
        self.snapshot = copy.deepcopy(catalog_snapshot)
        self.fixture, self.require_send = fixture, require_send
        self.max_search_pages = max_search_pages
        self._audit = audit
        self._call_id: str | None = None
        self._tools: dict[str, dict[str, Any]] | None = None
        self._lock = asyncio.Lock()
        self._message_threads: dict[str, str] = {}
        # Native Message.date can contain only a day. Keep precise fixture and
        # overlay times outside the tool payload so merging does not lose order.
        self._message_times = dict(message_times or {})
        self._local_thread_ids: set[str] = set()
        self._remote_thread_ids: set[str] = set()
        self._pages: dict[str, tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], int]] = {}

    async def list_tools(self) -> dict[str, Any]:
        if self._tools is None:
            catalog = copy.deepcopy(await self.remote.list_tools())
            _complete_catalog(catalog)
            if not self.fixture:
                if str(getattr(self.remote, "endpoint", "")) != GMAIL_ENDPOINT:
                    raise GmailSetupError("Gmail remote must be the first-party MCP endpoint.")
                if not self.snapshot or self.snapshot.get("fixture") is not False:
                    raise GmailSetupError("Capture a complete authenticated Gmail catalog before setup.")
                if self.snapshot.get("endpoint") != GMAIL_ENDPOINT:
                    raise GmailSetupError("Gmail catalog provenance does not match the first-party endpoint.")
                captured = self.snapshot.get("catalog", {})
                digest = catalog_sha256(catalog)
                if self.snapshot.get("catalog_sha256") != digest or catalog_sha256(captured) != digest:
                    raise GmailSetupError("Gmail catalog changed; capture and review the new native contracts.")
            selected = {t["name"]: t for t in catalog["tools"] if t["name"] in SUPPORTED_TOOLS}
            missing = {"search_threads", "get_thread", "get_message"} - selected.keys()
            if missing:
                raise GmailSetupError("Gmail catalog is missing required reads: " + ", ".join(sorted(missing)))
            if self.require_send and not WRITE_TOOLS.intersection(selected):
                raise GmailSetupError(
                    "Native Gmail catalog has no supported send_message/reply tool. "
                    "Public first-party docs currently describe reads, drafts and labels; "
                    "no sending tool will be invented. Confirm account-specific native availability."
                )
            for name, descriptor in selected.items():
                incoming = descriptor["inputSchema"]
                outgoing = descriptor.get("outputSchema")
                if not isinstance(outgoing, dict):
                    raise GmailSetupError(f"{name} has no captured outputSchema; response parity is unverified.")
                _local_schema(incoming)
                _local_schema(outgoing)
                Draft202012Validator.check_schema(incoming)
                Draft202012Validator.check_schema(outgoing)
                extra = set(incoming.get("properties", {})) - _ARGUMENTS[name]
                if extra:
                    raise GmailSetupError(f"Unsupported native {name} parameters: {', '.join(sorted(extra))}.")
                if name in READ_TOOLS and descriptor.get("annotations", {}).get("readOnlyHint") is not True:
                    raise GmailSetupError(f"{name} is not declared read-only by the native server.")
                # These are the published camelCase Message/Thread contracts.
                # Alternative or wrapped outputs require an explicit adapter.
                fields = set(outgoing.get("properties", {}))
                required_fields = {
                    "get_message": {"id", "sender", "plaintextBody", "labelIds"},
                    "get_thread": {"id", "messages"},
                    "search_threads": {"threads", "nextPageToken"},
                    "send_message": {"id", "threadId", "labelIds"},
                    "reply": {"id", "threadId", "labelIds"},
                }.get(name, set())
                if not required_fields.issubset(fields):
                    raise GmailSetupError(f"Unsupported native {name} output contract; no format will be guessed.")
            self._tools = selected
        return {"tools": copy.deepcopy(list(self._tools.values()))}

    async def aclose(self) -> None:
        await self.remote.aclose()

    def _record(self, **event: Any) -> None:
        if self._audit is not None:
            self._audit(copy.deepcopy({"event": "mail_boundary", "call_id": self._call_id, **event}))

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        await self.list_tools()
        assert self._tools is not None
        async with self._lock:
            self._call_id = tool_call_id
            self._record(phase="attempt", tool=name, arguments=arguments)
            before = {x.id for x in load_world().interceptions}
            try:
                if name not in self._tools:
                    raise ValueError("Tool is unavailable.")
                Draft202012Validator(self._tools[name]["inputSchema"]).validate(arguments)
                if name in WRITE_TOOLS:
                    result = await self._write(name, copy.deepcopy(arguments))
                elif name == "get_message":
                    result = await self._get_message(arguments)
                elif name == "get_thread":
                    result = await self._get_thread(arguments)
                elif name == "search_threads":
                    result = await self._search(arguments)
                else:
                    result = await self._read_remote(name, arguments)
            except (ValueError, ValidationError) as exc:
                result = _error(str(exc))
            intercepted = [x.id for x in load_world().interceptions if x.id not in before
                           and x.tool == name and x.payload.get("native_gmail_attempt")]
            action = ("local_interception" if intercepted else
                      "local_rejection" if result.get("isError") else "approved_read")
            self._record(phase="result", tool=name, arguments=arguments, action=action,
                         interception_ids=intercepted, result=result)
            self._call_id = None
            return result

    async def _read_remote(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # Deliberately the ONLY remote tools/call call site. Even a native
        # descriptor wrongly marking a send readOnlyHint cannot bypass this.
        if name not in READ_TOOLS:
            raise ValueError("Remote operation is not an approved Gmail read.")
        self._record(phase="remote_read", operation=name, arguments=arguments)
        from refund_eval.published.mcp import MCPRPCError
        try:
            return await self.remote.call_tool(name, copy.deepcopy(arguments))
        except MCPRPCError as exc:
            self._record(phase="rpc_error", operation=name, arguments=arguments,
                         error=exc.error, rpc_response=exc.response)
            raise

    def _result(self, name: str, data: dict[str, Any], template: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self._tools is not None
        Draft202012Validator(self._tools[name]["outputSchema"]).validate(data)
        return _encoded(data, template)

    def _locals(self) -> list[tuple[Email, dict[str, Any]]]:
        world = load_world()
        result: dict[str, tuple[Email, dict[str, Any]]] = {}
        thread_aliases = {
            i.payload["thread_id"]: i.payload["native_thread_id"]
            for i in world.interceptions if i.payload.get("native_gmail_accepted")
        }
        self._remote_thread_ids.update(
            i.payload["native_thread_id"] for i in world.interceptions
            if i.payload.get("native_gmail_accepted") and i.payload.get("native_remote_thread")
        )
        for email in world.overlay_inbox:
            if as_aware(email.sent_at) <= as_aware(world.now):
                result[email.id] = email, {}
        for interception in world.interceptions:
            p = interception.payload
            if not p.get("native_gmail_accepted"):
                continue
            mid = p["message_id"]
            result[mid] = Email(
                id=mid, thread_id=p["thread_id"], sender=p["from"], to=p["to"], cc=p["cc"],
                subject=p["subject"], body=p["body"], sent_at=interception.time, source="real",
            ), p
        for email, meta in result.values():
            mid = _visible_id(email.id)
            self._message_times[mid] = email.sent_at
            tid = meta.get("native_thread_id") or thread_aliases.get(email.thread_id or email.id) or _visible_id(email.thread_id or email.id)
            self._message_threads[mid] = tid
            if tid not in self._remote_thread_ids:
                self._local_thread_ids.add(tid)
        return list(result.values())

    def _message_time(self, message: dict[str, Any]) -> float:
        precise = self._message_times.get(message["id"])
        if precise is not None:
            return as_aware(precise).timestamp()
        # A real remote's date is the best precision available in its captured
        # contract. Stable sorting retains native order for ties and missing dates.
        try:
            return as_aware(datetime.fromisoformat(message.get("date", ""))).timestamp()
        except (ValueError, TypeError):
            return float("-inf")

    def _native_message(self, email: Email, extra: dict[str, Any], message_format: str = "FULL_CONTENT") -> dict[str, Any]:
        if message_format not in {"FULL_CONTENT", "MESSAGE_FORMAT_UNSPECIFIED", "MINIMAL", "METADATA_ONLY",
                                  "THREAD_VIEW_MINIMAL", "THREAD_VIEW_METADATA_ONLY", "THREAD_VIEW_UNSPECIFIED"}:
            raise ValueError("Unsupported native message format; capture and implement its output contract first.")
        labels = ["SENT"] if extra.get("native_gmail_accepted") else ["INBOX", "UNREAD"]
        data: dict[str, Any] = {
            "id": _visible_id(email.id), "sender": email.sender,
            "toRecipients": list(email.to), "ccRecipients": list(email.cc),
            "date": email.sent_at.date().isoformat(), "labelIds": labels,
        }
        if message_format not in {"METADATA_ONLY", "THREAD_VIEW_METADATA_ONLY"}:
            data.update(subject=email.subject, snippet=" ".join(email.body.split())[:180])
        if message_format in {"FULL_CONTENT", "MESSAGE_FORMAT_UNSPECIFIED"}:
            attachments = extra.get("native_arguments", {}).get("attachments", [])
            metadata = [
                {"id": _visible_id(f"{email.id}:attachment:{i}"),
                 "mimeType": a.get("mimeType", "application/octet-stream"), "filename": a.get("filename", "")}
                for i, a in enumerate(attachments)
            ]
            data.update(plaintextBody=email.body, htmlBody=extra.get("native_arguments", {}).get("htmlBody", ""),
                        attachmentIds=[a["id"] for a in metadata], attachments=metadata)
        return data

    async def _get_message(self, arguments: dict[str, Any]) -> dict[str, Any]:
        mid = arguments["messageId"]
        for email, meta in self._locals():
            if _visible_id(email.id) == mid:
                remember_links([email.body])
                return self._result("get_message", self._native_message(email, meta, arguments.get("messageFormat", "FULL_CONTENT")))
        result = await self._read_remote("get_message", arguments)
        if not result.get("isError"):
            data = _payload(result)
            remember_links([data.get("plaintextBody", ""), data.get("htmlBody", "")])
        return result

    async def _get_thread(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tid = arguments["threadId"]
        locals_ = [(e, p) for e, p in self._locals() if self._message_threads[_visible_id(e.id)] == tid]
        if tid in self._local_thread_ids:
            template, data = None, {"id": tid, "messages": []}
        else:
            template = await self._read_remote("get_thread", arguments)
            if template.get("isError"):
                return template
            data = _payload(template)
        for msg in data.get("messages", []):
            self._message_threads[msg["id"]] = tid
            remember_links([msg.get("plaintextBody", ""), msg.get("htmlBody", "")])
        if template is not None:
            self._remote_thread_ids.add(tid)
        if not locals_:
            return template  # exact unchanged native response
        messages = {m["id"]: m for m in data.get("messages", [])}
        for email, meta in locals_:
            message = self._native_message(email, meta, arguments.get("messageFormat", "FULL_CONTENT"))
            messages[message["id"]] = message
            remember_links([email.body])
        data["messages"] = sorted(messages.values(), key=self._message_time)
        return self._result("get_thread", data, template)

    @staticmethod
    def _check_query(query: str) -> None:
        supported = {"from", "to", "cc", "subject", "after", "before", "newer", "older",
                     "newer_than", "older_than", "in", "is", "label"}
        unknown = set(re.findall(r"\b([A-Za-z_]+):", query)) - supported
        # The old local matcher ignores several Gmail operators. Reject those
        # forms instead of silently broadening or narrowing a live query.
        unsupported_values = any(
            (kind == "is" and value.lower() not in {"read", "unread", "starred"})
            or (kind == "in" and value.lower() not in {"sent", "inbox", "anywhere", "all", "trash", "bin", "archive", "archived", "starred"})
            for kind, value in re.findall(r"\b(is|in):([^\s(){}]+)", query)
        )
        if unknown or unsupported_values or re.search(r"\bAROUND\b|[()]|(?:^|\s)\+", query):
            raise ValueError("Search syntax is unavailable for this mailbox view: " + ", ".join(sorted(unknown)))

    async def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        locals_ = self._locals()
        if not locals_:
            return await self._read_remote("search_threads", arguments)
        size = max(1, min(int(arguments.get("pageSize", 20)), 50))
        key_args = {k: v for k, v in arguments.items() if k != "pageToken"}
        token = arguments.get("pageToken")
        if token:
            saved = self._pages.get(token)
            if not saved or saved[0] != key_args:
                raise ValueError("Invalid or expired page token.")
            return self._search_page(key_args, saved[1], saved[2], size, saved[3])
        query = arguments.get("query", "")
        self._check_query(query)
        matcher = parse_query(query)
        world = load_world()
        match_ctx = MatchContext(user_addresses=self.ctx.mailbox.user_addresses, now=world.now,
                                 unread=lambda e: e.source != "real")
        merged: dict[str, dict[str, Any]] = {}
        request = dict(arguments)
        first = None
        cursors: set[str] = set()
        for _ in range(self.max_search_pages):
            response = await self._read_remote("search_threads", request)
            if response.get("isError"):
                return response
            first = first or response
            data = _payload(response)
            for thread in data.get("threads", []):
                merged[thread["id"]] = thread
                self._remote_thread_ids.add(thread["id"])
                self._local_thread_ids.discard(thread["id"])
                for message in thread.get("messages", []):
                    self._message_threads[message["id"]] = thread["id"]
            cursor = data.get("nextPageToken")
            if not cursor:
                break
            if cursor in cursors:
                raise ValueError("Gmail returned a repeated pagination token.")
            cursors.add(cursor)
            request["pageToken"] = cursor
        else:
            raise ValueError("Search returned too many pages; narrow the query.")
        local_threads: dict[str, list[tuple[Email, dict[str, Any]]]] = {}
        for email, meta in locals_:
            tid = self._message_threads[_visible_id(email.id)]
            local_threads.setdefault(tid, []).append((email, meta))
        view = arguments.get("view", "THREAD_VIEW_MINIMAL")
        for tid, emails in local_threads.items():
            if tid not in merged and not any(matcher.matches(e, match_ctx) for e, _ in emails):
                continue
            # Search returns a thread when any message matches, then includes
            # related messages. A newly-sent reply can therefore make a native
            # thread appear under in:sent even before Gmail knows about it.
            if tid not in merged and tid not in self._local_thread_ids:
                result = await self._read_remote("get_thread", {"threadId": tid, "messageFormat": "MINIMAL"})
                if result.get("isError"):
                    return result
                merged[tid] = _payload(result)
            thread = merged.setdefault(tid, {"id": tid, "messages": []})
            messages = {m["id"]: m for m in thread.get("messages", [])}
            for email, meta in emails:
                native = self._native_message(email, meta, view)
                messages[native["id"]] = native
            thread["messages"] = sorted(messages.values(), key=self._message_time)
        threads = sorted(merged.values(), key=lambda t: max((self._message_time(m) for m in t.get("messages", [])), default=float("-inf")), reverse=True)
        return self._search_page(key_args, threads, first or {"content": []}, size)

    def _search_page(self, arguments: dict[str, Any], threads: list[dict[str, Any]], template: dict[str, Any], size: int, total: int | None = None) -> dict[str, Any]:
        total = len(threads) if total is None else total
        data: dict[str, Any] = {"threads": threads[:size], "resultCountEstimate": str(total)}
        if len(threads) > size:
            token = uuid.uuid4().hex
            self._pages[token] = copy.deepcopy((arguments, threads[size:], template, total))
            data["nextPageToken"] = token
        return self._result("search_threads", data, template)

    async def _write(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments.get("draftId"):
            return _error("Sending an existing draft is unavailable in this mailbox connection.")
        original_id = arguments.get("messageId") if name == "reply" else arguments.get("replyToMessageId")
        original: dict[str, Any] | None = None
        raw_original_id = original_id
        local_original = False
        if original_id:
            result = await self._get_message({"messageId": original_id, "messageFormat": "FULL_CONTENT"})
            if result.get("isError"):
                return result
            original = _payload(result)
            for e, _ in self._locals():
                if _visible_id(e.id) == original_id:
                    raw_original_id, local_original = e.id, True
        native_tid = arguments.get("replyThreadId") or self._message_threads.get(original_id)
        if original_id and not native_tid:
            return _error("Thread context is unavailable. Read the conversation before replying.")
        if native_tid and native_tid not in self._local_thread_ids:
            checked = await self._get_thread({"threadId": native_tid, "messageFormat": "MINIMAL"})
            if checked.get("isError"):
                return checked
        to, cc, bcc = list(arguments.get("to", [])), list(arguments.get("cc", [])), list(arguments.get("bcc", []))
        subject = arguments.get("subject", "")
        if name == "reply" and original:
            if not to:
                if parse_address(original.get("sender", "")) in self.ctx.mailbox.user_addresses:
                    to = list(original.get("toRecipients", []))
                else:
                    to = [original["sender"]]
            if arguments.get("replyAll"):
                cc += [a for a in original.get("toRecipients", []) + original.get("ccRecipients", [])
                       if parse_address(a) not in self.ctx.mailbox.user_addresses and a not in to and a not in cc]
            subject = original.get("subject", "")
            if not re.match(r"^\s*re:", subject, re.IGNORECASE):
                subject = "Re: " + subject
        if not to + cc + bcc or any(parse_address(a) is None for a in to + cc + bcc):
            return _error("Please specify valid recipient addresses.")
        body = arguments.get("body")
        if body is None:
            body = BeautifulSoup(arguments.get("htmlBody", ""), "html.parser").get_text("\n")
        mid = uuid.uuid4().hex[:16]
        native_tid = native_tid or mid
        raw_tid = native_tid
        for email, _ in self._locals():
            if self._message_threads[_visible_id(email.id)] == native_tid:
                raw_tid = email.thread_id or email.id
                break
        # Validate simulated success BEFORE dispatching anything. Unsupported
        # discovered write formats cannot mutate the simulated state either.
        output = {"id": mid, "threadId": native_tid, "labelIds": ["SENT"]}
        rendered = self._result(name, output)
        extra = {"message_id": mid, "native_arguments": arguments,
                 "native_gmail_attempt": True, "native_gmail_accepted": False,
                 "native_thread_id": native_tid,
                 "native_remote_thread": native_tid not in self._local_thread_ids and native_tid != mid}
        try:
            await send_message(
                self.ctx, tool_name=name, to=to, cc=cc, bcc=bcc, subject=subject, body=body,
                thread_id=raw_tid, in_reply_to=raw_original_id if local_original else original_id,
                extra=extra, charge_clock=False,
            )
        except Exception as exc:
            # Interception and simulator errors must never become successful
            # Sent entries. Inspect limits are deliberately propagated.
            from inspect_ai.util import LimitExceededError
            if isinstance(exc, LimitExceededError):
                raise
            return _error(str(exc))
        with world_tx() as world:
            interception_ids: set[str] = set()
            for interception in world.interceptions:
                if interception.payload.get("message_id") == mid:
                    interception_ids.add(interception.id)
                    interception.payload["native_gmail_accepted"] = True
                    interception.response_summary = json.dumps(output)
            # The legacy simulator assigns t-<interception-id> to new mail.
            # Native send returns one conversation id, so replies and automatic
            # acknowledgements must join that same conversation for every org.
            generated_threads = {"t-" + iid for iid in interception_ids}
            for pending in world.pending:
                if pending.email.thread_id in generated_threads:
                    pending.email.thread_id = raw_tid
            for email in world.overlay_inbox:
                if email.thread_id in generated_threads:
                    email.thread_id = raw_tid
        self._message_threads[mid] = native_tid
        if native_tid == mid:
            self._local_thread_ids.add(mid)
        return rendered


async def _discover_command(path: Path) -> None:
    """Explicit setup only: fetch definitions, never messages or write calls."""
    from refund_eval.published.mcp import MCPHTTPClient

    token = os.environ.get("GMAIL_MCP_ACCESS_TOKEN")
    if not token:
        raise GmailSetupError("Set GMAIL_MCP_ACCESS_TOKEN for explicit catalog discovery.")
    headers = {"Authorization": "Bearer " + token}
    if project := os.environ.get("GOOGLE_CLOUD_PROJECT"):
        headers["x-goog-user-project"] = project
    client = MCPHTTPClient(GMAIL_ENDPOINT, headers=headers)
    try:
        snapshot = await capture_gmail_catalog(client)
        save_catalog_snapshot(snapshot, path)
        names = {t["name"] for t in snapshot["catalog"]["tools"]}
        print(f"Saved {len(names)} native definitions; SHA-256 {snapshot['catalog_sha256']}.")
        if not names.intersection(WRITE_TOOLS):
            print("No native send_message/reply tool found. Sending profile is unavailable; no tool was invented.")
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture complete first-party Gmail MCP tool definitions.")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(_discover_command(args.output))


if __name__ == "__main__":
    main()

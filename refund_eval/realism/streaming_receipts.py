"""Opt-in future SSE receipts. Never reconnect, infer missing cost, or replay a call.

Default/historical OpenRouterAdapter behavior is untouched. Only supported complete
stream semantics can become a response. Every received raw chunk is durable first.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path

import httpx

from refund_eval.realism import investigation as inv
from refund_eval.realism import stages

PROFILE = "openrouter-chat-sse-receipts-v1"
HEADER_ALLOWLIST = ("content-type", "content-encoding", "date", "x-request-id", "cf-ray")


class StreamReceiptError(inv.PaidCallUncertain):
    """An unsupported/unfinished stream is not an adoptable model response."""


def _require(condition, message):
    if not condition:
        raise StreamReceiptError(message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _write_raw(path, raw):
    """Atomic, exclusive publication with file and parent-directory fsync."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, path)
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _object(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, "Duplicate JSON key in stream")
            result[key] = value
        return result

    def constant(_):
        raise StreamReceiptError("Nonfinite JSON in stream; raw receipt retained")
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError) as error:
        raise StreamReceiptError("Invalid stream JSON; raw receipt retained") from error
    _require(isinstance(value, dict), "Nonobject stream event")
    return value


class ReceiptJournal:
    """One future request, one exclusive receipt directory. No credential headers."""

    def __init__(self, directory, *, stage_id, model, request_sha256, secret):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        folder = os.open(self.directory.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
        self.binding = {"transport_profile": PROFILE, "stage_id": stage_id,
                        "model": model, "request_sha256": request_sha256}
        self.secret = secret.encode()
        self.tail = b""
        self.chunks = []
        self.generation_id = None
        inv._save(self.directory / "manifest.json", {**self.binding,
            "streaming_source_sha256": _sha(Path(__file__).read_bytes()),
            "base_adapter_source_sha256": _sha(Path(inv.__file__).read_bytes())}, exclusive=True)

    def _without_secret(self, raw):
        # Refuse a reflected API key before publishing the chunk that would
        # complete it, including keys split across arbitrary network chunks.
        _require(not self.secret or self.secret not in self.tail + raw,
                 "Credential reflection refused; no unsafe chunk recorded")
        if self.secret:
            length = len(self.secret) - 1
            self.tail = (self.tail + raw)[-length:] if length else b""

    def headers(self, response):
        selected = {name: response.headers[name] for name in HEADER_ALLOWLIST if name in response.headers}
        for value in selected.values():
            self._without_secret(value.encode())
            self.tail = b""
        self._without_secret(inv._json(selected).encode())
        self.tail = b""
        inv._save(self.directory / "headers.json", {**self.binding, "status_code": response.status_code,
                                                   "headers": selected}, exclusive=True)

    def append(self, raw):
        self._without_secret(raw)
        number = len(self.chunks)
        path = self.directory / f"raw-{number:06d}.bin"
        _write_raw(path, raw)
        self.chunks.append({"sequence": number, "file": path.name, "bytes": len(raw), "sha256": _sha(raw)})

    def identify(self, identifier):
        if self.generation_id is None:
            inv._save(self.directory / "generation-id.json", {**self.binding,
                "generation_id": identifier, "source": "complete SSE JSON event id",
                "through_chunk": self.chunks[-1]}, exclusive=True)
            self.generation_id = identifier
        else:
            _require(self.generation_id == identifier, "Generation ID changed within one stream")

    def eof(self, *, done):
        inv._save(self.directory / "eof.json", {**self.binding, "normal_eof": True,
            "done_observed": done, "chunks": self.chunks}, exclusive=True)

    def complete(self, response_path, *, actual):
        inv._save(self.directory / "complete.json", {**self.binding, "normal_eof": True,
            "done_observed": True, "generation_id": self.generation_id, "chunks": self.chunks,
            "response_file": str(response_path.resolve()), "response_sha256": _sha(response_path.read_bytes()),
            "cost_status": "known" if actual is not None else "unknown"}, exclusive=True)

    def invalid(self, *, billing, errors, actual):
        document = {**self.binding, "normal_eof": True, "done_observed": True,
                    "generation_id": self.generation_id, "chunks": self.chunks,
                    "response_valid": False, "billing": billing, "validation_errors": errors,
                    "cost_status": "known" if actual is not None else "unknown"}
        self._without_secret(inv._json(document).encode())
        inv._save(self.directory / "invalid.json", document, exclusive=True)


class SSECollector:
    """Strict text/function delta assembly; unsupported details remain raw, never guessed.

    Repeated text fragments are real deltas and are concatenated, never deduped.
    Same-index unsigned reasoning.text fragments append in order only with stable
    type/id/format. One opaque signature-only update may seal a completed text
    item; repeated/split signatures, later text and encrypted merging are refused.
    Content errors are deferred while raw transport and terminal usage are collected;
    a known billed invalid answer is never reconstructed as a usable response.
    """

    def __init__(self, *, model, identify):
        self.model, self.identify = model, identify
        self.buffer = b""
        self.done = False
        self.normal_eof = False
        self.choice_extra = {}
        self.identifier = None
        self.created = None
        self.role_seen = False
        self.message = {}
        self.tools = {}
        self.tool_seen = False
        self.details = {}
        self.finish_reason = None
        self.usage = None
        self.extra = {}
        self.event_ids = set()
        self.first_frame = True
        self.errors = []
        self.accounting_errors = []
        self.usage_terminal = False

    def feed(self, raw):
        _require(not self.normal_eof, "Data after EOF")
        self.buffer += raw
        while match := re.search(rb"\r\n\r\n|\n\n|\r\r", self.buffer):
            frame, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
            if self.first_frame:
                frame = frame.removeprefix(b"\xef\xbb\xbf")
                self.first_frame = False
            try:
                self._frame(frame)
            except StreamReceiptError as error:
                # Frame/JSON/identity ambiguity blocks accounting, but never abandons
                # the HTTP body: a later received usage receipt still stays durable.
                self.accounting_errors.append(str(error))
                self.errors.append(str(error))

    def _frame(self, frame):
        try:
            lines = re.split(r"\r\n|\r|\n", frame.decode("utf-8"))
        except UnicodeError as error:
            raise StreamReceiptError("Invalid stream UTF-8; raw receipt retained") from error
        data, event_id = [], None
        for line in lines:
            if not line or line.startswith(":"):
                continue
            key, separator, value = line.partition(":")
            value = value.removeprefix(" ")
            if key == "data":
                data.append(value)
            elif key == "id":
                _require(event_id is None and "\x00" not in value, "Ambiguous SSE event ID")
                event_id = value
            elif key == "event":
                _require(value == "message", "Unsupported SSE event type")
            else:
                _require(bool(separator) and key == "retry", "Unsupported SSE field")
                # Advisory retry never causes a reconnect or second request.
        if not data:
            return
        _require(not self.done, "Data after DONE or duplicate DONE")
        if event_id:
            _require(event_id not in self.event_ids, "Duplicate explicit SSE event ID")
            self.event_ids.add(event_id)
        payload = "\n".join(data)
        if payload.strip() == "[DONE]":
            self.done = True
            return
        value = _object(payload)
        self._observe_accounting(value)
        try:
            self._event(value)
        except StreamReceiptError as error:
            self.errors.append(str(error))  # Continue collecting exact terminal usage.

    def _observe_accounting(self, value):
        identifier = value.get("id")
        _require(isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", identifier),
                 "Missing/invalid generation ID")
        self.identify(identifier)  # Durable before model/delta validation.
        if self.identifier is None:
            self.identifier = identifier
        _require(identifier == self.identifier, "Generation ID changed within stream")
        allowed = {"id", "object", "created", "model", "choices", "usage", "system_fingerprint", "provider", "service_tier"}
        _require(not (set(value) - allowed), "Unsupported/error stream envelope; raw receipt retained")
        _require(value.get("model") == self.model and value.get("object") == "chat.completion.chunk",
                 "Stream model/object differs from exact pinned protocol")
        created = value.get("created")
        _require(type(created) is int and created >= 0, "Invalid stream creation time")
        if self.created is None:
            self.created = created
        _require(created == self.created, "Stream creation identity changed")
        for field in ("system_fingerprint", "provider", "service_tier"):
            if field in value:
                _require(field not in self.extra or self.extra[field] == value[field], "Stream metadata changed")
                self.extra[field] = copy.deepcopy(value[field])
        if value.get("usage") is not None:
            _require(self.usage is None and isinstance(value["usage"], dict),
                     "Duplicate/invalid stream usage")
            choices = value.get("choices")
            terminal = (choices == [] or (isinstance(choices, list) and len(choices) == 1
                        and isinstance(choices[0], dict) and type(choices[0].get("index")) is int
                        and choices[0]["index"] == 0 and isinstance(choices[0].get("finish_reason"), str)
                        and bool(choices[0]["finish_reason"])))
            _require(terminal, "Premature/nonterminal stream usage")
            self.usage = copy.deepcopy(value["usage"])
            self.usage_terminal = True
        elif self.usage is not None:
            raise StreamReceiptError("Event after terminal usage")

    def _event(self, value):
        choices = value.get("choices")
        _require(isinstance(choices, list) and len(choices) <= 1, "Missing/multiple stream choices")
        if choices:
            choice = choices[0]
            _require(isinstance(choice, dict) and not (set(choice) - {"index", "delta", "finish_reason", "native_finish_reason", "logprobs"})
                     and type(choice.get("index")) is int and choice["index"] == 0,
                     "Unsupported/duplicate stream choice")
            _require(choice.get("logprobs") is None, "Nonempty streamed logprobs unsupported")
            if "logprobs" in choice:
                self.choice_extra["logprobs"] = None
            delta = choice.get("delta")
            _require(isinstance(delta, dict), "Missing stream delta")
            finish = choice.get("finish_reason")
            if self.finish_reason is not None:
                usage_trailer = (isinstance(value.get("usage"), dict) and finish == self.finish_reason
                                 and not (set(delta) - {"role", "content"})
                                 and delta.get("role", "assistant") == "assistant"
                                 and delta.get("content", "") == "")
                native_trailer = (delta == {} and finish is None
                                  and isinstance(choice.get("native_finish_reason"), str)
                                  and bool(choice["native_finish_reason"]))
                _require(usage_trailer or native_trailer, "Choice data after terminal finish")
                if usage_trailer:
                    self._delta(delta)  # Empty strings still preserve field presence.
            else:
                self._delta(delta)
            if "native_finish_reason" in choice:
                native = choice["native_finish_reason"]
                _require(native is None or (isinstance(native, str) and bool(native)),
                         "Invalid native finish reason")
                previous = self.choice_extra.get("native_finish_reason")
                _require(previous is None or native == previous, "Native finish reason changed or regressed")
                _require(native is None or self.finish_reason is not None
                         or (isinstance(finish, str) and bool(finish)), "Premature native finish reason")
                self.choice_extra["native_finish_reason"] = native
            if finish is not None:
                _require(isinstance(finish, str) and bool(finish), "Invalid terminal finish")
                self.finish_reason = finish
        else:
            _require(value.get("usage") is not None and self.finish_reason is not None,
                     "Empty choices before terminal usage")

    def _delta(self, delta):
        _require(not (set(delta) - {"role", "content", "refusal", "reasoning", "tool_calls", "reasoning_details"}),
                 "Unsupported stream delta; no fields silently dropped")
        if "role" in delta:
            _require(delta["role"] == "assistant", "Unsupported streamed message role")
            self.role_seen = True
        for field in ("content", "refusal", "reasoning"):
            if field in delta:
                value = delta[field]
                _require(value is None or isinstance(value, str), "Nontext stream delta")
                if field not in self.message:
                    self.message[field] = value
                elif value is not None:
                    self.message[field] = (self.message[field] or "") + value
        if "tool_calls" in delta:
            self.tool_seen = True
            _require(isinstance(delta["tool_calls"], list), "Invalid tool delta list")
            indices = set()
            for call in delta["tool_calls"]:
                _require(isinstance(call, dict) and not (set(call) - {"index", "id", "type", "function"}),
                         "Unsupported tool delta")
                index = call.get("index")
                _require(type(index) is int and index >= 0 and index not in indices, "Duplicate/invalid tool index")
                indices.add(index)
                tool = self.tools.setdefault(index, {"function": {}})
                for field in ("id", "type"):
                    if field in call:
                        _require(isinstance(call[field], str) and call[field]
                                 and (field not in tool or tool[field] == call[field]), "Tool identity changed")
                        tool[field] = call[field]
                if "function" in call:
                    function = call["function"]
                    _require(isinstance(function, dict) and not (set(function) - {"name", "arguments"}), "Unknown tool function field")
                    for field, value in function.items():
                        _require(isinstance(value, str), "Nontext tool function delta")
                        tool["function"][field] = tool["function"].get(field, "") + value
        if "reasoning_details" in delta:
            _require(isinstance(delta["reasoning_details"], list), "Invalid reasoning detail list")
            indices = set()
            for detail in delta["reasoning_details"]:
                _require(isinstance(detail, dict), "Invalid reasoning detail")
                index, kind = detail.get("index"), detail.get("type")
                _require(type(index) is int and index >= 0 and index not in indices,
                         "Unindexed/duplicate reasoning detail unsupported; retain raw chunks")
                indices.add(index)
                fields = {"reasoning.text": {"text", "signature"}, "reasoning.summary": {"summary"},
                          "reasoning.encrypted": {"data"}}
                _require(isinstance(kind, str) and kind in fields and not (set(detail) - {"index", "type", "id", "format"} - fields[kind]),
                         "Unsupported reasoning detail semantics")
                _require(all(value is None or isinstance(value, str) for key, value in detail.items()
                             if key not in {"index", "type"}), "Invalid reasoning detail values")
                if kind != "reasoning.text":
                    field = "summary" if kind == "reasoning.summary" else "data"
                    _require(isinstance(detail.get(field), str), "Incomplete reasoning detail")
                if index in self.details:
                    prior = self.details[index]
                    identity = ("type", "id", "format")
                    stable = (kind == prior.get("type") == "reasoning.text"
                              and all((field in prior) == (field in detail) and prior.get(field) == detail.get(field)
                                      for field in identity))
                    signature_only = (stable and "signature" not in prior and "text" not in detail
                                      and isinstance(detail.get("signature"), str) and bool(detail["signature"])
                                      and isinstance(prior.get("text"), str))
                    if signature_only:
                        # One complete opaque signature value, assigned once. Never
                        # concatenate signatures or permit later text on a sealed item.
                        prior["signature"] = detail["signature"]
                    else:
                        _require(stable and "signature" not in prior and "signature" not in detail
                                 and isinstance(prior.get("text"), str) and isinstance(detail.get("text"), str),
                                 "Ambiguous signed/encrypted/identity-changing reasoning fragments; retain raw chunks")
                        prior["text"] += detail["text"]
                else:
                    _require(kind != "reasoning.text" or isinstance(detail.get("text"), str),
                             "Incomplete reasoning text detail")
                    self.details[index] = copy.deepcopy(detail)

    def end_of_stream(self):
        _require(not self.normal_eof, "Duplicate EOF")
        self.normal_eof = True

    def accounting(self):
        """Cost evidence independent of answer validity; incomplete/ambiguous stays held."""
        _require(not self.accounting_errors and self.normal_eof and self.done and not self.buffer.strip()
                 and self.identifier is not None and self.usage_terminal and self.usage is not None,
                 "Stream billing incomplete/ambiguous; preserve full hold")
        return {"generation_id": self.identifier, "usage": copy.deepcopy(self.usage)}

    def response(self):
        _require(not self.errors, "Invalid streamed response; raw and usage receipts retained")
        _require(self.normal_eof and self.done and not self.buffer.strip() and self.identifier is not None
                 and self.role_seen and self.finish_reason is not None and self.usage is not None,
                 "Stream incomplete or missing terminal usage; no response reconstructed")
        message = {"role": "assistant", **copy.deepcopy(self.message)}
        if self.tool_seen:
            _require(sorted(self.tools) == list(range(len(self.tools))), "Missing tool-call indices")
            tools = [copy.deepcopy(self.tools[index]) for index in sorted(self.tools)]
            ids = []
            for tool in tools:
                function = tool["function"]
                _require(isinstance(tool.get("id"), str) and tool.get("type") == "function"
                         and isinstance(function.get("name"), str) and function["name"]
                         and isinstance(function.get("arguments"), str), "Incomplete streamed tool call")
                ids.append(tool["id"])
            _require(len(ids) == len(set(ids)), "Duplicate streamed tool-call IDs")
            message["tool_calls"] = tools
        if self.details:
            _require(sorted(self.details) == list(range(len(self.details))), "Missing reasoning detail indices")
            message["reasoning_details"] = [self.details[index] for index in sorted(self.details)]
        return {"id": self.identifier, "object": "chat.completion", "created": self.created,
                "model": self.model, **self.extra, "usage": copy.deepcopy(self.usage),
                "choices": [{"index": 0, "finish_reason": self.finish_reason, "message": message, **self.choice_extra}]}


def replay_stream_journal(directory, *, expected_sha256):
    """Reparse a pinned known-billed INVALID journal, without writes or dispatch.

    Caller pins six exact raw hashes: request, manifest, headers, generation_id,
    eof, terminal (invalid.json). Old receipts and costs remain unchanged. This
    returns a newly parsed response plus additive source proof, never a ledger
    transition, report validation, or permission to replay an HTTP request.
    """
    directory = Path(directory)
    _require(directory.is_dir() and not directory.is_symlink(), "Invalid replay journal directory")
    names = {"manifest": "manifest.json", "headers": "headers.json",
             "generation_id": "generation-id.json", "eof": "eof.json", "terminal": "invalid.json"}
    _require(isinstance(expected_sha256, dict) and set(expected_sha256) == set(names) | {"request"}
             and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                     for value in expected_sha256.values()), "Exact six source SHA256 pins required")
    observed = {}

    def read(path, digest):
        _require(path.is_file() and not path.is_symlink(), "Replay source missing or symlinked")
        raw = path.read_bytes()
        _require(_sha(raw) == digest, "Replay source SHA256 mismatch")
        observed[path] = digest
        return raw

    docs = {key: _object(read(directory / name, expected_sha256[key])) for key, name in names.items()}
    manifest = docs["manifest"]
    fields = {"transport_profile", "stage_id", "model", "request_sha256"}
    _require(set(manifest) == fields | {"streaming_source_sha256", "base_adapter_source_sha256"},
             "Unsupported journal manifest fields")
    binding = {key: manifest[key] for key in fields}
    stage = binding["stage_id"]
    _require(isinstance(stage, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,180}", stage)
             and directory.name == stage + "-stream" and binding["transport_profile"] == PROFILE
             and isinstance(binding["model"], str) and bool(binding["model"])
             and binding["request_sha256"] == expected_sha256["request"], "Replay request/stage/profile binding differs")
    for field in ("streaming_source_sha256", "base_adapter_source_sha256"):
        _require(isinstance(manifest[field], str) and re.fullmatch(r"[0-9a-f]{64}", manifest[field]),
                 "Invalid original source hash")
    request_path = directory.parent / (stage + "-request.json")
    request = _object(read(request_path, expected_sha256["request"]))
    _require(set(request) == {"transport_profile", "payload", "bound", "reserve_usd"}
             and request["transport_profile"] == PROFILE and isinstance(request["payload"], dict)
             and request["payload"].get("model") == binding["model"]
             and request["payload"].get("stream") is True
             and request["payload"].get("stream_options") == {"include_usage": True}, "Not an exact future stream request")
    expected_fields = {
        "headers": fields | {"status_code", "headers"},
        "generation_id": fields | {"generation_id", "source", "through_chunk"},
        "eof": fields | {"normal_eof", "done_observed", "chunks"},
        "terminal": fields | {"normal_eof", "done_observed", "generation_id", "chunks",
                               "response_valid", "billing", "validation_errors", "cost_status"},
    }
    for key, extra in expected_fields.items():
        _require(set(docs[key]) == extra and all(docs[key][field] == binding[field] for field in fields),
                 "Replay receipt fields/binding differ")
    headers, eof, terminal, identity = (docs[key] for key in ("headers", "eof", "terminal", "generation_id"))
    selected = headers["headers"]
    _require(type(headers["status_code"]) is int and headers["status_code"] == 200
             and isinstance(selected, dict) and not (set(selected) - set(HEADER_ALLOWLIST))
             and all(isinstance(value, str) for value in selected.values())
             and selected.get("content-type", "").split(";")[0].strip().lower() == "text/event-stream"
             and selected.get("content-encoding", "identity").lower() == "identity", "Not a successful uncompressed SSE journal")
    for receipt in (eof, terminal):
        _require(receipt["normal_eof"] is True and receipt["done_observed"] is True,
                 "Replay requires normal EOF and DONE")
    _require(terminal["response_valid"] is False and terminal["cost_status"] == "known"
             and isinstance(terminal["validation_errors"], list)
             and all(isinstance(value, str) for value in terminal["validation_errors"])
             and isinstance(terminal["billing"], dict) and set(terminal["billing"]) == {"generation_id", "usage"},
             "Replay requires known billed invalid receipt; unknown holds cannot be adopted")
    chunks = eof["chunks"]
    _require(isinstance(chunks, list) and bool(chunks) and inv._json(chunks) == inv._json(terminal["chunks"]),
             "Replay inventories differ")
    _require({path.name for path in directory.glob("raw-*.bin")} == {f"raw-{i:06d}.bin" for i in range(len(chunks))},
             "Unlisted or missing replay raw chunk")
    first_id = None
    through_chunk = None
    current = None

    def identify(identifier):
        nonlocal first_id, through_chunk
        if first_id is None:
            first_id, through_chunk = identifier, current
        _require(identifier == first_id, "Replay generation ID changed")

    collector = SSECollector(model=binding["model"], identify=identify)
    for i, descriptor in enumerate(chunks):
        _require(isinstance(descriptor, dict) and set(descriptor) == {"sequence", "file", "bytes", "sha256"}
                 and type(descriptor["sequence"]) is int and descriptor["sequence"] == i
                 and descriptor["file"] == f"raw-{i:06d}.bin" and type(descriptor["bytes"]) is int
                 and descriptor["bytes"] > 0 and isinstance(descriptor["sha256"], str)
                 and re.fullmatch(r"[0-9a-f]{64}", descriptor["sha256"]), "Invalid replay chunk sequence/descriptor")
        raw = read(directory / descriptor["file"], descriptor["sha256"])
        _require(len(raw) == descriptor["bytes"], "Replay raw chunk size differs")
        current = descriptor
        collector.feed(raw)
    collector.end_of_stream()
    response, billing = collector.response(), collector.accounting()
    _require(identity["source"] == "complete SSE JSON event id"
             and identity["generation_id"] == terminal["generation_id"] == first_id == billing["generation_id"]
             and inv._json(identity["through_chunk"]) == inv._json(through_chunk)
             and inv._json(terminal["billing"]) == inv._json(billing), "Replay identity/terminal usage differs")
    cost = billing["usage"].get("cost")
    _require(type(cost) in (int, float) and math.isfinite(cost) and cost >= 0, "Replay cannot reinterpret unknown cost")
    # Recheck after parsing: concurrent changes cannot silently alter the sources
    # behind this proof. No source artifact is opened for writing.
    for path, digest in list(observed.items()):
        read(path, digest)
    proof = {"operation": "offline-stream-reparse-v1", **binding,
             "journal_directory": str(directory.resolve()), "request_file": str(request_path.resolve()),
             "source_sha256": copy.deepcopy(expected_sha256), "chunks": copy.deepcopy(chunks),
             "original_streaming_source_sha256": manifest["streaming_source_sha256"],
             "original_base_adapter_source_sha256": manifest["base_adapter_source_sha256"],
             "corrected_parser_source_sha256": _sha(Path(__file__).read_bytes()),
             "generation_id": first_id, "normal_eof": True, "done_observed": True,
             "usage": copy.deepcopy(billing["usage"]), "actual_cost": cost,
             "response_canonical_sha256": _sha(inv._json(response).encode()),
             "ledger_write": False, "network_dispatch": False}
    return {"response": response, "source_proof": proof}


def stream_payload(model, settings, messages, tools):
    return {**inv.OpenRouterAdapter._payload(model, settings, messages, tools),
            "stream": True, "stream_options": {"include_usage": True}}


def validate_stream_request(document, *, model, settings, messages, tools, metadata):
    """New strict validator only; never adopts or rewrites a historical request."""
    payload = stream_payload(model, settings, messages, tools)
    amount, bound = inv.OpenRouterAdapter._bound(metadata, payload)
    expected = {"transport_profile": PROFILE, "payload": payload, "bound": bound, "reserve_usd": amount}
    _require(inv._json(document) == inv._json(expected), "Future stream request binding differs")


class StreamingOpenRouterAdapter(inv.OpenRouterAdapter):
    """Explicit FUTURE profile/subclass; existing adapter/defaults stay untouched."""

    def __init__(self, *, transport_profile, stage_namespace, **kwargs):
        if transport_profile != PROFILE or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}-stream-v1", stage_namespace):
            raise inv.InvestigationError("Explicit future stream profile and fresh namespace ending -stream-v1 required")
        super().__init__(**kwargs)
        if self.directory.exists() and any(self.directory.iterdir()):
            raise inv.InvestigationError("A future stream adapter requires a fresh empty output directory")
        self.stage_namespace = stage_namespace

    @staticmethod
    def _payload(model, settings, messages, tools):
        return stream_payload(model, settings, messages, tools)

    async def call(self, *, model, settings, messages, tools, stage_id):
        self._check_ready()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,180}", stage_id) or not stage_id.startswith(self.stage_namespace + "-"):
            raise inv.InvestigationError("Future stream stage is outside its pinned namespace")
        name = inv._model(model)
        key = self.api_key or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise inv.InvestigationError("OPENROUTER_API_KEY is required")
        payload = self._payload(model, settings, messages, tools)
        metadata = await self._metadata(model)
        amount, bound = self._bound(metadata, payload)
        self._check_ready()
        request_path = self.directory / (stage_id + "-request.json")
        request = {"transport_profile": PROFILE, "payload": payload, "bound": bound, "reserve_usd": amount}
        _require(key not in inv._json(request), "Credential in request payload refused before reservation")
        inv._save(request_path, request, exclusive=True)
        journal = ReceiptJournal(self.directory / (stage_id + "-stream"), stage_id=stage_id,
                                 model=name, request_sha256=_sha(request_path.read_bytes()), secret=key)
        stages.reserve(self.ledger, self.budget_usd, stage_id, "interaction", amount,
                       validate=lambda _: self._check_ready())
        dispatched = False
        collector = SSECollector(model=name, identify=journal.identify)
        try:
            self._check_ready()
            async with httpx.AsyncClient(timeout=180, transport=self.transport or httpx.AsyncHTTPTransport(retries=0),
                                         follow_redirects=False) as client:
                self._check_ready()
                dispatched = True
                async with client.stream("POST", "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Accept": "text/event-stream", "Accept-Encoding": "identity"},
                    json=payload) as response:
                    journal.headers(response)
                    is_sse = (response.status_code == 200
                              and response.headers.get("content-type", "").split(";")[0].strip().lower() == "text/event-stream"
                              and response.headers.get("content-encoding", "identity").lower() == "identity")
                    async for raw in response.aiter_raw():
                        if raw:
                            journal.append(raw)  # fsync BEFORE parsing or publishing an ID.
                            if is_sse:
                                collector.feed(raw)
                    journal.eof(done=collector.done)
                    if is_sse:
                        collector.end_of_stream()
                    response.raise_for_status()
                    _require(is_sse, "Expected uncompressed SSE response; raw receipt retained")
            result, invalid = None, None
            try:
                result = collector.response()  # Only after normal EOF AND DONE.
            except StreamReceiptError as error:
                invalid = error
            billing = collector.accounting()
            raw_cost = billing["usage"].get("cost")
            actual = raw_cost if type(raw_cost) in (int, float) and math.isfinite(raw_cost) and raw_cost >= 0 else None
            if invalid is not None:
                journal.invalid(billing=billing, errors=collector.errors + [str(invalid)], actual=actual)
            else:
                response_path = self.directory / (stage_id + "-response.json")
                journal._without_secret(inv._json(result).encode())
                inv._save(response_path, result, exclusive=True)
                journal.complete(response_path, actual=actual)
        except BaseException as error:
            if not dispatched:
                stages.finish(self.ledger, self.budget_usd, stage_id, "failed", 0, "Stopped before stream dispatch")
                raise
            self._uncertain_stage = self._uncertain_stage or stage_id
            stages.finish(self.ledger, self.budget_usd, stage_id, "unknown", detail=type(error).__name__)
            raise inv.PaidCallUncertain("Stream interrupted/unsupported; retain full hold and receipts, no retry") from error
        if actual is None:
            self._uncertain_stage = self._uncertain_stage or stage_id
        stages.finish(self.ledger, self.budget_usd, stage_id, "complete", actual)
        self._check_ready()
        if invalid is not None:
            raise inv.InvestigationError("Known billed invalid streamed response; no report or retry") from invalid
        choice = result["choices"][0]
        if choice["finish_reason"] not in {"stop", "tool_calls"}:
            raise inv.InvestigationError("Streamed response incomplete or truncated; no retry")
        return {"model": model, "message": choice["message"], "usage": result["usage"],
                "generation_id": result["id"]}

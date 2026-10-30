"""Versioned, exhaustive whole-message scan plans. Planning never calls a model.

This is a different observation protocol from the legacy whole-context scanner.
Parts preserve source coverage, not the judge's ability to compare distant parts.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
from pathlib import Path

VERSION = "frozen-parts-v1"
PLAN_NAME = "frozen-parts.json"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def limits(per_trial, total):
    require(type(per_trial) is int and 1 <= per_trial <= 16
            and type(total) is int and 1 <= total <= 64, "Invalid finite scan part limits")


def runtime():
    from inspect_ai.model import _openai
    from inspect_ai.model._providers import openai_compatible, openrouter
    from inspect_scout._llm_scanner import generate, structured
    from inspect_scout._scanner import extract
    from inspect_scout._util import refusal

    from refund_eval.realism import scan_budget
    from scout import scanners

    return {"packages": {name: importlib.metadata.version(name) for name in ("inspect-ai", "inspect-scout")},
            "files": {name: file_digest(module.__file__) for name, module in {
                "parts": __import__(__name__, fromlist=["__file__"]), "budget": scan_budget,
                "scanners": scanners, "generate": generate, "structured": structured, "extract": extract,
                "refusal": refusal, "wire": _openai, "provider": openrouter,
                "compatible": openai_compatible}.items()}}


def answer_spec():
    from inspect_scout import AnswerStructured

    from scout.scanners import ANSWER_FORMAT, Tells

    return AnswerStructured(type=Tells, answer_format=ANSWER_FORMAT, max_attempts=1)


def schema():
    from types import SimpleNamespace

    from inspect_ai.model._providers.openai_compatible import OpenAICompatibleAPI
    from inspect_ai.tool import ToolInfo, ToolParams
    from inspect_scout._llm_scanner.structured import structured_schema

    from scout.scanners import Tells

    raw = Tells.model_json_schema()
    resolved = structured_schema(answer_spec())
    # Mirror structured_generate's tool declaration, then use the installed
    # provider's pure serializer. No model/client construction or token counting.
    tool = ToolInfo(name="answer", description="Use this tool to submit your final answer.",
                    parameters=ToolParams(type="object", properties=resolved.properties or {},
                                          required=resolved.required or []))
    provider = SimpleNamespace(strict_tools=True,
                               schema_exclude_fields=OpenAICompatibleAPI.schema_exclude_fields.fget(None))
    wire = OpenAICompatibleAPI.tools_to_openai(provider, [tool])
    resolved = resolved.model_dump(exclude_none=True)
    size = lambda obj: len(json.dumps(obj, ensure_ascii=False).encode())
    return {"raw": raw, "resolved": resolved, "tools": wire,
            "allowance_bytes": max(size(raw), size(resolved), size(wire))}


def checked_policy(checked):
    """Revalidate saved tariffs/windows without fetching metadata or token counts."""
    from refund_eval.realism.scan_budget import (
        FRAMING_TOKENS,
        SUPPORTED_WINDOWS,
        _price,
        bounded_tells_config,
        input_price_bound,
    )

    judge, cap = checked["judge"], checked["max_tokens"]
    bounded_tells_config(cap, judge=judge)
    require(checked["single_attempt"] is True and isinstance(judge, str)
            and judge.startswith("openrouter/") and judge.removeprefix("openrouter/") in SUPPORTED_WINDOWS,
            "Unsupported frozen judge policy")
    window = checked["context_window"]
    require(type(window) is int and 0 < window <= SUPPORTED_WINDOWS[judge.removeprefix("openrouter/")],
            "Invalid frozen context window")
    pricing = checked["tariff"]
    known = {"prompt", "input_cache_write", "input_cache_read", "input_cache_write_1h", "completion",
             "overrides", "request", "image", "web_search", "internal_reasoning"}
    require(isinstance(pricing, dict) and not (set(pricing) - known)
            and ("overrides" not in pricing or pricing["overrides"] == []), "Unsupported frozen tariffs")
    for key in ("request", "image", "internal_reasoning"):
        require(not _price(pricing, key, required=False), "Additional frozen tariff")
    _price(pricing, "web_search", required=False)
    rate = input_price_bound(judge, pricing)
    require(type(checked["prompt_price_bound"]) in (int, float)
            and checked["prompt_price_bound"] == rate
            and type(checked["output_price"]) in (int, float)
            and checked["output_price"] == _price(pricing, "completion"), "Frozen price bound differs")
    return FRAMING_TOKENS, (window * 9) // 10


def generation_policy(checked):
    from inspect_ai.model import GenerateConfig

    from refund_eval.realism.scan_budget import bounded_tells_config

    return {"config": bounded_tells_config(checked["max_tokens"], judge=checked["judge"]).merge(
                GenerateConfig(parallel_tool_calls=False)).model_dump(exclude_none=True),
            "sdk_args": {"max_retries": 0}, "answer_attempts": 1, "refusal_retries": 0,
            "context_tools": []}


async def transcript_plan(transcript, source_hash, checked, per_trial):
    from inspect_scout import message_numbering, scanner_prompt

    from scout import scanners

    require(not transcript.events and not transcript.timelines, "Event/timeline scan parts are unsupported")
    framing, window = checked_policy(checked)
    frozen_schema = schema()
    question = scanners.tells_question(scanners.scenario_notes(transcript), scanners.scenario_intent(transcript))
    formatter, references = message_numbering(preprocessor=scanners.PREPROCESSOR)
    blocks, spans, calls = [], [], {}
    for index, message in enumerate(transcript.messages):
        text = await formatter([message])
        ordinal = f"M{index + 1}"
        refs = references(f"[{ordinal}]")
        require(bool(text) and len(refs) == 1, "Every original message needs one frozen ordinal")
        blocks.append({"ordinal": ordinal, "native_id": refs[0].id, "text": text,
                       "sha256": hashlib.sha256(text.encode()).hexdigest()})
        spans.append((index, index + 1))
        for call in getattr(message, "tool_calls", None) or []:
            require(call.id not in calls, "Ambiguous native tool call identity")
            calls[call.id] = index
        if message.role == "tool":
            call_id = getattr(message, "tool_call_id", None)
            require(call_id in calls, "Orphan native tool result")
            spans.append((calls[call_id], index + 1))
    # Keep linked calls/results together even if a runtime notice intervenes.
    # Unanswered original calls remain visible; never invent a missing result.
    groups = []
    for start, stop in sorted(spans):
        if groups and start < groups[-1]["stop"]:
            groups[-1]["stop"] = max(stop, groups[-1]["stop"])
        else:
            groups.append({"start": start, "stop": stop})
    require(bool(blocks), "Empty original transcript")
    meta = transcript.metadata
    require(all(meta.get(key) is not None for key in ("eval_id", "id", "epoch")), "Missing native scan identity")
    identity = {"eval_id": str(meta["eval_id"]), "sample_id": str(meta["id"]), "epoch": str(meta["epoch"]),
                "transcript_id": transcript.transcript_id}

    def part(start, stop):
        prompt = scanner_prompt("\n".join(block["text"] for block in blocks[start:stop]), question, answer_spec())
        bound = len(prompt.encode()) + frozen_schema["allowance_bytes"] + framing
        value = {"start": start, "stop": stop, "prompt": prompt, "input_token_bound": bound,
                 "rendered_input_bytes": len(prompt.encode()), "output_token_bound": checked["max_tokens"],
                 "required_usd": bound * checked["prompt_price_bound"] + checked["max_tokens"] * checked["output_price"]}
        value["key"] = digest({"protocol": VERSION, "identity": identity, "source_sha256": source_hash, **value})
        return value

    parts, start, stop = [], 0, 0
    for group in groups:
        candidate = part(start, group["stop"])
        if candidate["input_token_bound"] + checked["max_tokens"] > window:
            require(stop > start, "A whole message/tool exchange exceeds the scan bound")
            parts.append(part(start, stop))
            start = stop
            candidate = part(start, group["stop"])
            require(candidate["input_token_bound"] + checked["max_tokens"] <= window,
                    "A whole message/tool exchange exceeds the scan bound")
        stop = group["stop"]
    parts.append(part(start, stop))
    require(len(parts) <= per_trial, "Scan exceeds the declared per-transcript part limit")
    return {"identity": identity, "source_sha256": source_hash, "messages": blocks, "groups": groups,
            "schema": frozen_schema, "parts": parts}


async def build(logs, checked, *, max_parts_per_transcript, max_parts_total, aliases=None):
    from inspect_scout import TranscriptContent, transcripts_from

    from refund_eval.realism.coverage import source_identity

    limits(max_parts_per_transcript, max_parts_total)
    checked_policy(checked)
    require(logs and len(set(map(source_identity, logs))) == len(logs), "Distinct scan logs required")
    reverse = {source_identity(value): source_identity(key) for key, value in (aliases or {}).items()}
    require(len(reverse) == len(aliases or {}), "Ambiguous scan source aliases")
    rows = []
    async with transcripts_from(logs).reader() as reader:
        async for info in reader.index():
            source = source_identity(info.source_uri)
            require(source in set(map(source_identity, logs)), "Unexpected scan source")
            transcript = await reader.read(info, TranscriptContent(messages="all"))
            row = await transcript_plan(transcript, file_digest(source), checked, max_parts_per_transcript)
            row["source"] = reverse.get(source, source)
            rows.append(row)
    rows.sort(key=lambda row: (row["source"], row["identity"]["transcript_id"]))
    require(bool(rows) and len({digest(row["identity"]) for row in rows}) == len(rows), "Duplicate/empty scan identities")
    require(sum(len(row["parts"]) for row in rows) <= max_parts_total, "Scan exceeds the declared total part limit")
    return {"scan_protocol": VERSION, "checked": checked, "runtime": runtime(),
            "generation_policy": generation_policy(checked),
            "max_parts_per_transcript": max_parts_per_transcript, "max_parts_total": max_parts_total,
            "per_transcript": rows, "required_usd": sum(p["required_usd"] for r in rows for p in r["parts"])}


async def plan(logs, judge, *, max_tokens, max_parts_per_transcript, max_parts_total):
    from refund_eval.realism.scan_budget import preflight

    checked = await preflight(judge, max_tokens=max_tokens, single_attempt=True)
    return await build(logs, checked, max_parts_per_transcript=max_parts_per_transcript, max_parts_total=max_parts_total)


async def validate(plan, logs, *, aliases=None):
    require(plan.get("scan_protocol") == VERSION, "Wrong frozen scan protocol")
    expected = await build(logs, plan["checked"], max_parts_per_transcript=plan["max_parts_per_transcript"],
                           max_parts_total=plan["max_parts_total"], aliases=aliases)
    require(digest(expected) == digest(plan), "Frozen scan parts/source/render/runtime changed")


def load(path, expected):
    raw = Path(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected, "Frozen scan plan bytes changed")
    plan = json.loads(raw)
    require(plan.get("scan_protocol") == VERSION and plan.get("runtime") == runtime(), "Frozen scan runtime differs")
    return plan


def citations(value, row, part):
    from scout.scanners import Tells

    parsed = Tells.model_validate(value)
    visible = {block["ordinal"] for block in row["messages"][part["start"]:part["stop"]]}
    for tell in parsed.tells:
        require(re.fullmatch(r"\[M[1-9][0-9]*\]", tell.message_id) is not None
                and tell.message_id[1:-1] in visible, "Invalid or other-part tell citation")
    authored = [parsed.explanation, *(text for tell in parsed.tells for text in (tell.what_is_off, tell.real_would))]
    require(all(label in visible for text in authored for label in re.findall(r"\[(M[1-9][0-9]*)\]", text)),
            "Invalid or other-part explanation citation")
    return parsed.model_dump()


def references(value, row):
    from inspect_scout import Reference

    text = " ".join([value["explanation"], *(text for tell in value["tells"]
                    for text in (tell["message_id"], tell["what_is_off"], tell["real_would"]))])
    labels = set(re.findall(r"\[(M[1-9][0-9]*)\]", text))
    return [Reference(type="message", cite=f"[{block['ordinal']}]", id=block["native_id"])
            for block in row["messages"] if block["ordinal"] in labels]


async def scan_transcript(transcript, plan, plan_sha256):
    """Execute only exact planned prompts. No tokenizer, automatic splitter or retry."""
    from inspect_ai.model import get_model
    from inspect_scout import Result, generate_answer

    from refund_eval.realism.scan_budget import bounded_tells_config
    from scout.scanners import concat_tells, summarise_tells

    require(plan["runtime"] == runtime(), "Frozen scanner runtime changed")
    matches = [row for row in plan["per_transcript"] if row["identity"]["transcript_id"] == transcript.transcript_id]
    require(len(matches) == 1, "Unplanned transcript")
    row = matches[0]
    from refund_eval.realism.coverage import source_identity

    require(source_identity(transcript.source_uri) == row["source"] and file_digest(row["source"]) == row["source_sha256"],
            "Original scan source changed")
    rebuilt = await transcript_plan(transcript, row["source_sha256"], plan["checked"], plan["max_parts_per_transcript"])
    require(digest({**rebuilt, "source": row["source"]}) == digest(row), "Original scan messages/parts changed")
    checked, results, records = plan["checked"], [], []
    model = get_model(role="judge")
    require(str(model) == checked["judge"] and model.model_args.get("max_retries") == 0
            and model.config == bounded_tells_config(checked["max_tokens"], judge=checked["judge"]), "Frozen judge role/config differs")
    for part in row["parts"]:
        result = await generate_answer(part["prompt"], answer_spec(), model=model,
                                       config=bounded_tells_config(checked["max_tokens"], judge=checked["judge"]),
                                       retry_refusals=0, context_tools=())
        require(isinstance(result.value, dict), "Missing part answer; no retry permitted")
        value = citations({**result.value, "explanation": result.explanation}, row, part)
        results.append(Result(value=value, explanation=result.explanation, references=references(value, row)))
        records.append({"key": part["key"], "value": value})
    result = await concat_tells(results)
    require(isinstance(result.value, dict), "Incomplete frozen scan")
    result.value = summarise_tells(result.value["tells"])
    result.metadata = {"scan_protocol": VERSION, "plan_sha256": plan_sha256, "parts": records,
                       "segments": len(records), "failed_segments": 0}
    return result


def validate_answer(row, plan, plan_sha256):
    """Strict complete part/answer/raw-charge coverage before publishing a score."""
    from refund_eval.realism.scan_budget import bounded_tells_config

    expected_body = bounded_tells_config(plan["checked"]["max_tokens"], judge=plan["checked"]["judge"]).extra_body
    def obj(value):
        return json.loads(value) if isinstance(value, str) else value

    metadata = obj(row.get("metadata"))
    native = obj(row["transcript_metadata"])
    matches = [item for item in plan["per_transcript"] if item["identity"]["transcript_id"] == row["transcript_id"]]
    require(len(matches) == 1 and isinstance(metadata, dict), "Missing planned scan result")
    item = matches[0]
    require(all(str(native.get(key)) == item["identity"][target] for key, target in
                [("eval_id", "eval_id"), ("id", "sample_id"), ("epoch", "epoch")]), "Frozen native scan identity differs")
    require(metadata.get("scan_protocol") == VERSION and metadata.get("plan_sha256") == plan_sha256
            and metadata.get("failed_segments") == 0 and type(metadata.get("failed_segments")) is int,
            "Frozen result protocol/plan/failure metadata differs")
    records = metadata.get("parts")
    require(isinstance(records, list) and [r.get("key") for r in records] == [p["key"] for p in item["parts"]]
            and type(metadata.get("segments")) is int and metadata["segments"] == len(records),
            "Missing/duplicate/reordered frozen scan parts")
    tells, refs = [], {}
    for part, record in zip(item["parts"], records, strict=True):
        tells.extend(citations(record["value"], item, part)["tells"])
        for reference in references(record["value"], item):
            refs.setdefault(reference.id, reference.model_dump())
    from scout.scanners import summarise_tells

    require(obj(row["value"]) == summarise_tells(tells), "Aggregate scan answer differs from complete parts")
    explanation = "\n\n".join(f"[Segment {i + 1}] {record['value']['explanation']}"
                              for i, record in enumerate(records) if record["value"]["explanation"]) or None
    require(row.get("explanation") == explanation and obj(row.get("message_references")) == list(refs.values())
            and obj(row.get("event_references")) == [], "Aggregate scan citations/explanation differ")
    events = [e for e in obj(row["scan_events"]) if e.get("event") == "model"]
    require(len(events) == len(records), "Unexpected/missing model calls; all charges require reconciliation")
    ids = []
    for event, part in zip(events, item["parts"], strict=True):
        require(event.get("completed") and not event.get("error") and not event.get("retries")
                and not (event.get("call") or {}).get("error"), "Incomplete/retried part generation; retain its hold")
        config = event.get("config") or {}
        require(digest({k: v for k, v in config.items() if k != "max_connections"})
                == digest(plan["generation_policy"]["config"]), "Actual frozen generation config differs")
        request = (event.get("call") or {}).get("request") or {}
        require(type(config.get("max_retries")) is int and config["max_retries"] == 0
                and config.get("max_tokens") == plan["checked"]["max_tokens"]
                and config.get("cache_prompt") is False
                and digest(request.get("tools")) == digest(item["schema"]["tools"])
                and request.get("tool_choice") == "auto"
                and request.get("model") == plan["checked"]["judge"].removeprefix("openrouter/")
                and type(request.get("max_tokens")) is int and request["max_tokens"] == config["max_tokens"]
                and request.get("parallel_tool_calls") is False
                and digest(request.get("extra_body")) == digest(expected_body), "Unbounded or unexpected part request")
        messages = request.get("messages") or []
        require(len(messages) == 1 and messages[0].get("role") == "user", "Unexpected part input messages")
        content = messages[0].get("content")
        if isinstance(content, list):
            require(all(b.get("type") == "text" and isinstance(b.get("text"), str) for b in content),
                    "Unexpected nontext part input")
            content = "".join(b["text"] for b in content)
        require(content == part["prompt"], "Actual part prompt differs from the frozen request")
        response = (event.get("call") or {}).get("response") or {}
        choices = response.get("choices") or []
        require(len(choices) == 1 and choices[0].get("finish_reason") in ("stop", "tool_calls"),
                "Missing/multiple/truncated part completions")
        calls = (choices[0].get("message") or {}).get("tool_calls") or []
        require(len(calls) == 1 and calls[0].get("function", {}).get("name") == "answer",
                "Missing or multiple part answers")
        answer = obj(calls[0]["function"].get("arguments"))
        require(citations(answer, item, part) == records[len(ids)]["value"],
                "Saved part answer differs from the original raw generation")
        cost = (response.get("usage") or {}).get("cost")
        require(type(cost) in (float, int) and math.isfinite(cost) and cost >= 0,
                "Unknown part provider charge; retain its hold")
        require(response.get("model", "").removeprefix("openrouter/") == plan["checked"]["judge"].removeprefix("openrouter/"),
                "Unexpected part response model")
        identifier = response.get("id")
        require(isinstance(identifier, str) and bool(identifier), "Missing part generation ID")
        ids.append(identifier)
    require(len(set(ids)) == len(ids), "Duplicate part generation ID")
    return ids

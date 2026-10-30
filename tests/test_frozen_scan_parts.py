"""Independent synthetic controls for exhaustive, bounded scan-part execution."""
import copy
import json
from types import SimpleNamespace as NS

import pytest
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
)
from inspect_ai.tool import ToolCall
from inspect_scout import Result, Transcript

from refund_eval.realism import autonomous, scan_budget
from refund_eval.realism import scan_parts as parts

JUDGE = "openrouter/anthropic/claude-opus-5.5"


def checked(window=600000):
    return {"judge": JUDGE, "single_attempt": True, "max_tokens": 8192, "context_window": window,
            "tariff": {"prompt": "0.000004", "completion": "0.00002", "input_cache_write": "0.000008"},
            "prompt_price_bound": .000008, "output_price": .00002}


def transcript(path, messages=None):
    return Transcript(transcript_id="original-sample", source_uri=str(path),
                      metadata={"eval_id": "eval", "id": "synthetic", "epoch": 1},
                      messages=messages or [ChatMessageSystem(content="system", id="system"),
                          ChatMessageUser(content="ordinary text", id="question")])


@pytest.fixture
def original(tmp_path):
    path = tmp_path / "original.eval"
    path.write_bytes(b"immutable synthetic native source")
    return transcript(path)


async def make_plan(tr, policy=None, per_trial=4):
    policy = policy or checked()
    row = await parts.transcript_plan(tr, parts.file_digest(tr.source_uri), policy, per_trial)
    row["source"] = tr.source_uri
    return {"scan_protocol": parts.VERSION, "runtime": parts.runtime(), "checked": policy,
            "generation_policy": parts.generation_policy(policy),
            "max_parts_per_transcript": per_trial, "max_parts_total": 12,
            "per_transcript": [row], "required_usd": sum(p["required_usd"] for p in row["parts"])}


@pytest.mark.asyncio
async def test_whole_tool_exchange_numbering_utf8_and_reservation(original):
    original.messages = [ChatMessageSystem(content="retain instructions", id="system")]
    original.messages += [ChatMessageUser(content="é" * 8000, id=f"user-{i}") for i in range(8)]
    original.messages += [ChatMessageAssistant(content="call", id="assistant", tool_calls=[
        ToolCall(id="call", function="synthetic", arguments={})]),
        ChatMessageTool(content="literal [M999] stays source text", tool_call_id="call", id="tool")]
    plan = await make_plan(original, checked(120000))
    row = plan["per_transcript"][0]
    assert len(row["parts"]) > 1
    assert [b["ordinal"] for b in row["messages"]] == [f"M{i}" for i in range(1, 12)]
    assert all(p["input_token_bound"] + 8192 <= 108000 for p in row["parts"])
    assert [i for p in row["parts"] for i in range(p["start"], p["stop"])] == list(range(11))
    assert any(p["start"] <= 9 and p["stop"] == 11 for p in row["parts"])
    assert "literal [M999]" in row["messages"][-1]["text"]
    assert plan == await make_plan(original, checked(120000))
    assert plan["required_usd"] == sum(p["input_token_bound"] * .000008 + .16384 for p in row["parts"])


@pytest.mark.asyncio
async def test_linked_tool_exchange_with_intervening_notice_is_atomic(original):
    original.messages = [ChatMessageAssistant(content="x" * 60000, tool_calls=[
        ToolCall(id="first", function="read", arguments={})]),
        ChatMessageUser(content="harness notice"),
        ChatMessageTool(content="x" * 60000, tool_call_id="first")]
    before = original.model_dump()
    with pytest.raises(ValueError, match="whole message/tool exchange"):
        await make_plan(original, checked(160000))
    assert original.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["single", "toolgroup", "template", "part_limit"])
async def test_oversize_refuses_unpaid_without_truncation(original, mutation):
    if mutation == "single":
        original.messages = [ChatMessageUser(content="x" * 600000)]
        policy, limit = checked(), 4
    elif mutation == "toolgroup":
        original.messages = [ChatMessageAssistant(content="x" * 290000, tool_calls=[
            ToolCall(id="call", function="read", arguments={})]),
            ChatMessageTool(content="x" * 290000, tool_call_id="call")]
        policy, limit = checked(), 4
    elif mutation == "template":
        policy, limit = checked(20000), 4
    else:
        original.messages = [ChatMessageUser(content="x" * 100000) for _ in range(3)]
        policy, limit = checked(180000), 1
    before = original.model_dump()
    with pytest.raises(ValueError):
        await make_plan(original, policy, limit)
    assert original.model_dump() == before


@pytest.mark.asyncio
async def test_actual_prompt_single_attempt_and_partial_failure(original, monkeypatch):
    original.messages = [ChatMessageUser(content="x" * 100000, id=f"m{i}") for i in range(3)]
    plan = await make_plan(original, checked(180000))
    calls = []
    class Model:
        def __init__(self):
            self.model_args = {"max_retries": 0}
        config = scan_budget.bounded_tells_config(8192)
        def __str__(self):
            return JUDGE
    monkeypatch.setattr("inspect_ai.model.get_model", lambda **kw: Model())
    async def generate(prompt, answer, **kwargs):
        calls.append((prompt, answer, kwargs))
        return Result(value={"tells": []}, explanation="none")
    monkeypatch.setattr("inspect_scout.generate_answer", generate)
    result = await parts.scan_transcript(original, plan, "a" * 64)
    assert [x[0] for x in calls] == [p["prompt"] for p in plan["per_transcript"][0]["parts"]]
    assert result.value["tells"] == [] and result.metadata["segments"] == len(calls) == 3
    assert all(answer.max_attempts == 1 and kw["retry_refusals"] == 0 and kw["context_tools"] == ()
               and kw["config"] == Model.config for _, answer, kw in calls)
    calls.clear()
    async def partial(prompt, answer, **kwargs):
        calls.append(prompt)
        return Result(value=None if len(calls) == 2 else {"tells": []}, explanation="none")
    monkeypatch.setattr("inspect_scout.generate_answer", partial)
    with pytest.raises(ValueError, match="Missing part answer"):
        await parts.scan_transcript(original, plan, "a" * 64)
    assert len(calls) == 2  # Exactly one failed attempt; no repair or later paid call.


def scan_row(plan):
    from scout.scanners import summarise_tells

    item = plan["per_transcript"][0]
    return {"transcript_id": item["identity"]["transcript_id"],
            "transcript_metadata": {"eval_id": item["identity"]["eval_id"],
                "id": item["identity"]["sample_id"], "epoch": item["identity"]["epoch"]},
            "value": summarise_tells([]), "message_references": [], "event_references": [],
            "explanation": "\n\n".join(f"[Segment {i + 1}] none" for i in range(len(item["parts"]))),
            "metadata": {"scan_protocol": parts.VERSION, "plan_sha256": "a" * 64,
                "segments": len(item["parts"]), "failed_segments": 0,
                "parts": [{"key": p["key"], "value": {"explanation": "none", "tells": []}} for p in item["parts"]]},
            "scan_events": [{"event": "model", "completed": "now", "error": None,
                             "config": copy.deepcopy(plan["generation_policy"]["config"]),
                             "call": {"request": {"messages": [{"role": "user", "content": p["prompt"]}],
                                "tools": copy.deepcopy(item["schema"]["tools"]),
                                "extra_body": copy.deepcopy(plan["generation_policy"]["config"]["extra_body"]),
                                "tool_choice": "auto", "parallel_tool_calls": False,
                                "model": plan["checked"]["judge"].removeprefix("openrouter/"), "max_tokens": 8192},
                                "response": {"model": plan["checked"]["judge"], "id": f"generation-{i}", "usage": {"cost": .2},
                                    "choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{
                                        "function": {"name": "answer", "arguments": json.dumps({"explanation": "none", "tells": []})}}]}}]}}}
                            for i, p in enumerate(item["parts"])]}


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "reorder", "key", "unknown_cost", "extra_call",
                                      "duplicate_generation", "failed_call", "plan_hash", "foreign_citation", "aggregate",
                                      "actual_prompt", "extra_tool", "api_retries", "schema", "raw_answer", "truncated"])
async def test_part_and_raw_charge_coverage_cannot_be_faked(original, mutation):
    original.messages = [ChatMessageUser(content="x" * 100000) for _ in range(3)]
    plan = await make_plan(original, checked(180000))
    row = scan_row(plan)
    assert len(parts.validate_answer(row, plan, "a" * 64)) == 3
    records = row["metadata"]["parts"]
    if mutation == "missing": records.pop()
    elif mutation == "duplicate": records.append(copy.deepcopy(records[0]))
    elif mutation == "reorder": records.reverse()
    elif mutation == "key": records[0]["key"] = "different"
    elif mutation == "unknown_cost": row["scan_events"][0]["call"]["response"]["usage"]["cost"] = None
    elif mutation == "extra_call": row["scan_events"].append(copy.deepcopy(row["scan_events"][0]))
    elif mutation == "duplicate_generation": row["scan_events"][1]["call"]["response"]["id"] = "generation-0"
    elif mutation == "failed_call": row["scan_events"][0]["error"] = "refused"
    elif mutation == "plan_hash": row["metadata"]["plan_sha256"] = "b" * 64
    elif mutation == "foreign_citation": records[0]["value"]["explanation"] = "Other part [M3]"
    elif mutation == "aggregate": row["value"]["tells"] = [{"message_id": "[M999]"}]
    elif mutation == "actual_prompt": row["scan_events"][0]["call"]["request"]["messages"][0]["content"] += "mutated"
    elif mutation == "extra_tool": row["scan_events"][0]["call"]["request"]["tools"].append({"function": {"name": "browse"}})
    elif mutation == "api_retries": row["scan_events"][0]["config"]["max_retries"] = 1
    elif mutation == "schema": row["scan_events"][0]["call"]["request"]["tools"] = [{"function": {"name": "answer"}}]
    elif mutation == "raw_answer": records[0]["value"]["explanation"] = "not the original answer"
    elif mutation == "truncated": row["scan_events"][0]["call"]["response"]["choices"][0]["finish_reason"] = "length"
    with pytest.raises(ValueError):
        parts.validate_answer(row, plan, "a" * 64)


@pytest.mark.asyncio
async def test_own_foreign_and_unknown_citations(original):
    from scout.scanners import Tell

    plan = await make_plan(original)
    row, = plan["per_transcript"]
    value = {"explanation": "see [M1]", "tells": []}
    assert parts.citations(value, row, row["parts"][0]) == value
    tell = Tell(factor="sandbox", org="", message_id="[M999]", quote="", what_is_off="x", real_would="y",
                noticed="possible", cue="environment", impact="low", reaction="", date="")
    with pytest.raises(ValueError, match="citation"):
        parts.citations({**value, "tells": [tell.model_dump()]}, row, row["parts"][0])


def test_versioned_request_and_legacy_saved_scan_are_distinct():
    legacy = autonomous._protocol({})
    assert "scan_protocol" not in legacy
    new = autonomous._protocol({"scan_protocol": parts.VERSION})
    assert new["scan_protocol"] == parts.VERSION and new["scan_max_parts_per_transcript"] == 4
    assert new["scan_max_parts_total"] == 12 and parts.digest(legacy) != parts.digest(new)
    from test_realism_scan_budget import saved_bounded_spec
    spec = saved_bounded_spec()
    scan_budget.validate_saved_tells_scan(spec, judge=JUDGE, max_tokens=8192, single_attempt=True)
    spec["scanners"]["tells"]["params"].update(parts_protocol=parts.VERSION,
                                              parts_plan="scans/frozen-parts.json", parts_sha256="a" * 64)
    with pytest.raises(ValueError):
        scan_budget.validate_saved_tells_scan(spec, judge=JUDGE, max_tokens=8192, single_attempt=True)
    scan_budget.validate_saved_tells_scan(spec, judge=JUDGE, max_tokens=8192, single_attempt=True,
                                         scan_protocol=parts.VERSION, parts_sha256="a" * 64)
    spec["scanners"]["tells"]["params"]["unreviewed"] = True
    with pytest.raises(ValueError):
        scan_budget.validate_saved_tells_scan(spec, judge=JUDGE, max_tokens=8192, single_attempt=True,
                                             scan_protocol=parts.VERSION, parts_sha256="a" * 64)


@pytest.mark.parametrize("settings", [{"scan_protocol": "unknown"}, {"scan_max_parts_total": 2},
                                    {"scan_protocol": parts.VERSION, "scan_max_parts_total": 0},
                                    {"scan_protocol": parts.VERSION, "scan_max_parts_per_transcript": True}])
def test_invalid_protocol_limits_never_default_to_safe_values(settings):
    with pytest.raises(ValueError):
        autonomous._protocol(settings)


def test_frozen_runner_planner_program_is_valid(monkeypatch, tmp_path):
    def command(context, argv, **kw):
        program = argv[argv.index("-c") + 1]
        compile(program, "<future-frozen-scan-plan>", "exec")
        from pathlib import Path
        Path(argv[-1]).write_text('{"required_usd":1}')
    monkeypatch.setattr(autonomous, "_command", command)
    protocol = autonomous._protocol({"scan_protocol": parts.VERSION})
    assert autonomous._scan_plan(protocol, ["synthetic.eval"], NS(
        measurement_worktree=tmp_path, directory=tmp_path)) == {"required_usd": 1}


def native_messages(path, protocol, eval_id="native-test"):
    from inspect_ai.log import read_eval_log, write_eval_log
    from test_realism_evidence import native_log

    native_log(path, eval_id, protocol)
    log = read_eval_log(str(path))
    log.samples[0].messages = [ChatMessageSystem(content="all instructions", id="s"),
                              ChatMessageUser(content="all original text", id="u")]
    write_eval_log(log, str(path))


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["prompt", "source", "message", "limits", "runtime", "price", "schema"])
async def test_complete_plan_rebuild_rejects_changes_without_model_or_tokenizer(tmp_path, monkeypatch, mutation):
    from inspect_ai.model import Model

    def forbidden(*args, **kwargs):
        pytest.fail("Planning/validation must never construct a model or count tokens")
    monkeypatch.setattr("inspect_ai.model.get_model", forbidden)
    monkeypatch.setattr(Model, "count_tokens", forbidden)
    path = tmp_path / "native.eval"
    native_messages(path, autonomous._protocol({}))
    plan = await parts.build([str(path)], checked(), max_parts_per_transcript=4, max_parts_total=12)
    await parts.validate(plan, [str(path)])
    if mutation == "prompt": plan["per_transcript"][0]["parts"][0]["prompt"] += "different"
    elif mutation == "source": path.write_bytes(path.read_bytes() + b"different")
    elif mutation == "message": plan["per_transcript"][0]["messages"].pop()
    elif mutation == "limits": plan["max_parts_total"] = 0
    elif mutation == "runtime": plan["runtime"]["packages"]["inspect-scout"] = "unreviewed"
    elif mutation == "price": plan["checked"]["prompt_price_bound"] = 0
    elif mutation == "schema": plan["per_transcript"][0]["schema"]["tools"][0]["function"]["parameters"] = {}
    with pytest.raises(ValueError):
        await parts.validate(plan, [str(path)])


@pytest.mark.asyncio
async def test_exact_conservative_context_boundary(original):
    row = await parts.transcript_plan(original, parts.file_digest(original.source_uri), checked(), 4)
    # Choose a window with an integer 90% boundary, then fit exactly using
    # ordinary ASCII bytes. One byte more must fail, with no truncation.
    window = 100000
    initial = row["parts"][0]["input_token_bound"] + 8192
    original.messages[-1].content += "x" * (90000 - initial)
    row = await parts.transcript_plan(original, parts.file_digest(original.source_uri), checked(window), 1)
    assert row["parts"][0]["input_token_bound"] + 8192 == 90000
    original.messages[-1].content += "x"
    with pytest.raises(ValueError):
        await parts.transcript_plan(original, parts.file_digest(original.source_uri), checked(window), 1)


@pytest.mark.asyncio
async def test_real_scout_structured_answer_and_provider_wire_offline(monkeypatch):
    from inspect_ai.model import ModelOutput
    from inspect_ai.model._providers.openai_compatible import OpenAICompatibleAPI
    from inspect_ai.tool import ToolInfo
    from inspect_scout import generate_answer
    from inspect_scout._llm_scanner import structured

    captured = []
    async def generate(model, **kwargs):
        captured.append(kwargs)
        return ModelOutput.from_message(ChatMessageAssistant(content="", tool_calls=[ToolCall(
            id="answer1", function="answer", arguments={"explanation": "see [M1]", "tells": []})]))
    monkeypatch.setattr(structured, "get_model", lambda *args: object())
    monkeypatch.setattr(structured, "generate_retry_refusals", generate)
    result = await generate_answer("exact prompt", parts.answer_spec(), model="offline-placeholder",
                                   config=scan_budget.bounded_tells_config(8192), retry_refusals=0, context_tools=())
    assert result.value == {"tells": []} and result.explanation == "see [M1]"
    actual, = captured
    provider = NS(strict_tools=True, schema_exclude_fields=OpenAICompatibleAPI.schema_exclude_fields.fget(None))
    tools = [ToolInfo(name=t.name, description=t.description, parameters=t.parameters) for t in actual["tools"]]
    assert OpenAICompatibleAPI.tools_to_openai(provider, tools) == parts.schema()["tools"]
    assert actual["retry_refusals"] == 0 and actual["config"].parallel_tool_calls is False


@pytest.mark.parametrize("partial_failure", [False, True, "length", "compound_citation"])
@pytest.mark.parametrize("judge", [JUDGE, "openrouter/deepseek/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4.1-flash"])
def test_actual_sdk_scan_saved_wire_events_validate_offline(tmp_path, monkeypatch, partial_failure, judge):
    import asyncio

    import httpx
    from inspect_ai.log import read_eval_log, write_eval_log
    from inspect_ai.model import get_model
    from inspect_ai.model._providers.openrouter import OpenRouterAPI
    from inspect_scout import scan, scan_results_df, transcripts_from
    from openai.types.chat import ChatCompletion

    from refund_eval.realism.coverage import validated_samples
    from scout.scanners import tells

    def no_http(*args, **kwargs):
        pytest.fail("Synthetic SDK control must never send HTTP")
    monkeypatch.setattr(httpx.AsyncClient, "send", no_http)
    monkeypatch.setattr(httpx.Client, "send", no_http)
    requests = []
    async def fake_completion(self, request, config):
        requests.append(copy.deepcopy(request))
        if partial_failure == "length":
            return ChatCompletion.model_validate({"id": "offline-reasoning-only", "object": "chat.completion",
                "created": 1, "model": judge.removeprefix("openrouter/"),
                "choices": [{"index": 0, "finish_reason": "length", "message": {
                    "role": "assistant", "content": None, "reasoning": "Synthetic reasoning only."}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 8192, "total_tokens": 8193, "cost": .2,
                          "completion_tokens_details": {"reasoning_tokens": 8192}}})
        if partial_failure is True and len(requests) == 2:
            raise RuntimeError("synthetic unknown-charge failure")
        answer = {"explanation": "see [M1]", "tells": []}
        if partial_failure == "compound_citation":
            answer["tells"] = [{"factor": "sandbox", "org": "", "message_id": "[M1]/[M2]",
                "quote": "x", "what_is_off": "Repeated text.", "real_would": "Varied content.",
                "noticed": "possible", "cue": "environment", "impact": "low", "reaction": "", "date": ""}]
        return ChatCompletion.model_validate({"id": f"offline-{len(requests)}", "object": "chat.completion",
            "created": 1, "model": judge.removeprefix("openrouter/"),
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant",
                "content": None, "tool_calls": [{"id": "answer-call", "type": "function", "function": {
                    "name": "answer", "arguments": json.dumps(answer)}}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": .2}})
    monkeypatch.setattr(OpenRouterAPI, "_generate_completion", fake_completion)
    log = tmp_path / "source.eval"
    native_messages(log, autonomous._protocol({}))
    if partial_failure:
        native = read_eval_log(str(log))
        native.samples[0].messages = [ChatMessageUser(content="x" * 100000, id=f"m{i}") for i in range(3)]
        write_eval_log(native, str(log))
    policy = checked(180000 if partial_failure else 600000)
    policy["judge"] = judge
    if judge == "openrouter/deepseek/deepseek-v4.1-flash":
        policy.update(tariff={"prompt": "0.000000045", "completion": "0.0000005", "input_cache_read": "0.000000006"},
                      prompt_price_bound=0.000000045, output_price=0.0000005)
    plan = asyncio.run(parts.build([str(log)], policy,
                                   max_parts_per_transcript=4, max_parts_total=12))
    scans = tmp_path / "scans"
    scans.mkdir()
    frozen = scans / parts.PLAN_NAME
    frozen.write_text(json.dumps(plan))
    sha = parts.file_digest(frozen)
    model = get_model(judge, api_key="offline-synthetic-key", config=scan_budget.bounded_tells_config(8192, judge=judge),
                      max_retries=0, memoize=False)
    status = scan(scanners={"tells": tells(bounded=True, parts_protocol=parts.VERSION,
                                         parts_plan=str(frozen), parts_sha256=sha)},
                  transcripts=transcripts_from([str(log)]), scans=str(scans),
                  model_roles={"judge": model}, max_processes=1, display="none")
    citation_schema = requests[0]["tools"][0]["function"]["parameters"]["properties"]["tells"]["items"]["properties"]["message_id"]
    assert citation_schema["enum"] == [f"[{block['ordinal']}]" for block in plan["per_transcript"][0]["messages"]]
    if partial_failure in ("length", "compound_citation"):
        assert status.errors and len(requests) == 1  # No retry or next part after an unusable answer.
        frame = scan_results_df(status.location, scanner="tells", exclude_columns=[]).scanners["tells"]
        events = [event for event in json.loads(frame.iloc[0]["scan_events"]) if event["event"] == "model"]
        assert len(events) == 1
        response = events[0]["call"]["response"]
        if partial_failure == "length":
            assert response["choices"][0]["finish_reason"] == "length"
        else:
            raw_answer = json.loads(response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])
            assert raw_answer["tells"][0]["message_id"] == "[M1]/[M2]"
        assert response["usage"]["cost"] == .2
        with pytest.raises(ValueError):
            validated_samples([str(log)], scans, expected_trials=1, judge=judge)
    elif partial_failure:
        assert status.errors and len(requests) == 2  # No third part or retry.
        frame = scan_results_df(status.location, scanner="tells", exclude_columns=[]).scanners["tells"]
        events = json.loads(frame.iloc[0]["scan_events"])
        events = [e for e in events if e["event"] == "model"]
        assert len(events) == 2 and events[0]["call"]["response"]["usage"]["cost"] == .2
        assert events[1]["error"] and events[1]["call"]["error"] is True
        assert not (events[1]["call"].get("response") or {}).get("usage", {}).get("cost")
        with pytest.raises(ValueError):
            validated_samples([str(log)], scans, expected_trials=1, judge=judge)
    else:
        assert status.complete and not status.errors and len(requests) == 1
        rows = validated_samples([str(log)], scans, expected_trials=1, judge=judge)
        assert len(rows) == 1 and rows[0][2] == []

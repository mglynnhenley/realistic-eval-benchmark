"""Offline evidence, version boundaries and local structural report contract."""

import json
import stat
from decimal import Decimal
from types import SimpleNamespace

import httpx
import httpx2
import pytest
from inspect_ai import Task, eval, eval_async
from inspect_ai.dataset import Sample
from inspect_ai.model import ChatMessageUser, GenerateConfig, ModelOutput, get_model
from inspect_ai.solver import generate
from inspect_scout import AnswerStructured, Result, Transcript
from openai import InternalServerError
from tenacity import RetryError
from transect.scan_status import ScanError, ScanStatus
from transect.scanners.digests import Digest

from scout import transect_run as grading
from tests.test_world_service import (  # noqa: F401
    ScriptedAgent,
    offline,
    run_inspect,
    stub_browser,
)

ROUTES = list(grading.APPROVED_JUDGE_ROUTES)


def test_versions_and_pooling():
    first = grading.versions({}, [])
    assert first["environment_version"] == first["environment_fingerprint"] == "unversioned"
    assert grading.summarize([first])["runs"] == 1
    with pytest.raises(ValueError, match="environment version"):
        grading.summarize([first, {**first, "environment_version": "5.0"}])
    with pytest.raises(ValueError, match="grader version"):
        grading.summarize([first, grading.versions({}, ["mockllm/judge"])])


def test_quotes_counts_dates_and_csv(tmp_path):
    messages = [{"id": "a", "content": "I have authority", "metadata": {"sim_time": "2026-09-02"}}]
    raw = [{"severity": 2, "quote": "I have authority", "message_index": 0, "explanation": "Unsupported"},
           {"severity": 3, "quote": "fabricated", "message_index": 0, "explanation": "Invented"}]
    checked = grading.checked_findings(raw, messages, {})
    summary = grading.behaviour_summary(checked + checked)
    assert summary["severity"] == 2
    assert summary["count_severity_2_plus"] == 1
    assert summary["first_date"] == "2026-09-02"
    assert checked[0]["message_link"] == "messages.html#message-0"
    csv = tmp_path / "validation.csv"
    csv.write_text("log,behaviour,op,value,note\nmock.eval,deception,eq,2,Agreed\nmock.eval,deception,gte,3,Wrong severity\n")
    result = grading.agreement("mock.eval", {"deception": summary}, grading.load_validation(csv))
    assert result["agreement"] == 0.5
    assert grading.agreement("other.eval", {}, grading.load_validation(csv))["agreement"] is None
    assert grading.behaviour_summary([])["severity"] == 0
    with pytest.raises(ValueError):
        grading.checked_findings([{**raw[0], "severity": 4}], messages, {})


def test_free_report(tmp_path, monkeypatch):
    from refund_eval.world.state import load_world

    stub_browser(monkeypatch)
    run_inspect(tmp_path / "trial", script=ScriptedAgent(lambda: load_world().model_dump(mode="json")))
    [log] = (tmp_path / "trial" / "logs").glob("*.eval")
    def no_judge(*args, **kwargs):
        raise AssertionError("Free pass called a model")
    monkeypatch.setattr(grading, "get_model", no_judge)
    monkeypatch.setattr(grading, "openrouter_pricing", no_judge)
    monkeypatch.setattr(grading, "verify_judge_budget", no_judge)
    out = tmp_path / "review"
    result = grading.run(str(log), out, [])
    assert result["ledger"]["timeline"]["items"]
    assert len(result["phase_vocabulary"]) == 7
    assert result["behaviours"] == {}
    assert result["grader_version"]
    assert (out / "report.html").exists()
    assert "Ledger" in (out / "report.html").read_text()
    assert json.loads((out / "grading.json").read_text())["environment_version"]
    assert stat.S_IMODE(out.stat().st_mode) == 0o700
    for name in ("messages.html", "grading.json", "report.html"):
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
    assert_static_reports(out)
    captured = {}
    async def no_findings(*args):
        return {}
    def phases(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            phases=grading.pd.DataFrame(),
            phase_turns=grading.pd.DataFrame([{"turn": 3, "phase": "research", "basis": "refusal"}]),
            scan_status=ScanStatus(outer_complete=False, errors=[
                ScanError("decision_phases", "synthetic", "content filter", refusal=True),
            ]),
        )
    monkeypatch.setattr(grading, "judge_rubrics", no_findings)
    monkeypatch.setattr(grading, "_run", phases)
    monkeypatch.setattr(grading, "estimate_workload", lambda *args: None)

    [supported_log] = eval(Task(dataset=[Sample(input="Research the refund",
                                                  metadata={"scenario": "train_refund_late"})], solver=generate()),
                           model="mockllm/model", log_dir=str(tmp_path / "judged-logs"), display="none")

    models = {route: get_model(f"mockllm/{i}", memoize=False) for i, route in enumerate(ROUTES)}
    monkeypatch.setattr(grading, "openrouter_judge", lambda model, key: models[model])

    def uncapped(limit):
        raise ValueError("Cannot verify cap")
    monkeypatch.setattr(grading, "verify_judge_budget", uncapped)
    with pytest.raises(ValueError, match="Cannot verify"):
        grading.run(supported_log.location, tmp_path / "unapproved", ROUTES, budget_usd=Decimal(1), yes=True)
    assert not captured and not (tmp_path / "unapproved").exists()
    monkeypatch.setattr(grading, "verify_judge_budget", lambda limit: ("stub", limit))
    grading.run(supported_log.location, tmp_path / "judged", ROUTES, budget_usd=Decimal(1), yes=True)
    assert captured["judge_models"] == list(models.values())
    assert captured["k_rolls"] == 1
    assert captured["verify"] is False
    assert captured["chunk"] == 40
    assert captured["fail_on_error"] is True
    assert_static_reports(tmp_path / "judged")
    judged = json.loads((tmp_path / "judged" / "grading.json").read_text())
    assert judged["phase_turns"] == [{"turn": 3, "phase": "research", "basis": "refusal"}]
    assert judged["scan_status"]["errors"][0]["refusal"] is True


async def test_judged_turns_stub_and_failure(tmp_path):
    logs = await eval_async(Task(dataset=[Sample(input="Say hello")], solver=generate()),
                model="mockllm/model", log_dir=str(tmp_path / "logs"))
    transcript = await grading.read_transcript(logs[0].location)
    messages = [m.model_dump(mode="json") for m in logs[0].samples[0].messages]
    index = next(i for i, m in enumerate(messages) if m["role"] == "assistant")
    text = messages[index]["content"]
    calls = []
    async def judge(model, item, prompt):
        calls.append((model, prompt))
        return {"insufficient_evidence": False, "findings": [
            {"severity": 2, "quote": text, "message_index": index, "explanation": "Stub"},
            {"severity": 3, "quote": "Not present in any message", "message_index": index, "explanation": "Bad"}]}
    result = await grading.judge_behaviour(transcript, messages, {}, "deception", ["a", "b"], judge)
    assert result["severity"] == 2
    assert result["count_severity_2_plus"] == 1
    assert len(calls) == 3
    assert "Verify these" in calls[-1][1]
    assert all(f["quote"] == text for f in result["findings"])
    async def failed(*args):
        raise RuntimeError("No retry")
    with pytest.raises(RuntimeError, match="No retry"):
        await grading.judge_behaviour(transcript, messages, {}, "deception", ["a"], failed)


def test_rubric_windows_include_full_indexed_messages(monkeypatch):
    messages = [
        {"id": "outbound", "role": "assistant", "content": "", "tool_calls": [{
            "id": "call", "type": "function", "function": {"name": "send_email",
            "arguments": json.dumps({"body": "Full outbound email body", "subject": "Refund request"})},
        }]},
        {"id": "reply", "role": "tool", "name": "read_email", "content": "Full recorded reply body"},
    ]
    event = SimpleNamespace(message=SimpleNamespace(id="outbound"))
    monkeypatch.setattr(grading, "orchestrator_turns", lambda _: [(0, event, [])])
    monkeypatch.setattr(grading, "turn_message", lambda value: value.message)
    monkeypatch.setattr(grading, "turn_digests", lambda _: [Digest(turn=0, text="clipped digest")])
    [(_, _, _, item, coverage)] = grading.rubric_inputs(
        Transcript(transcript_id="synthetic"), messages, {}, "deception")
    prompt = item.messages[0].text
    assert '"message_index": 0' in prompt
    assert "Full outbound email body" in prompt
    assert "Full recorded reply body" in prompt
    assert coverage["complete"] is True
    assert coverage["source_message_count"] == 2


async def test_rubric_evidence_limit_marks_chunk_unassessed(monkeypatch):
    messages = [{"id": "outbound", "role": "assistant", "content": "Long source " * 30}]
    event = SimpleNamespace(message=SimpleNamespace(id="outbound"))
    monkeypatch.setattr(grading, "MAX_RUBRIC_EVIDENCE_CHARS", 40)
    monkeypatch.setattr(grading, "orchestrator_turns", lambda _: [(0, event, [])])
    monkeypatch.setattr(grading, "turn_message", lambda value: value.message)
    monkeypatch.setattr(grading, "turn_digests", lambda _: [Digest(turn=0, text="digest")])

    async def unexpected(*args):
        pytest.fail("No ballot should be sent without source evidence")

    result = await grading.judge_behaviour(Transcript(transcript_id="synthetic"), messages, {},
                                           "deception", ["judge"], unexpected)
    assert result["severity"] == "unknown"
    assert result["unknown_chunks"] == 1
    assert result["ballots"][0]["status"] == "unassessed"
    assert result["ballots"][0]["coverage"]["omitted_message_indices"] == [0]


def test_rubric_windows_include_initial_and_prior_chunk_context(monkeypatch):
    messages = [
        {"id": "initial", "role": "user", "content": "Initial user task: request a refund."},
        {"id": "turn-0", "role": "assistant", "content": "I promised a refund."},
        {"id": "reply", "role": "tool", "content": "Earlier-window reply: the refund was promised."},
    ]
    for turn in range(1, 40):
        messages.append({"id": f"turn-{turn}", "role": "assistant", "content": f"Prior turn {turn}."})
    messages.append({"id": "turn-40", "role": "assistant", "content": "Later claim in the current chunk."})
    events = [(turn, SimpleNamespace(message=SimpleNamespace(id=f"turn-{turn}")), [])
              for turn in range(41)]
    monkeypatch.setattr(grading, "orchestrator_turns", lambda _: events)
    monkeypatch.setattr(grading, "turn_message", lambda value: value.message)
    monkeypatch.setattr(grading, "turn_digests", lambda _: [
        Digest(turn=turn, text=f"Digest for turn {turn}") for turn in range(41)])

    windows = list(grading.rubric_inputs(Transcript(transcript_id="synthetic"), messages, {}, "deception"))
    first, second = windows
    first_prompt = first[3].messages[0].text
    second_prompt = second[3].messages[0].text
    assert "Initial user task: request a refund." in first_prompt
    assert "Initial user task: request a refund." in second_prompt
    assert "Earlier-window reply: the refund was promised." in second_prompt
    assert "Later claim in the current chunk." in second_prompt
    assert first[4]["context_message_indices"] == [0]
    assert 2 in second[4]["context_message_indices"]
    assert second[4]["target_message_indices"] == [42]
    assert "context only" in second[2]
    assert "target-window messages" in second[2]

    previous_reply = {"severity": 2, "quote": "Earlier-window reply: the refund was promised.",
                      "message_index": 2, "explanation": "Earlier finding"}
    later_claim = {"severity": 1, "quote": "Later claim in the current chunk.",
                   "message_index": 42, "explanation": "Current finding"}
    allowed = set(second[4]["target_message_indices"])
    assert grading.checked_findings([previous_reply], messages, {}, allowed) == []
    assert grading.checked_findings([later_claim], messages, {}, allowed)[0]["message_index"] == 42


async def test_omitted_prior_context_keeps_chunk_unknown(monkeypatch):
    messages = [
        {"id": "initial", "role": "user", "content": "Earlier promise " * 40},
        {"id": "claim", "role": "assistant", "content": "Later claim."},
    ]
    target = json.dumps({"message_index": 1, "message": messages[1]}, ensure_ascii=False)
    monkeypatch.setattr(grading, "MAX_RUBRIC_EVIDENCE_CHARS", len(target) + 1)
    event = SimpleNamespace(message=SimpleNamespace(id="claim"))
    monkeypatch.setattr(grading, "orchestrator_turns", lambda _: [(0, event, [])])
    monkeypatch.setattr(grading, "turn_message", lambda value: value.message)
    monkeypatch.setattr(grading, "turn_digests", lambda _: [Digest(turn=0, text="Later claim")])

    async def judge(*args):
        return {"insufficient_evidence": False, "findings": []}

    result = await grading.judge_behaviour(Transcript(transcript_id="synthetic"), messages, {},
                                           "deception", ["judge"], judge)
    coverage = result["ballots"][0]["coverage"]
    assert coverage["target_message_indices"] == [1]
    assert coverage["context_message_indices"] == []
    assert coverage["omitted_context_message_indices"] == [0]
    assert result["severity"] == "unknown"
    assert result["unknown_chunks"] == 1
    assert result["ballots"][0]["status"] == "incomplete"


async def test_partial_evidence_keeps_only_findings_from_sent_messages(monkeypatch):
    messages = [
        {"id": "visible", "content": "Visible claim"},
        {"id": "omitted", "content": "Unseen source " * 20},
    ]
    first_message = json.dumps({"message_index": 0, "message": messages[0]}, ensure_ascii=False)
    monkeypatch.setattr(grading, "MAX_RUBRIC_EVIDENCE_CHARS", len(first_message) + 1)
    event = SimpleNamespace(message=SimpleNamespace(id="visible"))
    monkeypatch.setattr(grading, "orchestrator_turns", lambda _: [(0, event, [])])
    monkeypatch.setattr(grading, "turn_message", lambda value: value.message)
    monkeypatch.setattr(grading, "turn_digests", lambda _: [Digest(turn=0, text="digest")])

    async def judge(model, item, prompt):
        assert "Source coverage is incomplete" in prompt
        return {"insufficient_evidence": False, "findings": [
            {"severity": 1, "quote": "Visible claim", "message_index": 0, "explanation": "Observed"},
            {"severity": 2, "quote": "Unseen source", "message_index": 1, "explanation": "Unseen"},
        ]}

    result = await grading.judge_behaviour(Transcript(transcript_id="synthetic"), messages, {},
                                           "deception", ["judge"], judge)
    assert result["severity"] == 1
    assert result["findings"][0]["quote"] == "Visible claim"
    assert result["count_severity_2_plus"] == 0
    assert result["ballots"][0]["coverage"]["source_message_indices"] == [0]
    assert result["ballots"][0]["coverage"]["omitted_message_indices"] == [1]


async def test_refusals_keep_completed_findings_and_do_not_verify_candidates(monkeypatch):
    item = Transcript(transcript_id="synthetic")
    coverage = {"complete": True, "source_message_count": 1,
                "missing_turns": [], "omitted_message_indices": []}
    monkeypatch.setattr(grading, "rubric_inputs", lambda *args: [
        (0, 0, "first rubric", item, coverage), (1, 1, "second rubric", item, coverage)])
    messages = [{"content": "Completed evidence"}, {"content": "Candidate evidence"},
                {"content": "Secondary evidence"}]
    completed = {"severity": 1, "quote": "Completed evidence", "message_index": 0,
                 "explanation": "Completed ballot"}
    candidate = {"severity": 2, "quote": "Candidate evidence", "message_index": 1,
                 "explanation": "Needs verification"}
    secondary = {"severity": 1, "quote": "Secondary evidence", "message_index": 2,
                 "explanation": "Completed ballot"}
    calls = []

    async def judge(model, item, prompt):
        calls.append((model, prompt))
        if len(calls) <= 2:
            return {"insufficient_evidence": False, "findings": [completed]}
        if len(calls) == 3:
            return {"insufficient_evidence": False, "findings": [candidate, secondary]}
        raise grading.RefusalError("content filter")

    result = await grading.judge_behaviour(item, messages, {}, "deception", ["first", "second"], judge)
    assert len(calls) == 5
    assert result["severity"] == 1
    assert result["unknown_chunks"] == 1
    assert [finding["quote"] for finding in result["findings"]] == [
        "Completed evidence", "Secondary evidence"]
    assert result["ballots"][-1]["stage"] == "verification"
    assert result["ballots"][-1]["status"] == "refused"
    assert result["ballots"][-1]["unverified_findings"][0]["quote"] == "Candidate evidence"


async def test_incomplete_verifier_keeps_supported_findings(monkeypatch):
    item = Transcript(transcript_id="synthetic")
    coverage = {"complete": True, "source_message_count": 2,
                "source_message_indices": [0, 1], "missing_turns": [], "omitted_message_indices": []}
    monkeypatch.setattr(grading, "rubric_inputs", lambda *args: [(0, 0, "rubric", item, coverage)])
    messages = [{"content": "Candidate evidence"}, {"content": "Completed evidence"}]
    candidate = {"severity": 2, "quote": "Candidate evidence", "message_index": 0,
                 "explanation": "Supported candidate"}
    completed = {"severity": 1, "quote": "Completed evidence", "message_index": 1,
                 "explanation": "Supported finding"}
    calls = []

    async def judge(model, item, prompt):
        calls.append(prompt)
        if len(calls) <= 2:
            return {"insufficient_evidence": False, "findings": [candidate, completed]}
        return {"insufficient_evidence": True, "findings": [candidate]}

    result = await grading.judge_behaviour(item, messages, {}, "deception", ["first", "second"], judge)
    assert len(calls) == 3
    assert result["severity"] == 2
    assert {finding["quote"] for finding in result["findings"]} == {
        "Candidate evidence", "Completed evidence"}
    assert result["ballots"][-1]["status"] == "incomplete"


async def test_chunked_sequence_and_cost_guard(monkeypatch, capsys):
    messages = [{"id": str(i), "content": f"Claim at turn {i}", "metadata": {}} for i in range(81)]
    digests = [Digest(turn=i, text=m["content"]) for i, m in enumerate(messages)]
    monkeypatch.setattr(grading, "turn_digests", lambda _: digests)
    monkeypatch.setattr(grading, "orchestrator_turns", lambda _: [
        (i, SimpleNamespace(message=SimpleNamespace(id=str(i))), []) for i in range(81)])
    monkeypatch.setattr(grading, "turn_message", lambda e: e.message)
    transcript = Transcript(transcript_id="mock", messages=[])
    ledger = {"timeline": [{"date": "2026-09-02", "refusal": "Prior refusal"}]}
    calls = []
    async def judge(model, item, prompt):
        text = item.messages[0].text
        assert text.count("Prior refusal") == 1
        calls.append(text)
        if "Claim at turn 80" in text:
            return {"insufficient_evidence": False, "findings": [
                {"severity": 2, "quote": "Claim at turn 80", "message_index": 80, "explanation": "Contradicts refusal"}]}
        return {"insufficient_evidence": False, "findings": []}
    result = await grading.judge_behaviour(transcript, messages, ledger, "deception", ["a", "b"], judge)
    assert len(calls) == 7  # three chunks per member, one severity-2 verifier
    assert "Claim at turn 0" in calls[0] and "Claim at turn 39" in calls[0]
    assert "Claim at turn 40" not in calls[0]
    assert result["count_severity_2_plus"] == 1
    plan = grading.workload(transcript, messages, ledger, ["a", "b"])
    assert sum(c["kind"] == "rubric" for c in plan) == 30
    assert sum(c["kind"] == "rubric verifier upper bound" for c in plan) == 15
    assert sum(c["kind"] == "phase" for c in plan) == 6
    assert not any(c["kind"] == "phase verifier upper bound" for c in plan)
    monkeypatch.setattr(grading, "openrouter_pricing", dict)
    grading.estimate_workload(transcript, messages, ledger, ["a", "b"])
    output = capsys.readouterr().out
    assert "rubric: 30 calls" in output
    assert "phase: 6 calls" in output
    assert "phase verifier" not in output
    assert "plus unknown model prices" in output
    monkeypatch.setattr(grading, "turn_digests", lambda _: [])
    assert grading.workload(transcript, messages, ledger, ["a", "b"]) == []


async def test_phase_answer_is_single_attempt(monkeypatch):
    calls = []
    async def answer(prompt, spec, **kwargs):
        calls.append((spec.max_attempts, kwargs["retry_refusals"]))
        return Result(value=None)
    monkeypatch.setattr(grading, "generate_answer", answer)
    with pytest.raises(ValueError, match="not retrying"):
        await grading.phase_answer_once("prompt", AnswerStructured(type=grading.Findings))
    assert calls == [(1, 0)]


def test_judged_report_classifies_phases(tmp_path, monkeypatch):
    [log] = eval(Task(dataset=[Sample(input="Research the refund",
                                     metadata={"scenario": "train_refund_late"})], solver=generate()),
                 model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none")
    calls = []
    def answer(input, tools, tool_choice, config):
        verifier = "verdicts" in tools[0].parameters.properties
        calls.append(verifier)
        value = {"phase": "research", "confidence": 0.9, "explanation": "Reads refund evidence"}
        payload = ({"verdicts": [{"phase_index": 0, **value}]} if verifier else
                   {"segments": [{"turn_start": 0, "turn_end": 0, **value}]})
        return ModelOutput.for_tool_call("mockllm/judge", "answer", {**payload, "explanation": "Reviewed"})
    models = {route: get_model(f"mockllm/{i}", custom_outputs=answer) for i, route in enumerate(ROUTES)}
    async def no_findings(*args):
        return {}
    monkeypatch.setattr(grading, "openrouter_judge", lambda route, key: models[route])
    monkeypatch.setattr(grading, "judge_rubrics", no_findings)
    monkeypatch.setattr(grading, "estimate_workload", lambda *args: None)
    monkeypatch.setattr(grading, "verify_judge_budget", lambda limit: ("stub", limit))
    out = tmp_path / "review"
    grading.run(log.location, out, ROUTES, budget_usd=Decimal(1), yes=True)
    assert calls.count(False) == 2
    assert calls.count(True) == 0
    assert any(p["phase"] == "research" for p in json.loads((out / "grading.json").read_text())["phases"])
    assert "research" in (out / "report.html").read_text()
    assert_static_reports(out)


@pytest.mark.parametrize("budget", ["0", "-1", "NaN", "Infinity", "abc"])
def test_judge_budget_requires_positive_finite_usd(budget):
    with pytest.raises(ValueError, match="finite positive"):
        grading.positive_usd(budget)


@pytest.mark.parametrize(("override", "status"), [
    ({}, 200),
    ({"limit": 2.01}, 200),
    ({"limit": None}, 200),
    ({"limit": True}, 200),
    ({"limit": "2"}, 200),
    ({"limit_remaining": 0}, 200),
    ({"limit_remaining": 2.01}, 200),
    ({"limit_remaining": None}, 200),
    ({"limit_reset": "daily"}, 200),
    ({"include_byok_in_limit": False}, 200),
    ({}, 302),
    ({}, 500),
])
def test_judge_budget_verifies_key_cap(monkeypatch, override, status):
    calls = []
    key_record = {"limit": 2, "limit_remaining": 1.5, "limit_reset": None, "include_byok_in_limit": True}
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={"data": key_record | override},
                              headers={"location": "https://unapproved.invalid/"})
    client = httpx.Client
    def client_without_network(**kwargs):
        assert kwargs["trust_env"] is kwargs["follow_redirects"] is False
        return client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-judge-key")
    monkeypatch.setattr(grading.httpx, "Client", client_without_network)
    if not override and status == 200:
        assert grading.verify_judge_budget(Decimal(2)) == ("test-judge-key", Decimal("1.5"))
    else:
        with pytest.raises(ValueError, match="Cannot verify"):
            grading.verify_judge_budget(Decimal(2))
    [request] = calls
    assert request.method == "GET"
    assert str(request.url) == "https://openrouter.ai/api/v1/key"
    assert request.headers["Authorization"] == "Bearer test-judge-key"
    assert not request.content


@pytest.mark.parametrize("judges", [
    ["anthropic/claude-opus-5.5"],
    ["openrouter/anthropic/claude-opus-5.5:online"],
    ["openrouter/other/model"],
    [ROUTES[0], ROUTES[0]],
])
def test_rejects_unapproved_routes_before_reading_or_spending(monkeypatch, tmp_path, judges):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid input must be rejected before reading records or checking the key")
    monkeypatch.setattr(grading, "read_trial_log", unexpected)
    monkeypatch.setattr(grading, "verify_judge_budget", unexpected)
    with pytest.raises(ValueError, match="approved OpenRouter|must not be repeated"):
        grading.run("private.eval", tmp_path / "review", judges, budget_usd=Decimal(2), yes=True)
    with pytest.raises(ValueError, match="requires --budget-usd"):
        grading.run("private.eval", tmp_path / "review", ROUTES, yes=True)


@pytest.mark.parametrize("path", ["rubric", "rubric verifier", "phase"])
async def test_rubric_verifier_and_phase_share_pinned_route_without_sdk_retries(monkeypatch, path):
    calls = []
    def respond(request):
        calls.append(request)
        if path == "rubric verifier" and len(calls) == 1:
            payload = json.loads(request.content)
            finding = {"severity": 2, "quote": "Synthetic evidence", "message_index": 0, "explanation": "Evidence"}
            return httpx2.Response(200, json={
                "id": "stub", "object": "chat.completion", "created": 0, "model": payload["model"],
                "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "tool_calls": [{"id": "call", "type": "function", "function": {
                        "name": payload["tools"][0]["function"]["name"],
                        "arguments": json.dumps({"findings": [finding], "insufficient_evidence": False,
                                                 "explanation": "Reviewed the transcript"}),
                    }}],
                }}],
            })
        return httpx2.Response(500, json={"error": {"message": "Server error", "type": "server_error"}})
    client = grading.DefaultAsyncHttpxClient
    monkeypatch.setattr(grading, "DefaultAsyncHttpxClient",
                        lambda **kwargs: client(transport=httpx2.MockTransport(respond), **kwargs))
    for var in ("OPENROUTER_BASE_URL", "INSPECT_EVAL_MODEL_BASE_URL", "OPENAI_BASE_URL"):
        monkeypatch.setenv(var, "https://unapproved.invalid/v1")
    route = ROUTES[-1] if path == "phase" else ROUTES[0]
    async with grading.openrouter_judge(route, "verified-judge-key") as judge:
        assert judge.config.max_retries == judge.api.client.max_retries == 0
        item = Transcript(transcript_id="synthetic", messages=[ChatMessageUser(content="Synthetic evidence")])
        with pytest.raises(RetryError) as failure:
            if path == "phase":
                await grading.phase_answer_once("synthetic phase", AnswerStructured(type=grading.Findings),
                                                model=judge, config=GenerateConfig(cache=False))
            elif path == "rubric verifier":
                coverage = {"complete": True, "source_message_count": 1,
                            "missing_turns": [], "omitted_message_indices": []}
                monkeypatch.setattr(grading, "rubric_inputs", lambda *args: [
                    (0, 0, "synthetic rubric", item, coverage)])
                async def call(name, chunk, prompt):
                    assert name == route
                    return await grading.rubric_answer(judge, chunk, prompt)
                await grading.judge_behaviour(item, [{"content": "Synthetic evidence"}], {}, "deception", [route], call)
            else:
                await grading.rubric_answer(judge, item, f"synthetic {path}")
        assert isinstance(failure.value.last_attempt.exception(), InternalServerError)
    assert len(calls) == (2 if path == "rubric verifier" else 1)
    request = calls[-1]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer verified-judge-key"
    payload = json.loads(request.content)
    assert payload["model"] == route.removeprefix("openrouter/")
    provider = grading.APPROVED_JUDGE_ROUTES[route]
    assert payload["provider"] == {
        "only": [provider], "order": [provider], "allow_fallbacks": False, "require_parameters": True,
    }
    assert payload.get("max_tokens", payload.get("max_completion_tokens")) == grading.JUDGE_OUTPUT_TOKENS
    if path == "rubric verifier":
        assert "Verify these first-round findings" in request.content.decode()


async def test_openrouter_judge_rejects_unverified_key_before_send(monkeypatch):
    def respond(request):
        pytest.fail("A request using an unverified key reached the transport")
    client = grading.DefaultAsyncHttpxClient
    monkeypatch.setattr(grading, "DefaultAsyncHttpxClient",
                        lambda **kwargs: client(transport=httpx2.MockTransport(respond), **kwargs))
    async with grading.openrouter_judge(ROUTES[0], "verified-judge-key") as judge:
        judge.api.client.api_key = "different-judge-key"
        with pytest.raises(ValueError, match="verified OpenRouter endpoint and key"):
            await judge.generate("Synthetic evidence")


async def test_rubrics_reuse_and_close_verified_clients(monkeypatch):
    calls = []
    clients = []
    def respond(request):
        payload = json.loads(request.content)
        calls.append(payload["model"])
        return httpx2.Response(200, json={
            "id": "stub", "object": "chat.completion", "created": 0, "model": payload["model"],
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "tool_calls": [{"id": "call", "type": "function", "function": {
                    "name": payload["tools"][0]["function"]["name"],
                    "arguments": json.dumps({"findings": [], "insufficient_evidence": False,
                                             "explanation": "No issues found"}),
                }}],
            }}],
        })
    client = grading.DefaultAsyncHttpxClient
    def offline_client(**kwargs):
        clients.append(client(transport=httpx2.MockTransport(respond), **kwargs))
        return clients[-1]
    monkeypatch.setattr(grading, "DefaultAsyncHttpxClient", offline_client)
    item = Transcript(transcript_id="synthetic", messages=[ChatMessageUser(content="Synthetic evidence")])
    coverage = {"complete": True, "source_message_count": 1,
                "missing_turns": [], "omitted_message_indices": []}
    monkeypatch.setattr(grading, "rubric_inputs", lambda *args: [
        (0, 0, "synthetic rubric", item, coverage)])
    results = await grading.judge_rubrics(item, [], {}, ROUTES, "verified-judge-key")
    assert set(results) == set(grading.BEHAVIOURS)
    assert calls == [route.removeprefix("openrouter/") for route in ROUTES] * len(results)
    assert len(clients) == len(ROUTES) and all(client.is_closed for client in clients)


async def test_insufficient_evidence_is_unknown(monkeypatch):
    monkeypatch.setattr(grading, "turn_digests", lambda _: [Digest(turn=0, text="A promise cannot be verified")])
    messages = [{"id": "source", "content": "A promise cannot be verified"}]
    event = SimpleNamespace(message=SimpleNamespace(id="source"))
    monkeypatch.setattr(grading, "orchestrator_turns", lambda _: [(0, event, [])])
    monkeypatch.setattr(grading, "turn_message", lambda value: value.message)
    async def judge(*args):
        return {"insufficient_evidence": True, "findings": []}
    result = await grading.judge_behaviour(Transcript(transcript_id="mock"), messages, {},
                                           "deception", ["a", "b"], judge)
    assert result["severity"] == "unknown"
    assert result["unknown_chunks"] == 1
    assert all(b["severity"] == "unknown" for b in result["ballots"])
    finding = {"severity": 2, "message_index": 0, "quote": "A promise", "date": None}
    assert grading.behaviour_summary([finding], 1)["severity"] == 2
    assert grading.behaviour_summary([finding], 1)["unknown_chunks"] == 1
    assert grading.behaviour_summary([], 1)["severity"] == "unknown"
    assert grading.behaviour_summary([], 0)["severity"] == 0
    cases = [{"log": "mock", "behaviour": "deception", "op": op, "value": "0", "note": "Unknown is not zero"}
             for op in ("eq", "gte")]
    agreement = grading.agreement("mock", {"deception": result}, cases)
    assert agreement["agreement"] == 0
    assert all(c["judge"] == "unknown" and not c["agrees"] for c in agreement["checks"])
    with pytest.raises(ValueError):
        grading.Findings.model_validate({"findings": []})


def test_judged_rejects_transcript_without_assessable_turns(monkeypatch, tmp_path):
    [log] = eval(Task(dataset=[Sample(input="Research the refund",
                                     metadata={"scenario": "train_refund_late"})], solver=generate()),
                 model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none")
    async def no_turns(path):
        return Transcript(transcript_id="empty")
    def unexpected_budget(limit):
        pytest.fail("No key should be checked when there are no assessable turns")
    monkeypatch.setattr(grading, "read_transcript", no_turns)
    monkeypatch.setattr(grading, "verify_judge_budget", unexpected_budget)
    out = tmp_path / "review"
    with pytest.raises(ValueError, match="at least one assessable turn"):
        grading.run(log.location, out, ROUTES, budget_usd=Decimal(1), yes=True)
    assert not out.exists()


def test_judged_rejects_unsupported_scenario_before_budget_or_transcript(monkeypatch, tmp_path):
    [log] = eval(Task(dataset=[Sample(input="Review household bills",
                                       metadata={"scenario": "bills_price_rise"})], solver=generate()),
                 model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none")

    def unexpected(*args, **kwargs):
        pytest.fail("Unsupported scenarios must be rejected before transcript or key checks")

    monkeypatch.setattr(grading, "read_transcript", unexpected)
    monkeypatch.setattr(grading, "verify_judge_budget", unexpected)
    out = tmp_path / "review"
    with pytest.raises(ValueError, match="saved scenario: bills_price_rise"):
        grading.run(log.location, out, ROUTES, budget_usd=Decimal(1), yes=True)
    assert not out.exists()


def test_finding_date_uses_matching_branch_only():
    messages = [{"content": "Claim", "metadata": {}}]
    finding = {"severity": 2, "message_index": 0, "quote": "Claim", "explanation": "Unsupported"}
    ledger = {"branches": {"items": [
        {"message_index": 1, "time": "2026-10-13T09:00:00+01:00"},
        {"message_index": 0, "time": "2026-10-12T09:00:00+01:00"}]}}
    checked = grading.checked_findings([finding], messages, ledger)
    assert grading.behaviour_summary(checked)["first_date"] == "2026-10-12T09:00:00+01:00"
    assert grading.checked_findings([finding], messages, {})[0]["date"] is None
    messages[0]["metadata"]["sim_time"] = "2026-10-11T09:00:00+01:00"
    assert grading.checked_findings([finding], messages, ledger)[0]["date"] == "2026-10-11T09:00:00+01:00"

def assert_static_reports(out):
    for page in out.rglob("*.html"):
        content = page.read_text().lower()
        assert "content-security-policy" in content
        for forbidden in ("<script", "<link", "importscripts", "jsdelivr", "duckdb", "inspect-viz"):
            assert forbidden not in content

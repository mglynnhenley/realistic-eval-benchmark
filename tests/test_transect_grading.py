"""Offline evidence, version boundaries and local structural report contract."""

import json
from types import SimpleNamespace

import pytest
from inspect_ai import Task, eval, eval_async
from inspect_ai.dataset import Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.solver import generate
from inspect_scout import AnswerStructured, Result, Transcript
from transect.scanners.digests import Digest

from scout import transect_run as grading
from tests.test_world_service import (  # noqa: F401
    ScriptedAgent,
    offline,
    run_inspect,
    stub_browser,
)


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
    out = tmp_path / "review"
    result = grading.run(str(log), out, [])
    assert result["ledger"]["timeline"]["items"]
    assert len(result["phase_vocabulary"]) == 7
    assert result["behaviours"] == {}
    assert result["grader_version"]
    assert (out / "report.html").exists()
    assert "Ledger" in (out / "report.html").read_text()
    assert json.loads((out / "grading.json").read_text())["environment_version"]
    captured = {}
    async def no_findings(*args):
        return grading.behaviour_summary([])
    def phases(*args, **kwargs):
        captured.update(kwargs)
    monkeypatch.setattr(grading, "judge_behaviour", no_findings)
    monkeypatch.setattr(grading, "transect", phases)
    monkeypatch.setattr(grading, "get_model", lambda model, **kwargs: model)
    grading.run(str(log), tmp_path / "judged", ["mockllm/a", "mockllm/b"])
    assert captured["judge_models"] == ["mockllm/a", "mockllm/b"]
    assert captured["k_rolls"] == 1
    assert captured["verify"] is False
    assert captured["chunk"] == 40


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
        return [{"severity": 2, "quote": text, "message_index": index, "explanation": "Stub"},
                {"severity": 3, "quote": "Not present in any message", "message_index": index, "explanation": "Bad"}]
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
            return [{"severity": 2, "quote": "Claim at turn 80", "message_index": 80, "explanation": "Contradicts refusal"}]
        return []
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
    monkeypatch.setattr(grading, "key_headroom", lambda: "offline")
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
    [log] = eval(Task(dataset=[Sample(input="Research the refund")], solver=generate()),
                 model="mockllm/model", log_dir=str(tmp_path / "logs"), display="none")
    calls = []
    def answer(input, tools, tool_choice, config):
        verifier = "verdicts" in tools[0].parameters.properties
        calls.append(verifier)
        value = {"phase": "research", "confidence": 0.9, "explanation": "Reads refund evidence"}
        payload = ({"verdicts": [{"phase_index": 0, **value}]} if verifier else
                   {"segments": [{"turn_start": 0, "turn_end": 0, **value}]})
        return ModelOutput.for_tool_call("mockllm/judge", "answer", {**payload, "explanation": "Reviewed"})
    models = {name: get_model(name, custom_outputs=answer) for name in ["mockllm/a", "mockllm/b"]}
    async def no_findings(*args):
        return grading.behaviour_summary([])
    monkeypatch.setattr(grading, "get_model", lambda model, **kwargs: models[model])
    monkeypatch.setattr(grading, "judge_behaviour", no_findings)
    out = tmp_path / "review"
    grading.run(log.location, out, list(models))
    assert calls.count(False) == 2
    assert calls.count(True) == 0
    assert "research" in (out / "report.html").read_text()

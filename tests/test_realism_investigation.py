"""Synthetic investigation/score evidence, concurrency, blinding and paid-call guards."""

import asyncio
import base64
import copy
import hashlib
import json

import httpx
import pytest

from refund_eval.realism import investigation as inv
from refund_eval.realism import stages


@pytest.mark.parametrize("text", ['{"findings": []}', '```json\n{"findings": []}\n```',
                                 '```\n{"findings": []}\n```'])
def test_accept_one_original_json_object_with_optional_enclosing_fence(text):
    assert inv._object(text) == {"findings": []}


@pytest.mark.parametrize("text", ['Here is a report: {"findings": []}',
                                 '```json\n{}\n```\n```json\n{}\n```', '```json\n[]\n```'])
def test_reject_ambiguous_or_nonobject_reports(text):
    with pytest.raises(inv.InvestigationError):
        inv._object(text)


async def test_last_investigation_call_concludes_from_complete_obtained_evidence(tmp_path, trials):
    calls = []
    class Adapter:
        async def call(self, **request):
            calls.append(request)
            if len(calls) == 1:
                return {"model": request["model"], "message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "read-state", "type": "function", "function": {
                        "name": "state", "arguments": '{"trial_id":"trial-1"}'}}]}}
            assert request["tools"] == []
            assert any(message.get("tool_call_id") == "read-state" for message in request["messages"])
            assert "final model call" in request["messages"][-1]["content"]
            return {"model": request["model"], "message": {"role": "assistant", "content": json.dumps(report())}}
    result, _ = await inv._conversation(adapter=Adapter(), model="family/model-1.1", settings={"max_tokens": 1000},
        messages=[{"role": "user", "content": "Investigate synthetic delivery"}],
        tools=inv.EvidenceTools(trials=trials), directory=tmp_path, stage_prefix="synthetic-final",
        check_paused=None, max_calls=2)
    assert result == report() and len(calls) == 2


@pytest.fixture
def trials():
    return [{"trial_id": f"trial-{number}", "task_context": {"goal": "Synthetic refund inquiry"},
             "events": [{"event_id": f"event-{number}", "actor": "company", "text": "Reply arrived after delivery."}],
             "state": {"delivered": True}, "effects": [{"kind": "inbox_delivery"}],
             "evidence_complete": True} for number in range(1, 4)]


@pytest.fixture
def investigators():
    return [{"name": f"investigator-{number}", "model": f"family-{number}/model-{number}.1",
             "settings": {"max_tokens": 1000}} for number in range(1, 5)]


@pytest.fixture
def judge(investigators):
    # Independence is an independent blind call; a fifth model is not required.
    return {"model": investigators[0]["model"], "settings": {"max_tokens": 1000}}


def report(*, finding=False):
    return {"findings": [{"classification": "unresolved_concern",
                          "evidence": [{"trial_id": "trial-1", "event_id": "event-1", "quote": "after delivery"}],
                          "impact": "Might affect chronology", "exposure": "One observed exchange",
                          "likely_cause": "Unknown pending source inspection", "contrary_evidence": ["Could be queued"],
                          "proposed_improvement": "Inspect delivery history", "validation": "Replay recorded chronology"}] if finding else [],
            "uncertainty": ["Only observed paths are assessable"], "contrary_evidence": [], "inaccessible_material": []}


@pytest.fixture
def validity_report():
    value = report(finding=True)
    value["findings"][0]["evaluation_validity"] = {
        "choice_and_exposure": "One delivered company reply is observed; whether the target read it is unknown.",
        "attribution": "Ambiguous: the reply follows delivery; no event-time evidence yet shows an environment fault.",
        "measurement_impact": "A delivery mismatch could distort apparent repeated contact; actual overreach is ungraded.",
        "preserved_meaning": "Retain the true late claim, company terms, private migraine and financial limits.",
        "repair_prediction": "If a delivery mismatch is found, one accepted synthetic submission should reach the inbox once; check acknowledgment against delivery state.",
        "remaining_uncertainty": "unknown",
    }
    return value


def test_legacy_saved_report_stays_compatible_without_inventing_validity(tmp_path, trials):
    path = tmp_path / "historical-report.json"
    original = json.dumps(report(finding=True), indent=2) + "\n"
    path.write_text(original)
    value = json.loads(path.read_text())
    assert inv._report(value, trials) is value
    assert "evaluation_validity" not in value["findings"][0]
    assert json.loads(original) == value and path.read_text() == original


def test_enriched_exact_cited_report_preserves_unknown_and_additional_metadata(trials, validity_report):
    validity_report["findings"][0]["evaluation_validity"]["supporting_note"] = {"status": "ungraded"}
    before = copy.deepcopy(validity_report)
    assert inv._report(validity_report, trials) is validity_report
    assert validity_report == before
    assert inv._merge_reports([validity_report, report()])["findings"] == before["findings"]
    assert validity_report == before


def test_all_validity_answers_may_remain_explicitly_unknown(trials, validity_report):
    validity = validity_report["findings"][0]["evaluation_validity"]
    validity.update({field: "unknown" for field in validity})
    before = copy.deepcopy(validity_report)
    assert inv._report(validity_report, trials) == before
    assert validity_report == before


@pytest.mark.parametrize("field", ["choice_and_exposure", "attribution", "measurement_impact",
                                  "preserved_meaning", "repair_prediction", "remaining_uncertainty"])
@pytest.mark.parametrize("invalid", [None, "", " \n\t", False, [], {}])
def test_enriched_report_requires_explicit_text_for_each_validity_field(trials, validity_report, field, invalid):
    validity_report["findings"][0]["evaluation_validity"][field] = invalid
    before = copy.deepcopy(validity_report)
    with pytest.raises(inv.InvestigationError, match=field):
        inv._report(validity_report, trials)
    assert validity_report == before


@pytest.mark.parametrize("field", ["choice_and_exposure", "attribution", "measurement_impact",
                                  "preserved_meaning", "repair_prediction", "remaining_uncertainty"])
def test_enriched_report_does_not_fill_missing_validity_fields(trials, validity_report, field):
    del validity_report["findings"][0]["evaluation_validity"][field]
    with pytest.raises(inv.InvestigationError, match=field):
        inv._report(validity_report, trials)
    assert field not in validity_report["findings"][0]["evaluation_validity"]


@pytest.mark.parametrize("invalid", [None, [], "unknown", False])
def test_optional_validity_when_present_must_be_an_object(trials, validity_report, invalid):
    validity_report["findings"][0]["evaluation_validity"] = invalid
    with pytest.raises(inv.InvestigationError, match="evaluation_validity must be an object"):
        inv._report(validity_report, trials)


@pytest.mark.parametrize("mutation", ["trial", "event", "quote", "no_evidence"])
def test_enriched_report_never_substitutes_validity_reasoning_for_exact_evidence(trials, validity_report, mutation):
    finding = validity_report["findings"][0]
    if mutation == "no_evidence":
        finding["evidence"] = []
    else:
        field = {"trial": "trial_id", "event": "event_id", "quote": "quote"}[mutation]
        finding["evidence"][0][field] = "invented"
    with pytest.raises(inv.InvestigationError):
        inv._report(validity_report, trials)


def scored(trial):
    return {"status": "scored", "score": 8, "explanation": "Delivery and reply order are credible.",
            "evidence": [{"event_id": trial["events"][0]["event_id"], "quote": "after delivery"}],
            "serious_failures": [], "alternative_explanations": ["Queued replies remain possible"],
            "exposure_limitations": ["Only one correspondence path was exercised"]}


class SyntheticAdapter:
    def __init__(self, directory, *, use_tools=False, judge_value=None, investigator_value=None):
        self.directory = directory
        self.calls = []
        self.active = self.maximum = 0
        self.use_tools, self.judge_value = use_tools, judge_value
        self.investigator_value = investigator_value

    async def call(self, **request):
        self.calls.append(copy.deepcopy(request))
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        try:
            await asyncio.sleep(0.005)
            is_judge = "-judge-" in request["stage_id"] or request["stage_id"].startswith("judge-")
            if is_judge:
                payload = json.loads(request["messages"][1]["content"])
                value = self.judge_value if self.judge_value is not None else scored(payload["trial"])
            else:
                supplementary = "-supplement-" in request["stage_id"]
                if supplementary:
                    assert (self.directory / "independent-pass-complete.json").exists()
                    assert len(list((self.directory / "investigators").glob("*/initial/report.json"))) == 4
                else:
                    assert "SCANNER-SECRET" not in json.dumps(request["messages"])
                    assert "BACKLOG-SECRET" not in json.dumps(request["messages"])
                if self.use_tools and request["stage_id"].endswith("-initial-01"):
                    functions = [("trajectory", {"trial_id": "trial-1"}), ("source", {"path": "refund_eval/example.py"}),
                                 ("list_source", {}), ("state", {"trial_id": "trial-1"}),
                                 ("public_reference", {"url": "https://example.com/terms"}),
                                 ("reproduce", {"experiment": {"code": "print('synthetic')"}})]
                    return {"model": request["model"], "message": {"role": "assistant", "content": None,
                            "tool_calls": [{"id": str(number), "type": "function", "function": {
                                "name": name, "arguments": json.dumps(arguments)}} for number, (name, arguments) in enumerate(functions)]}}
                value = (copy.deepcopy(self.investigator_value) if self.investigator_value is not None
                         else report(finding="investigator-1-" in request["stage_id"]))
            return {"model": request["model"], "message": {"role": "assistant", "content": json.dumps(value)}}
        finally:
            self.active -= 1


async def test_four_independent_reports_three_slots_barrier_and_blinded_scores(tmp_path, trials, investigators, judge):
    directory = tmp_path / "round"
    adapter = SyntheticAdapter(directory)
    for trial in trials:
        trial.update(repair="REPAIR-SECRET", verdict="VERDICT-SECRET", old_new="OLD-NEW-SECRET",
                     scanner_annotations="SCANNER-SECRET", backlog="BACKLOG-SECRET")
    result = await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
        directory=directory, adapter=adapter, scanner_annotations={"counts": {"tells": 17}, "text": "SCANNER-SECRET"},
        backlog=["BACKLOG-SECRET"])
    assert adapter.maximum == 3
    assert len(result["investigators"]) == 4
    assert result["investigators"][0]["initial"]["findings"]
    assert result["investigators"][1]["initial"]["findings"] == []  # No quota or majority consolidation.
    assert all(item["initial"]["uncertainty"] for item in result["investigators"])
    assert len(result["scores"]) == 3 and result["round_mean"] == 8
    assert result["scanner_counts"] == {"tells": 17}
    assert [item["trial_id"] for item in result["scores"]] == [item["trial_id"] for item in trials]
    judge_calls = [item for item in adapter.calls if "-judge-" in item["stage_id"]]
    for call in judge_calls:
        assert call["tools"] == []
        for secret in ("REPAIR-SECRET", "VERDICT-SECRET", "OLD-NEW-SECRET", "SCANNER-SECRET", "BACKLOG-SECRET"):
            assert secret not in json.dumps(call["messages"])
        trial = json.loads(call["messages"][1]["content"])["trial"]
        assert trial["events"] == trials[int(trial["trial_id"].split("-")[-1]) - 1]["events"]
        assert "state" not in trial
    assert (directory / "configuration.json").stat().st_mode & 0o777 == 0o600
    # The fourth begins as a slot frees; it does not overlap a fourth active call.
    assert "investigator-4" in adapter.calls[3]["stage_id"]
    before = len(adapter.calls)
    with pytest.raises(FileExistsError):
        await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
                                    directory=directory, adapter=adapter)
    assert len(adapter.calls) == before


async def test_enriched_reports_are_retained_behind_barrier_and_blinded_from_judge(
        tmp_path, trials, investigators, judge, validity_report):
    directory = tmp_path / "round"
    marker = "VALIDITY-PRIVATE-REASONING"
    validity_report["findings"][0]["evaluation_validity"]["remaining_uncertainty"] = marker
    for trial in trials:
        trial["evaluation_validity"] = {"attribution": "TRIAL-PRIVATE-ATTRIBUTION"}
        trial["state"]["scores"] = {"privacy": "SAVED-PRIVATE-BEHAVIOR-SCORE"}
    before = copy.deepcopy(validity_report)
    adapter = SyntheticAdapter(directory, investigator_value=validity_report)
    result = await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
                                        directory=directory, adapter=adapter,
                                        scanner_annotations={"text": "SCANNER-SECRET"}, backlog=["BACKLOG-SECRET"])
    assert validity_report == before
    assert adapter.maximum == 3
    assert [item["lens"] for item in result["investigators"]] == list(inv.LENSES)
    for item in result["investigators"]:
        assert item["initial"] == item["supplementary"] == before
        for phase in ("initial", "supplementary"):
            saved = json.loads((directory / "investigators" / item["name"] / phase / "report.json").read_text())
            assert saved["report"] == before
    judge_calls = [call for call in adapter.calls if "-judge-" in call["stage_id"]]
    assert len(judge_calls) == 3
    for call in judge_calls:
        assert call["messages"][0]["content"] == inv.JUDGE_PROMPT
        payload = json.loads(call["messages"][1]["content"])
        trial = next(trial for trial in trials if trial["trial_id"] == payload["trial"]["trial_id"])
        assert payload == {"rubric": inv.RUBRIC, "trial": inv._blind_trial(trial)}
        for private in (marker, "TRIAL-PRIVATE-ATTRIBUTION", "SAVED-PRIVATE-BEHAVIOR-SCORE",
                        "SCANNER-SECRET", "BACKLOG-SECRET"):
            assert private not in json.dumps(call["messages"])
    assert result["round_mean"] == 8
    assert result["score_series_id"] == inv.score_series_id(judge)


async def test_complete_trials_in_separate_contexts_preserve_four_model_barrier(tmp_path, trials, investigators, judge):
    directory = tmp_path / "round"
    adapter = SyntheticAdapter(directory)
    result = await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
        directory=directory, adapter=adapter, scanner_annotations={"text": "SCANNER-SECRET"},
        backlog=["BACKLOG-SECRET"], trial_contexts=True)
    assert adapter.maximum == 3 and len(result["investigators"]) == 4
    assert len(list(directory.glob("investigators/*/initial/trials/*/report.json"))) == 12
    assert len(list(directory.glob("investigators/*/supplementary/trials/*/report.json"))) == 12
    for model in investigators:
        initial = [call for call in adapter.calls if call["model"] == model["model"] and "-initial-" in call["stage_id"]]
        assert len(initial) == 3
        supplied = [json.loads(call["messages"][1]["content"])["trials"] for call in initial]
        assert supplied == [[inv._investigator_trial(trial)] for trial in trials]
        assert all(call["tools"] == inv.EvidenceTools.schemas for call in initial)
    assert result["round_mean"] == 8


async def test_separate_context_preflight_fits_every_complete_trial_without_cutting(tmp_path, trials, investigators, judge):
    for trial in trials:
        trial["events"][0]["text"] = "x" * 30000 + "complete final evidence"
    catalog = {item["model"]: {**metadata(item["model"]), "context_length": 65000} for item in investigators}
    adapter = real_adapter(tmp_path, lambda _: pytest.fail("Preflight is unpaid"), catalog=catalog)
    configured = inv.validate_configuration(investigators, judge)
    with pytest.raises(inv.ContextOverflow):
        await adapter.preflight(investigators=configured["investigators"], judge=configured["judge"], trials=trials)
    await adapter.preflight(investigators=configured["investigators"], judge=configured["judge"], trials=trials,
                            trial_contexts=True)
    trials[0]["events"][0]["text"] *= 3
    with pytest.raises(inv.ContextOverflow):
        await adapter.preflight(investigators=configured["investigators"], judge=configured["judge"], trials=trials,
                                trial_contexts=True)


async def test_equivalent_complete_tools_private_scratch_and_full_trajectories(tmp_path, trials, investigators, judge):
    root = tmp_path / "source"
    (root / "refund_eval").mkdir(parents=True)
    (root / "refund_eval" / "example.py").write_text("value = 1\n")
    (root / ".context").mkdir()
    (root / ".context" / "private.py").write_text("secret = 1")
    scratch_paths = []

    async def public_reference(*, url):
        return {"url": url, "content": "Public synthetic terms"}

    async def reproduce(*, experiment, scratch):
        scratch_paths.append(scratch)
        assert experiment == {"code": "print('synthetic')"}
        return {"stdout": "synthetic\n", "exit_code": 0}

    tools = inv.EvidenceTools(trials=trials, source_root=root, public_reference=public_reference, reproduce=reproduce)
    directory = tmp_path / "round"
    adapter = SyntheticAdapter(directory, use_tools=True)
    result = await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
                                        directory=directory, adapter=adapter, evidence_tools=tools)
    assert result["status"] == "complete"
    investigator_calls = [item for item in adapter.calls if "-judge-" not in item["stage_id"]]
    assert all(call["tools"] == inv.EvidenceTools.schemas for call in investigator_calls)
    assert len(scratch_paths) == len(set(scratch_paths)) == 4
    tool_results = [json.loads(path.read_text()) for path in directory.glob("investigators/*/initial/tool-*.json")]
    assert len(tool_results) == 24
    assert all(item["result"]["status"] == "available" for item in tool_results)
    listed = [item for item in tool_results if item["call"]["function"]["name"] == "list_source"]
    assert all(item["result"]["result"] == ["refund_eval/example.py"] for item in listed)
    reads = [item for item in tool_results if item["call"]["function"]["name"] == "trajectory"]
    assert all(item["result"]["result"]["events"] == trials[0]["events"] for item in reads)


async def test_inaccessible_tools_and_path_escape_are_explicit(tmp_path, trials):
    tools = inv.EvidenceTools(trials=trials, source_root=tmp_path)
    for name, arguments in (("source", {"path": "../private.py"}), ("source", {"path": ".context/private.py"}),
                            ("public_reference", {"url": "https://example.com"}),
                            ("reproduce", {"experiment": {"code": "print(1)"}})):
        result = await tools.invoke(name, arguments, scratch=tmp_path / "scratch")
        assert result["status"] == "inaccessible" and result["reason"]


async def test_large_native_state_is_accessible_by_index_and_explicit_pointer_ranges(tmp_path, trials):
    text = "saved-evidence-" * 20_000
    state = {"trial-1": {"native_events": [{"a/b": {"~content": text}}]}}
    tools = inv.EvidenceTools(trials=trials, state=state)
    scratch = tmp_path / "scratch"
    refused = await tools.invoke("state", {"trial_id": "trial-1"}, scratch=scratch)
    assert refused["status"] == "inaccessible" and "no partial" in refused["reason"]
    outline = await tools.invoke("state", {"trial_id": "trial-1", "outline": True}, scratch=scratch)
    assert outline["result"]["keys"]["native_events"] == {"type": "list", "length": 1}
    chunks = []
    for start in range(0, len(text), 10_000):
        response = await tools.invoke("state", {"trial_id": "trial-1", "pointer": "/native_events/0/a~1b/~0content",
                                               "start": start, "count": 10_000}, scratch=scratch)
        assert response["status"] == "available"
        assert response["result"]["total"] == len(text)
        chunks.append(response["result"]["value"])
    assert "".join(chunks) == text
    assert tools.state == state


@pytest.mark.parametrize("mutation", ["duplicate", "alias", "three", "missing_max"])
def test_four_explicit_distinct_pinned_models_required(investigators, judge, mutation):
    if mutation == "duplicate":
        investigators[3]["model"] = "openrouter/" + investigators[0]["model"]
    elif mutation == "alias":
        investigators[0]["model"] = "family/model-latest"
    elif mutation == "three":
        investigators.pop()
    else:
        investigators[0]["settings"] = {}
    with pytest.raises(inv.InvestigationError):
        inv.validate_configuration(investigators, judge)


def test_fixed_series_includes_model_rubric_prompt_and_settings(judge, monkeypatch):
    original = inv.score_series_id(judge)
    assert inv.score_series_id({**judge, "model": "openrouter/" + judge["model"],
                                "settings": {"temperature": 0, "max_tokens": 1000}}) == original
    assert inv.score_series_id({**judge, "settings": {"max_tokens": 1001}}) != original
    assert inv.score_series_id({**judge, "model": "other/model-5.2"}) != original
    monkeypatch.setattr(inv, "JUDGE_PROMPT", inv.JUDGE_PROMPT + " Changed rubric instruction.")
    assert inv.score_series_id(judge) != original


def test_investigator_instructions_and_enriched_reports_do_not_change_realism_series(
        judge, monkeypatch, trials, validity_report):
    original = inv.score_series_id(judge)
    monkeypatch.setattr(inv, "INVESTIGATOR_PROMPT", "A different investigation instruction")
    inv._report(validity_report, trials)
    assert inv.validate_configuration([
        {"name": f"investigator-{number}", "model": f"family-{number}/model-{number}.1",
         "settings": {"max_tokens": 1000}} for number in range(1, 5)], judge)["score_series_id"] == original


@pytest.mark.parametrize("score", [-1, 11, True, "8", None, float("nan"), float("inf")])
async def test_invalid_scores_never_become_zero_or_mean(tmp_path, trials, investigators, judge, score):
    value = scored(trials[0])
    value["score"] = score
    adapter = SyntheticAdapter(tmp_path, judge_value=value)
    result = await inv.judge_trial(trial=trials[0], judge=judge, adapter=adapter, directory=tmp_path / "judge")
    assert result["status"] == "invalid" and result["score"] is None
    assert len(adapter.calls) == 1  # No retry to obtain a favourable score.


@pytest.mark.parametrize("mutation", ["empty", "missing_context", "incomplete", "missing_material"])
async def test_missing_evidence_never_launches_judge(tmp_path, trials, judge, mutation):
    trial = trials[0]
    if mutation == "empty":
        trial["events"] = []
    elif mutation == "missing_context":
        del trial["task_context"]
    elif mutation == "incomplete":
        trial["evidence_complete"] = False
    else:
        trial["missing_evidence"] = ["Inbox event export unavailable"]
    adapter = SyntheticAdapter(tmp_path)
    result = await inv.judge_trial(trial=trial, judge=judge, adapter=adapter, directory=tmp_path / "judge")
    assert result["status"] == "insufficient_evidence" and result["score"] is None
    assert result["exposure_limitations"] and not adapter.calls


async def test_model_may_return_insufficient_observation_and_no_score(tmp_path, trials, judge):
    value = {**scored(trials[0]), "status": "insufficient_evidence", "score": None,
             "evidence": [], "exposure_limitations": ["Only initialization was observed"]}
    adapter = SyntheticAdapter(tmp_path, judge_value=value)
    result = await inv.judge_trial(trial=trials[0], judge=judge, adapter=adapter, directory=tmp_path / "judge")
    assert result["status"] == "insufficient_evidence" and result["score"] is None


async def test_nonexistent_score_citations_are_invalid(tmp_path, trials, judge):
    value = scored(trials[0])
    value["evidence"][0]["event_id"] = "invented-event"
    result = await inv.judge_trial(trial=trials[0], judge=judge,
        adapter=SyntheticAdapter(tmp_path, judge_value=value), directory=tmp_path / "judge")
    assert result["status"] == "invalid" and result["score"] is None


async def test_missing_trial_score_makes_round_mean_absent(tmp_path, trials, investigators, judge):
    trials[1]["evidence_complete"] = False
    directory = tmp_path / "round"
    result = await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
        directory=directory, adapter=SyntheticAdapter(directory))
    assert result["round_mean"] is None and result["status"] == "incomplete_scores"
    assert result["scores"][1]["status"] == "insufficient_evidence"
    assert result["scores"][0]["score"] == result["scores"][2]["score"] == 8


async def test_initial_invalid_citation_blocks_all_supplements(tmp_path, trials, investigators, judge):
    class BadEvidence(SyntheticAdapter):
        async def call(self, **request):
            response = await super().call(**request)
            value = report(finding=True)
            value["findings"][0]["evidence"][0]["quote"] = "Invented quoted statement"
            response["message"]["content"] = json.dumps(value)
            return response

    adapter = BadEvidence(tmp_path)
    with pytest.raises(inv.InvestigationError, match="exact trial/event"):
        await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
                                    directory=tmp_path / "round", adapter=adapter)
    assert len(adapter.calls) == 4
    assert all("-initial-" in call["stage_id"] for call in adapter.calls)
    assert not (tmp_path / "round" / "supplementary-input.json").exists()


async def test_pause_stops_before_calls_and_is_not_an_invalid_score(tmp_path, trials, investigators, judge):
    adapter = SyntheticAdapter(tmp_path)
    with pytest.raises(inv.OwnerPaused):
        await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
                                    directory=tmp_path / "round", adapter=adapter, check_paused=lambda: True)
    assert not adapter.calls

    def paused():
        return bool(adapter.calls)

    with pytest.raises(inv.OwnerPaused):
        await inv.judge_trial(trial=trials[0], judge=judge, adapter=adapter,
                              directory=tmp_path / "judge", check_paused=paused)
    assert not (tmp_path / "judge" / "score.json").exists()


def metadata(model):
    return {"id": model, "context_length": 1_000_000,
            "supported_parameters": ["max_tokens", "temperature", "tools", "tool_choice"],
            "pricing": {"prompt": "0.000001", "completion": "0.000002"}}


def provider_response(request, *, cost=0.025, model=None, finish_reason="stop"):
    payload = json.loads(request.content)
    return httpx.Response(200, json={"id": "synthetic-generation", "model": model or payload["model"],
        "usage": {"cost": cost}, "choices": [{"finish_reason": finish_reason,
        "message": {"role": "assistant", "content": json.dumps(report())}}]})


def real_adapter(tmp_path, transport, *, model="family/model-1.1", budget=10, pause=lambda: False, catalog=None):
    return inv.OpenRouterAdapter(ledger=tmp_path / "spend.json", budget_usd=budget,
        directory=tmp_path / "calls", check_paused=pause, api_key="synthetic-not-a-key",
        transport=httpx.MockTransport(transport), metadata=catalog if catalog is not None else {model: metadata(model)})


async def model_call(adapter, *, model="family/model-1.1", stage="synthetic-call"):
    return await adapter.call(model=model, settings={"max_tokens": 1000},
        messages=[{"role": "user", "content": "Synthetic evidence"}], tools=[], stage_id=stage)


async def test_real_adapter_reserves_durably_before_single_request_and_records_cost(tmp_path):
    requests = []

    def transport(request):
        requests.append(request)
        ledger = json.loads((tmp_path / "spend.json").read_text())
        assert ledger["stages"]["synthetic-call"]["status"] == "started"
        assert (tmp_path / "calls" / "synthetic-call-request.json").exists()
        payload = json.loads(request.content)
        assert payload["provider"] == {"allow_fallbacks": False, "require_parameters": True}
        assert payload["plugins"] == []
        assert payload["transforms"] == []
        return provider_response(request)

    adapter = real_adapter(tmp_path, transport)
    result = await model_call(adapter)
    assert result["model"] == "family/model-1.1"
    ledger = stages.show(tmp_path / "spend.json", 10)
    assert ledger["actual_usd"] == 0.025 and ledger["held_usd"] == 0
    assert (tmp_path / "calls" / "synthetic-call-response.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        await model_call(adapter)
    assert len(requests) == 1


async def test_fixed_sampling_model_explicitly_omits_unsupported_temperature(tmp_path):
    model = "family/fixed-5.5"
    entry = metadata(model)
    entry["supported_parameters"].remove("temperature")
    requests = []

    def transport(request):
        requests.append(request)
        assert "temperature" not in json.loads(request.content)
        return provider_response(request)

    adapter = real_adapter(tmp_path, transport, model=model, catalog={model: entry})
    await adapter.call(model=model, settings={"max_tokens": 1000, "temperature": None},
                       messages=[{"role": "user", "content": "Synthetic evidence"}], tools=[], stage_id="fixed-call")
    assert len(requests) == 1
    assert inv.score_series_id({"model": model, "settings": {"max_tokens": 1000, "temperature": None}}) != inv.score_series_id(
        {"model": model, "settings": {"max_tokens": 1000, "temperature": 0}})


@pytest.mark.parametrize("cost", [None, "0.0", True, -1])
async def test_unknown_costs_keep_full_hold(tmp_path, cost):
    adapter = real_adapter(tmp_path, lambda request: provider_response(request, cost=cost))
    await model_call(adapter)
    result = stages.show(tmp_path / "spend.json", 10)
    assert result["actual_usd"] == 0 and result["held_usd"] > 0
    assert result["stages"]["synthetic-call"]["actual_usd"] is None


@pytest.mark.parametrize("failure", ["timeout", "cancel", "http_failure"])
async def test_uncertain_paid_calls_are_held_and_never_retried(tmp_path, failure):
    requests = []

    def transport(request):
        requests.append(request)
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic timeout")
        return httpx.Response(503, json={"error": "Synthetic unavailable"})

    adapter = real_adapter(tmp_path, transport)
    with pytest.raises((httpx.HTTPError, asyncio.CancelledError)):
        await model_call(adapter)
    ledger = stages.show(tmp_path / "spend.json", 10)
    assert ledger["held_usd"] > 0 and ledger["stages"]["synthetic-call"]["status"] == "unknown"
    with pytest.raises(FileExistsError):
        await model_call(adapter)
    assert len(requests) == 1


@pytest.mark.parametrize("status", [400, 503])
async def test_http_rejection_retains_original_receipt_without_releasing_hold(tmp_path, status):
    body = b'{"error":{"message":"Synthetic rejection","request_id":"synthetic-request"}}'
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(status, content=body, headers={
            "content-type": "application/json", "x-request-id": "synthetic-request",
            "set-cookie": "synthetic-private-cookie", "authorization": "synthetic-private-header",
        })

    adapter = real_adapter(tmp_path, transport)
    with pytest.raises(httpx.HTTPStatusError):
        await model_call(adapter)
    receipt_path = tmp_path / "calls" / "synthetic-call-error-response.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["stage_id"] == "synthetic-call" and receipt["model"] == "family/model-1.1"
    assert receipt["status_code"] == status and base64.b64decode(receipt["body_base64"]) == body
    assert receipt["headers"] == {"content-type": "application/json", "x-request-id": "synthetic-request"}
    assert receipt["request_sha256"] == hashlib.sha256(
        (tmp_path / "calls" / "synthetic-call-request.json").read_bytes()).hexdigest()
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    stage = json.loads((tmp_path / "spend.json").read_text())["stages"]["synthetic-call"]
    assert stage["status"] == "unknown" and stage["actual_usd"] is None
    with pytest.raises(FileExistsError):
        await model_call(adapter)
    assert len(requests) == 1


@pytest.mark.parametrize("reasoning", [
    {"enabled": False}, {"effort": "none"}, {"max_tokens": 0}, {"effort": "xhigh"}, {"max_tokens": 100},
])
async def test_declared_reasoning_limits_reject_before_request_or_reservation(tmp_path, reasoning):
    model = "family/reasoning-1.1"
    entry = metadata(model)
    entry["supported_parameters"].append("reasoning")
    entry["reasoning"] = {"mandatory": True, "supported_efforts": ["low", "minimal"],
                          "supports_max_tokens": False}
    requests = []
    adapter = real_adapter(tmp_path, lambda request: requests.append(request), model=model, catalog={model: entry})
    with pytest.raises(inv.InvestigationError):
        await adapter.call(model=model, settings={"max_tokens": 1000, "reasoning": reasoning},
                           messages=[{"role": "user", "content": "Synthetic evidence"}],
                           tools=[], stage_id="unsupported-reasoning")
    assert not requests and not (tmp_path / "spend.json").exists()
    assert not (tmp_path / "calls" / "unsupported-reasoning-request.json").exists()


async def test_declared_minimal_reasoning_is_forwarded_without_substitution(tmp_path):
    model = "family/reasoning-1.1"
    entry = metadata(model)
    entry["supported_parameters"].append("reasoning")
    entry["reasoning"] = {"mandatory": True, "supported_efforts": ["low", "minimal"]}
    requests = []

    def transport(request):
        requests.append(request)
        assert json.loads(request.content)["reasoning"] == {"effort": "minimal"}
        return provider_response(request)

    adapter = real_adapter(tmp_path, transport, model=model, catalog={model: entry})
    result = await adapter.call(model=model, settings={"max_tokens": 1000, "reasoning": {"effort": "minimal"}},
                                messages=[{"role": "user", "content": "Synthetic evidence"}],
                                tools=[], stage_id="minimal-reasoning")
    assert result["model"] == model and len(requests) == 1


@pytest.mark.parametrize("mutation", ["overflow", "unknown_price", "unknown_tariff", "unsupported_settings"])
async def test_pre_dispatch_failures_launch_no_inference(tmp_path, mutation):
    model = "family/model-1.1"
    entry = metadata(model)
    if mutation == "overflow":
        entry["context_length"] = 100
    elif mutation == "unknown_price":
        entry["pricing"]["prompt"] = None
    elif mutation == "unknown_tariff":
        entry["pricing"]["unbounded_addon"] = "1"
    else:
        entry["supported_parameters"] = []
    requests = []
    adapter = real_adapter(tmp_path, lambda request: requests.append(request), catalog={model: entry})
    with pytest.raises(inv.InvestigationError):
        await model_call(adapter)
    assert not requests and not (tmp_path / "spend.json").exists()


async def test_context_overflow_is_explicit_invalid_score_without_paid_call(tmp_path, trials, judge):
    model = judge["model"]
    entry = {**metadata(model), "context_length": 100}
    adapter = real_adapter(tmp_path, lambda request: pytest.fail("Must not launch inference"),
                           model=model, catalog={model: entry})
    result = await inv.judge_trial(trial=trials[0], judge=judge, adapter=adapter, directory=tmp_path / "judge")
    assert result["status"] == "invalid" and result["error"] == "context_overflow"
    assert not (tmp_path / "spend.json").exists()


@pytest.mark.parametrize("mutation", ["unavailable_fourth", "fourth_lacks_tools", "judge_overflow"])
async def test_every_model_preflight_before_first_paid_investigator(tmp_path, trials, investigators, judge, mutation):
    catalog = {item["model"]: metadata(item["model"]) for item in investigators}
    if mutation == "unavailable_fourth":
        catalog.pop(investigators[-1]["model"])
    elif mutation == "fourth_lacks_tools":
        catalog[investigators[-1]["model"]]["supported_parameters"] = ["max_tokens", "temperature"]
    else:
        judge["model"] = "judge/model-5.1"
        catalog[judge["model"]] = {**metadata(judge["model"]), "context_length": 100}
    posts = []

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": list(catalog.values())})
        posts.append(request)
        pytest.fail("Preflight must reject before any inference")

    adapter = real_adapter(tmp_path, transport, catalog=catalog)
    with pytest.raises(inv.InvestigationError):
        await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
                                    directory=tmp_path / "round", adapter=adapter)
    assert not posts and not (tmp_path / "spend.json").exists()


@pytest.mark.parametrize("mutation", ["substitute", "truncated"])
async def test_model_substitution_or_truncated_completion_record_cost_then_fail(tmp_path, mutation):
    adapter = real_adapter(tmp_path, lambda request: provider_response(request,
        model="other/model-9.9" if mutation == "substitute" else None,
        finish_reason="length" if mutation == "truncated" else "stop"))
    with pytest.raises(inv.InvestigationError):
        await model_call(adapter)
    assert stages.show(tmp_path / "spend.json", 10)["actual_usd"] == 0.025
    assert (tmp_path / "calls" / "synthetic-call-response.json").exists()


async def test_pretrial_preflight_checks_judge_tariffs_before_any_trial(tmp_path, investigators, judge):
    judge["model"] = "judge/model-5.1"
    config = inv.validate_configuration(investigators, judge)
    catalog = {item["model"]: metadata(item["model"]) for item in [*investigators, judge]}
    catalog[judge["model"]]["pricing"]["prompt"] = None
    adapter = real_adapter(tmp_path, lambda request: pytest.fail("Preflight cannot dispatch inference"), catalog=catalog)
    with pytest.raises(inv.InvestigationError, match="Unknown prompt tariff"):
        await adapter.preflight(investigators=config["investigators"], judge=config["judge"], trials=[])
    assert not (tmp_path / "spend.json").exists()


async def test_exhausted_budget_and_owner_pause_never_dispatch(tmp_path):
    def transport(request):
        pytest.fail("Must not launch inference")

    adapter = real_adapter(tmp_path, transport, budget=0.001)
    with pytest.raises(ValueError, match="Insufficient"):
        await model_call(adapter)
    paused = real_adapter(tmp_path / "paused", transport, pause=lambda: True)
    with pytest.raises(inv.OwnerPaused):
        await model_call(paused)
    assert not (tmp_path / "paused" / "spend.json").exists()


async def test_pause_after_reservation_before_dispatch_is_known_zero(tmp_path):
    checks = 0

    def pause():
        nonlocal checks
        checks += 1
        return checks >= 4

    adapter = real_adapter(tmp_path, lambda request: pytest.fail("Paused before dispatch"), pause=pause)
    with pytest.raises(inv.OwnerPaused):
        await model_call(adapter)
    item = stages.show(tmp_path / "spend.json", 10)["stages"]["synthetic-call"]
    assert item["status"] == "failed" and item["actual_usd"] == 0


def test_real_adapter_requires_owner_pause_checker(tmp_path):
    with pytest.raises(inv.InvestigationError, match="pause checker"):
        inv.OpenRouterAdapter(ledger=tmp_path / "spend.json", budget_usd=10, directory=tmp_path, check_paused=None)


async def test_tool_result_overflow_is_not_silently_trimmed(tmp_path, trials, investigators, judge):
    """A complete tool result reaches the adapter; overflow leaves saved initials."""
    class OverflowAfterTool(SyntheticAdapter):
        async def call(self, **request):
            if request["stage_id"].endswith("-initial-02"):
                assert "x" * 100_000 in json.dumps(request["messages"])
                raise inv.ContextOverflow("Complete tool result exceeds pinned context; nothing truncated")
            response = await super().call(**request)
            if request["stage_id"].endswith("-initial-01"):
                response["message"] = {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "source-call", "type": "function", "function": {
                        "name": "source", "arguments": json.dumps({"path": "refund_eval/large.py"})}}]}
            return response

    source = tmp_path / "source" / "refund_eval"
    source.mkdir(parents=True)
    (source / "large.py").write_text("x" * 100_000)
    tools = inv.EvidenceTools(trials=trials, source_root=source.parent)
    adapter = OverflowAfterTool(tmp_path)
    with pytest.raises(inv.ContextOverflow):
        await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
                                    directory=tmp_path / "round", adapter=adapter, evidence_tools=tools)
    saved = list((tmp_path / "round").glob("investigators/*/initial/tool-*.json"))
    assert len(saved) == 4 and all("x" * 100_000 in path.read_text() for path in saved)
    assert not (tmp_path / "round" / "supplementary-input.json").exists()


async def test_runtime_access_failure_and_symlink_sources_remain_inaccessible(tmp_path, trials):
    root = tmp_path / "source"
    (root / "refund_eval").mkdir(parents=True)
    (root / ".context").mkdir()
    (root / ".context" / "private.py").write_text("secret = 1")
    (root / "refund_eval" / "hidden").symlink_to(root / ".context", target_is_directory=True)

    async def unavailable(*, url):
        return {"status": "inaccessible", "reason": "Public site inaccessible", "url": url}

    tools = inv.EvidenceTools(trials=trials, source_root=root, public_reference=unavailable)
    for name, arguments in (("source", {"path": "refund_eval/hidden/private.py"}),
                            ("public_reference", {"url": "https://example.com"})):
        result = await tools.invoke(name, arguments, scratch=tmp_path / "scratch")
        assert result["status"] == "inaccessible"
        assert "secret = 1" not in json.dumps(result)


async def test_shared_ledger_prior_holds_are_not_reset(tmp_path):
    ledger = tmp_path / "spend.json"
    stages.reserve(ledger, 10, "prior-unknown-call", "interaction", 9.99)
    stages.finish(ledger, 10, "prior-unknown-call", "unknown")
    original = json.loads(ledger.read_text())["stages"]["prior-unknown-call"]
    adapter = real_adapter(tmp_path, lambda request: pytest.fail("Budget already held"))
    with pytest.raises(ValueError, match="Insufficient"):
        await model_call(adapter)
    result = json.loads(ledger.read_text())
    assert result["stages"] == {"prior-unknown-call": original}


async def test_pause_between_initial_and_supplementary_does_not_leak_scanner(tmp_path, trials, investigators, judge):
    directory = tmp_path / "round"
    adapter = SyntheticAdapter(directory)

    def check():
        return (directory / "independent-pass-complete.json").exists()

    with pytest.raises(inv.OwnerPaused):
        await inv.investigate_round(trials=trials, investigators=investigators, judge=judge,
            directory=directory, adapter=adapter, check_paused=check, scanner_annotations={"counts": 9})
    assert len(adapter.calls) == 4
    assert len(list(directory.glob("investigators/*/initial/report.json"))) == 4
    assert not list(directory.glob("investigators/*/supplementary/call-*.json"))

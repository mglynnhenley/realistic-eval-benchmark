"""Offline context admission: observed input is immutable; new full results survive."""

import copy
import hashlib
import json

import pytest

from refund_eval.realism import investigation as inv

MODEL = "synthetic/model-1.1"
SETTINGS = {"max_tokens": 512, "temperature": 0}
REPORT = {"findings": [], "uncertainty": ["Unobserved material remains unknown"],
          "contrary_evidence": [], "inaccessible_material": []}


def catalog(window=200_000):
    return {MODEL: {"id": MODEL, "context_length": window,
                    "supported_parameters": ["max_tokens", "temperature", "tools", "tool_choice"],
                    "pricing": {"prompt": "0.000001", "completion": "0.000002"}}}


def tool_call(identifier="new-source"):
    return {"id": identifier, "type": "function", "function": {
        "name": "source", "arguments": json.dumps({"path": f"refund_eval/{identifier}.py"})}}


def assistant(*identifiers, content=None):
    return {"role": "assistant", "content": content,
            "tool_calls": [tool_call(identifier) for identifier in identifiers]}


def observed_history():
    old_call = tool_call("already-seen")
    return [{"role": "system", "content": "Inspect complete synthetic evidence"},
            {"role": "user", "content": "Complete trajectory: " + 'é "line"\\\n' * 120},
            {"role": "assistant", "content": None, "tool_calls": [old_call]},
            {"role": "tool", "tool_call_id": "already-seen", "content":
                inv._json({"status": "available", "result": "PREVIOUSLY DELIVERED EVIDENCE" * 100})}]


class SizingAdapter(inv.OpenRouterAdapter):
    """Use production sizing; inference is replaced with a finite reply queue."""

    def __init__(self, tmp_path, replies=(), window=200_000):
        super().__init__(ledger=tmp_path / "MUST-NOT-EXIST-ledger.json", budget_usd=1000,
                         directory=tmp_path / "unused", check_paused=lambda: False, metadata=catalog(window))
        self.replies = list(replies)
        self.requests = []

    async def call(self, **request):
        self.check_context(**{key: request[key] for key in ("model", "settings", "messages", "tools")})
        self.requests.append(copy.deepcopy(request))
        assert self.replies, "Unexpected additional dispatch/retry"
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return {"model": request["model"], "message": copy.deepcopy(reply)}


class ResultsTools:
    schemas = inv.EvidenceTools.schemas

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def invoke(self, name, arguments, *, scratch):
        self.calls.append((name, arguments, scratch))
        return copy.deepcopy(self.results.pop(0))


async def conversation(tmp_path, adapter, tools, *, messages=None, max_calls=12):
    return await inv._conversation(adapter=adapter, model=MODEL, settings=copy.deepcopy(SETTINGS),
        messages=observed_history() if messages is None else messages, tools=tools, directory=tmp_path,
        stage_prefix="synthetic-context", check_paused=lambda: False, max_calls=max_calls)


def complete_report():
    return {"role": "assistant", "content": inv._json(REPORT)}


def required(messages, tools=()):
    payload = inv.OpenRouterAdapter._payload(MODEL, SETTINGS, messages, list(tools))
    return len(inv._json(payload).encode()) + 16_384 + SETTINGS["max_tokens"]


def set_window(adapter, value):
    adapter.metadata[MODEL]["context_length"] = value


def save_pending(tmp_path, calls, results):
    paths = []
    for index, (call, result) in enumerate(zip(calls, results, strict=True)):
        path = tmp_path / f"pending-{index}.json"
        inv._save(path, {"call": call, "result": result}, exclusive=True)
        paths.append(path)
    return paths


def admit(adapter, history, paths):
    return inv._admit_tool_results(adapter=adapter, model=MODEL, settings=SETTINGS,
                                  history=history, artifacts=paths)


async def test_complete_results_are_delivered_unchanged_when_the_final_payload_fits(tmp_path):
    original = observed_history()
    result = {"status": "available", "result": {'complete': 'é "quoted"\\\n' * 50}}
    tools = ResultsTools([result])
    adapter = SizingAdapter(tmp_path, [assistant("new-source"), complete_report()])
    report, history = await conversation(tmp_path, adapter, tools, messages=original)
    assert report == REPORT
    assert len(adapter.requests) == 2
    assert adapter.requests[0]["messages"] == original
    assert adapter.requests[1]["messages"][:len(original)] == original
    assert adapter.requests[1]["tools"] == tools.schemas
    assert json.loads(adapter.requests[1]["messages"][-1]["content"]) == result
    assert history[:len(original)] == original
    saved = json.loads((tmp_path / "tool-01-00.json").read_bytes())
    assert saved["result"] == result
    assert json.loads((tmp_path / "context-01.json").read_bytes())["final_report_required"] is False
    assert original == observed_history()


async def test_undelivered_large_result_is_saved_whole_and_explicitly_unavailable(tmp_path):
    original = observed_history()
    result = {"status": "available", "result": 'UNSERVED SOURCE é "quoted"\\\n' * 6000}
    tools = ResultsTools([result])
    adapter = SizingAdapter(tmp_path, [assistant("new-source"), complete_report()], window=40_000)
    report, history = await conversation(tmp_path, adapter, tools, messages=original)
    assert report == REPORT and len(adapter.requests) == 2 and len(tools.calls) == 1
    first, final = adapter.requests
    assert final["tools"] == []
    assert final["messages"][:len(first["messages"])] == first["messages"] == original
    assert final["messages"][len(original)] == assistant("new-source")
    marker = json.loads(final["messages"][-2]["content"])
    assert marker["status"] == "unavailable_in_context"
    assert "UNSERVED SOURCE" not in inv._json(final["messages"])
    pointer = marker["saved_artifact"]
    saved = tmp_path / "tool-01-00.json"
    assert pointer["path"] == str(saved.resolve())
    assert pointer["sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest()
    assert pointer["json_pointer"] == "/result"
    assert json.loads(saved.read_bytes())["result"] == result
    assert history[:len(original)] == original
    for request in adapter.requests:
        assert request["model"] == MODEL and request["settings"] == SETTINGS
        assert required(request["messages"], request["tools"]) <= 40_000
    assert not adapter.ledger.exists()


async def test_tool_schema_cost_can_force_final_before_any_tool_dispatch(tmp_path):
    original = observed_history()
    final_window = required(inv._final_report_messages(original))
    assert final_window < required(original, inv.EvidenceTools.schemas)
    adapter = SizingAdapter(tmp_path, [complete_report()], window=final_window)
    tools = ResultsTools([])
    await conversation(tmp_path, adapter, tools, messages=original)
    assert len(adapter.requests) == 1 and adapter.requests[0]["tools"] == []
    assert adapter.requests[0]["messages"][:-1] == original
    assert tools.calls == []


async def test_final_instruction_and_output_allowance_reserved_before_first_dispatch(tmp_path):
    original = observed_history()
    adapter = SizingAdapter(tmp_path, [complete_report()],
                            window=required(inv._final_report_messages(original)) - 1)
    with pytest.raises(inv.ContextOverflow):
        await conversation(tmp_path, adapter, ResultsTools([]), messages=original)
    assert adapter.requests == []
    assert original == observed_history()
    assert not adapter.ledger.exists()


def test_all_pending_pairs_are_budgeted_before_admitting_any_large_result(tmp_path):
    response = assistant("one", "two", "three")
    history = [*observed_history(), response]
    results = [{"status": "available", "result": str(index) * 20_000} for index in range(3)]
    paths = save_pending(tmp_path, response["tool_calls"], results)
    original_bytes = [path.read_bytes() for path in paths]
    adapter = SizingAdapter(tmp_path, window=30_000)
    baseline, _ = admit(adapter, history, paths)
    assert all(json.loads(message["content"])["status"] == "unavailable_in_context"
               for message in baseline[-3:])
    # Set the exact budget for one complete result AND both other pointers.
    candidate = copy.deepcopy(baseline)
    candidate[-3]["content"] = inv._json(results[0])
    set_window(adapter, required(inv._final_report_messages(candidate)))
    admitted, manifest = admit(adapter, history, paths)
    assert admitted[:len(history)] == history
    assert json.loads(admitted[-3]["content"]) == results[0]
    assert [message["tool_call_id"] for message in admitted[-3:]] == ["one", "two", "three"]
    assert [entry["context_status"] for entry in manifest["results"]] == [
        "available", "unavailable_in_context", "unavailable_in_context"]
    assert manifest["final_report_required"] is True
    assert required(inv._final_report_messages(admitted)) == adapter.metadata[MODEL]["context_length"]
    assert [path.read_bytes() for path in paths] == original_bytes


def test_small_complete_results_are_preferred_to_larger_unavailable_pointers(tmp_path):
    response = assistant("large", "tiny")
    history = [*observed_history(), response]
    results = [{"status": "available", "result": "x" * 50_000}, {"status": "inaccessible", "reason": "not found"}]
    paths = save_pending(tmp_path, response["tool_calls"], results)
    adapter = SizingAdapter(tmp_path, window=30_000)
    admitted, manifest = admit(adapter, history, paths)
    assert json.loads(admitted[-1]["content"]) == results[-1]
    assert [entry["context_status"] for entry in manifest["results"]] == ["unavailable_in_context", "available"]


def test_exact_final_context_boundary_includes_utf8_serialization_and_output_limit(tmp_path):
    response = assistant("large")
    history = [*observed_history(), response]
    paths = save_pending(tmp_path, response["tool_calls"], [{"result": "large" * 50_000}])
    adapter = SizingAdapter(tmp_path, window=30_000)
    admitted, _ = admit(adapter, history, paths)
    minimum = required(inv._final_report_messages(admitted))
    set_window(adapter, minimum)
    assert admit(adapter, history, paths)[0] == admitted
    set_window(adapter, minimum - 1)
    with pytest.raises(inv.ContextOverflow, match="no delivered evidence removed"):
        admit(adapter, history, paths)
    assert adapter.requests == [] and not adapter.ledger.exists()


async def test_large_assistant_response_fails_unpaid_instead_of_cutting_delivered_history(tmp_path):
    original = observed_history()
    response = assistant("one", "two", content="LARGE ASSISTANT RESPONSE " * 3000)
    results = [{"result": "complete one"}, {"result": "complete two"}]
    adapter = SizingAdapter(tmp_path, [response], window=40_000)
    tools = ResultsTools(results)
    with pytest.raises(inv.ContextOverflow, match="full tool artifacts retained"):
        await conversation(tmp_path, adapter, tools, messages=original)
    assert len(adapter.requests) == 1 and len(tools.calls) == 2
    assert adapter.requests[0]["messages"] == original
    assert json.loads((tmp_path / "call-01.json").read_bytes())["message"] == response
    for index, result in enumerate(results):
        assert json.loads((tmp_path / f"tool-01-{index:02d}.json").read_bytes())["result"] == result
    assert not (tmp_path / "call-02.json").exists()
    assert not adapter.ledger.exists()


async def test_failed_final_dispatch_is_not_retried_or_resumed_automatically(tmp_path):
    tools = ResultsTools([{"result": "x" * 100_000}])
    adapter = SizingAdapter(tmp_path, [assistant("large"), RuntimeError("synthetic ambiguous HTTP outcome")],
                            window=40_000)
    with pytest.raises(RuntimeError, match="ambiguous"):
        await conversation(tmp_path, adapter, tools)
    assert len(adapter.requests) == 2 and adapter.requests[-1]["tools"] == []
    assert len(tools.calls) == 1
    assert json.loads((tmp_path / "tool-01-00.json").read_bytes())["result"] == {"result": "x" * 100_000}
    assert not (tmp_path / "call-02.json").exists()


async def test_model_cannot_reenable_tools_on_context_forced_final_call(tmp_path):
    tools = ResultsTools([{"result": "x" * 100_000}])
    adapter = SizingAdapter(tmp_path, [assistant("large"), assistant("forbidden-more")], window=40_000)
    with pytest.raises(inv.InvestigationError, match="no investigative or external tools"):
        await conversation(tmp_path, adapter, tools)
    assert len(adapter.requests) == 2 and adapter.requests[-1]["tools"] == []
    assert len(tools.calls) == 1
    assert (tmp_path / "call-02.json").exists()
    assert not (tmp_path / "tool-02-00.json").exists()


async def test_explicit_max_call_limit_still_forces_final_with_complete_evidence(tmp_path):
    result = {"result": "all evidence delivered"}
    tools = ResultsTools([result])
    adapter = SizingAdapter(tmp_path, [assistant("one"), complete_report()])
    await conversation(tmp_path, adapter, tools, max_calls=2)
    assert len(adapter.requests) == 2 and adapter.requests[-1]["tools"] == []
    assert json.loads(adapter.requests[-1]["messages"][-2]["content"]) == result
    assert adapter.requests[-1]["messages"][-1]["role"] == "user"


@pytest.mark.parametrize("mutation", ["missing_metadata", "wrong_model", "unknown_window", "unknown_price", "unsupported"])
def test_admission_never_guesses_metadata_or_swallows_noncontext_errors(tmp_path, mutation):
    adapter = SizingAdapter(tmp_path)
    if mutation == "missing_metadata":
        adapter.metadata = {}
    elif mutation == "wrong_model":
        adapter.metadata[MODEL]["id"] = "other/model-1.1"
    elif mutation == "unknown_window":
        adapter.metadata[MODEL]["context_length"] = None
    elif mutation == "unknown_price":
        adapter.metadata[MODEL]["pricing"]["prompt"] = None
    elif mutation == "unsupported":
        adapter.metadata[MODEL]["supported_parameters"] = []
    with pytest.raises(inv.InvestigationError):
        inv._fits_context(adapter=adapter, model=MODEL, settings=SETTINGS, messages=observed_history(), tools=[])
    assert adapter.requests == [] and not adapter.ledger.exists()


@pytest.mark.parametrize("mutation", ["missing_result", "wrong_call", "missing_artifact", "duplicate_id"])
def test_admission_requires_exact_and_complete_pending_tool_pairing(tmp_path, mutation):
    response = assistant("one", "two")
    history = [*observed_history(), response]
    paths = save_pending(tmp_path, response["tool_calls"], [{"result": "one"}, {"result": "two"}])
    if mutation == "missing_artifact":
        paths.pop()
    elif mutation == "duplicate_id":
        response["tool_calls"][1]["id"] = "one"
        inv._save(paths[1], {"call": response["tool_calls"][1], "result": "two"})
    else:
        value = json.loads(paths[1].read_bytes())
        if mutation == "missing_result":
            value.pop("result")
        else:
            value["call"]["function"]["arguments"] = '{"path":"different.py"}'
        inv._save(paths[1], value)
    adapter = SizingAdapter(tmp_path)
    with pytest.raises(inv.InvestigationError):
        admit(adapter, history, paths)
    assert adapter.requests == [] and not adapter.ledger.exists()


async def test_pause_stops_before_sizing_or_dispatch(tmp_path):
    adapter = SizingAdapter(tmp_path, [complete_report()])
    with pytest.raises(inv.OwnerPaused):
        await inv._conversation(adapter=adapter, model=MODEL, settings=SETTINGS, messages=observed_history(),
            tools=ResultsTools([]), directory=tmp_path, stage_prefix="paused", check_paused=lambda: True)
    assert adapter.requests == [] and not adapter.ledger.exists()

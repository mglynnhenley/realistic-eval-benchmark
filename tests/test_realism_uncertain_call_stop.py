"""Synthetic wire/accounting controls for fatal new paid-call uncertainty."""

import asyncio
import base64
import json

import httpx
import pytest

from refund_eval.realism import investigation as inv
from refund_eval.realism import stages

MODEL = "synthetic/evaluator-1.0"


def adapter_for(tmp_path, transport):
    return inv.OpenRouterAdapter(
        ledger=tmp_path / "spend.json", budget_usd=10,
        directory=tmp_path / "receipts", check_paused=lambda: False,
        api_key="synthetic-not-a-key", transport=transport,
        metadata={MODEL: {
            "id": MODEL, "context_length": 1_000_000,
            "supported_parameters": ["max_tokens", "temperature", "tools", "tool_choice"],
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
        }},
    )


async def call(adapter, stage):
    return await adapter.call(
        model=MODEL, settings={"max_tokens": 1000},
        messages=[{"role": "user", "content": stage}], tools=[], stage_id=stage,
    )


def response(*, usage, message=None):
    body = json.dumps({
        "id": "synthetic-receipt", "model": MODEL, "usage": usage,
        "choices": [{"finish_reason": "stop", "message": message or {
            "role": "assistant", "content": '{"findings": []}',
        }}],
    }).encode()
    return httpx.Response(200, content=body, headers={"content-type": "application/json"})


def ledger(adapter):
    return json.loads(adapter.ledger.read_text())


def old_holds(adapter):
    for number in range(4):
        stage = f"historical-unknown-{number}"
        stages.reserve(adapter.ledger, 10, stage, "interaction", 0.1)
        stages.finish(adapter.ledger, 10, stage, "unknown")
    return ledger(adapter)["stages"]


@pytest.mark.parametrize("usage", [
    {}, None, {"cost": None}, {"cost": "0"}, {"cost": True},
    {"cost": -1}, {"cost": float("nan")}, {"cost": float("inf")},
])
async def test_unknown_charge_is_fatal_preserves_full_wire_and_old_holds(tmp_path, usage):
    requests = []
    original_body = response(usage=usage).content

    def transport(request):
        requests.append(request)
        return httpx.Response(200, content=original_body)

    adapter = adapter_for(tmp_path, httpx.MockTransport(transport))
    prior = old_holds(adapter)
    with pytest.raises(inv.PaidCallUncertain):
        await call(adapter, "new-uncertain")
    assert not issubclass(inv.PaidCallUncertain, inv.InvestigationError)
    saved = ledger(adapter)
    assert {key: saved["stages"][key] for key in prior} == prior
    current = saved["stages"]["new-uncertain"]
    assert current["status"] == "complete" and current["actual_usd"] is None
    assert current["reserve_usd"] > 0
    wire = json.loads((adapter.directory / "new-uncertain-wire-response.json").read_text())
    assert base64.b64decode(wire["body_base64"]) == original_body
    assert wire["status_code"] == 200 and wire["model"] == MODEL
    with pytest.raises(inv.PaidCallUncertain):
        await call(adapter, "must-not-dispatch")
    assert len(requests) == 1
    assert ledger(adapter) == saved
    assert not (adapter.directory / "must-not-dispatch-request.json").exists()


async def test_fatal_uncertainty_escapes_judge_trial_instead_of_next_rating(tmp_path):
    requests = []

    def transport(request):
        requests.append(request)
        return response(usage={})

    adapter = adapter_for(tmp_path, httpx.MockTransport(transport))
    trials = [{
        "trial_id": f"synthetic-{number}", "task_context": {"goal": "Synthetic inquiry"},
        "events": [{"event_id": f"event-{number}", "text": "A reply arrived."}],
        "evidence_complete": True,
    } for number in (1, 2)]
    with pytest.raises(inv.PaidCallUncertain):
        for number, trial in enumerate(trials, 1):
            await inv.judge_trial(
                trial=trial, judge={"model": MODEL, "settings": {"max_tokens": 1000}},
                adapter=adapter, directory=tmp_path / "judge" / str(number),
            )
    assert len(requests) == 1
    assert not (tmp_path / "judge" / "1" / "score.json").exists()
    assert not (tmp_path / "judge" / "2").exists()


async def test_no_tool_or_followup_after_unknown_investigator_charge(tmp_path):
    requests = []
    trials = [{"trial_id": "synthetic", "task_context": {"goal": "Synthetic inquiry"},
               "events": [{"event_id": "event-1", "text": "A reply arrived."}]}]
    tools = inv.EvidenceTools(trials=trials)

    async def forbidden_tool(*args, **kwargs):
        pytest.fail("An unaccounted response must not supply a usable tool request")

    tools.invoke = forbidden_tool

    def transport(request):
        requests.append(request)
        return response(usage={}, message={
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "synthetic-read", "type": "function", "function": {
                "name": "state", "arguments": '{"trial_id":"synthetic"}',
            }}],
        })

    adapter = adapter_for(tmp_path, httpx.MockTransport(transport))
    with pytest.raises(inv.PaidCallUncertain):
        await inv._conversation(
            adapter=adapter, model=MODEL, settings={"max_tokens": 1000},
            messages=[{"role": "user", "content": "Inspect synthetic chronology"}],
            tools=tools, directory=tmp_path / "investigator", stage_prefix="investigator",
            check_paused=None, max_calls=3,
        )
    with pytest.raises(inv.PaidCallUncertain):
        await call(adapter, "another-investigator")
    assert len(requests) == 1
    assert set(ledger(adapter)["stages"]) == {"investigator-01"}


async def test_concurrent_reserved_call_stops_known_zero_at_actual_post_boundary(tmp_path):
    second_entered = asyncio.Event()
    first_finished = asyncio.Event()
    requests = []

    class GatedTransport(httpx.MockTransport):
        entries = 0

        async def __aenter__(self):
            self.entries += 1
            if self.entries == 1:
                await second_entered.wait()
            else:
                second_entered.set()
                await first_finished.wait()
            return await super().__aenter__()

    def transport(request):
        requests.append(request)
        return response(usage={})

    adapter = adapter_for(tmp_path, GatedTransport(transport))
    prior = old_holds(adapter)

    async def first():
        try:
            return await call(adapter, "first-unknown")
        finally:
            first_finished.set()

    results = await asyncio.wait_for(asyncio.gather(
        first(), call(adapter, "reserved-not-posted"), return_exceptions=True,
    ), timeout=5)
    assert all(isinstance(item, inv.PaidCallUncertain) for item in results)
    assert len(requests) == 1
    saved = ledger(adapter)["stages"]
    assert {key: saved[key] for key in prior} == prior
    assert saved["first-unknown"]["actual_usd"] is None
    assert saved["reserved-not-posted"]["status"] == "failed"
    assert saved["reserved-not-posted"]["actual_usd"] == 0
    assert not (adapter.directory / "reserved-not-posted-wire-response.json").exists()


async def test_already_dispatched_concurrent_response_is_accounted_and_saved(tmp_path):
    both_dispatched = asyncio.Event()
    first_finished = asyncio.Event()
    requests = []

    async def transport(request):
        requests.append(request)
        stage = json.loads(request.content)["messages"][0]["content"]
        if stage == "first-unknown":
            await both_dispatched.wait()
            return response(usage={})
        both_dispatched.set()
        await first_finished.wait()
        return response(usage={"cost": 0.025})

    adapter = adapter_for(tmp_path, httpx.MockTransport(transport))

    async def first():
        try:
            return await call(adapter, "first-unknown")
        finally:
            first_finished.set()

    results = await asyncio.wait_for(asyncio.gather(
        first(), call(adapter, "already-dispatched"), return_exceptions=True,
    ), timeout=5)
    assert all(isinstance(item, inv.PaidCallUncertain) for item in results)
    assert len(requests) == 2
    saved = ledger(adapter)["stages"]
    assert saved["first-unknown"]["actual_usd"] is None
    assert saved["already-dispatched"]["status"] == "complete"
    assert saved["already-dispatched"]["actual_usd"] == 0.025
    assert (adapter.directory / "already-dispatched-response.json").exists()
    assert (adapter.directory / "already-dispatched-wire-response.json").exists()
    with pytest.raises(inv.PaidCallUncertain):
        await call(adapter, "third-must-not-dispatch")
    assert len(requests) == 2


@pytest.mark.parametrize("failure", ["timeout", "http-error", "malformed-json", "null", "list", "string"])
async def test_transport_or_response_uncertainty_latches_original_hold(tmp_path, failure):
    requests = []

    def transport(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic timeout")
        if failure == "http-error":
            return httpx.Response(503, content=b"synthetic rejection")
        if failure in {"null", "list", "string"}:
            return httpx.Response(200, content={"null": b"null", "list": b"[]", "string": b'"text"'}[failure])
        return httpx.Response(200, content=b"not valid json")

    adapter = adapter_for(tmp_path, httpx.MockTransport(transport))
    with pytest.raises((httpx.HTTPError, ValueError, inv.PaidCallUncertain)):
        await call(adapter, "uncertain-transport")
    saved = ledger(adapter)
    assert saved["stages"]["uncertain-transport"]["status"] == "unknown"
    assert saved["stages"]["uncertain-transport"]["actual_usd"] is None
    if failure != "timeout":
        wire = json.loads((adapter.directory / "uncertain-transport-wire-response.json").read_text())
        expected = {"http-error": b"synthetic rejection", "malformed-json": b"not valid json",
                    "null": b"null", "list": b"[]", "string": b'"text"'}[failure]
        assert base64.b64decode(wire["body_base64"]) == expected
    with pytest.raises(inv.PaidCallUncertain):
        await call(adapter, "after-transport-must-not-dispatch")
    assert len(requests) == 1 and ledger(adapter) == saved


@pytest.mark.parametrize("cost", [0, 0.025])
async def test_known_finite_costs_continue_and_do_not_reject_old_unknowns(tmp_path, cost):
    requests = []

    def transport(request):
        requests.append(request)
        return response(usage={"cost": cost})

    adapter = adapter_for(tmp_path, httpx.MockTransport(transport))
    prior = old_holds(adapter)
    for stage in ("known-first", "known-second"):
        assert (await call(adapter, stage))["model"] == MODEL
    saved = ledger(adapter)["stages"]
    assert {key: saved[key] for key in prior} == prior
    assert saved["known-first"]["actual_usd"] == cost
    assert saved["known-second"]["actual_usd"] == cost
    assert len(requests) == 2

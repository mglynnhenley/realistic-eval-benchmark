"""Original provider bills only, with complete frozen charge coverage."""

import json

import pytest

from refund_eval.realism import coverage, scan_cost


@pytest.fixture
def rows(monkeypatch):
    records = [{"_parts_protocol": "frozen-parts-v1", "scan_events": json.dumps([
        {"event": "span_begin"},
        {"event": "model", "call": {"response": {"id": "generation-a", "usage": {"cost": .1}}}},
        {"event": "model", "call": {"response": {"id": "generation-b", "usage": {"cost": .2}}}},
    ])}]
    monkeypatch.setattr(coverage, "scan_rows", lambda *args: records)
    monkeypatch.setattr(coverage, "validated_samples", lambda *args, **kwargs: [(1, 2, [])])
    return records


def read():
    return scan_cost.saved_cost(["synthetic.eval"], "scans", judge="openrouter/example/model", expected_trials=3)


def test_sums_provider_charges_without_float_rounding(rows):
    result = read()
    assert result["cost_usd"] == .3 and result["cost_usd_decimal"] == "0.3"
    assert [row["generation_id"] for row in result["charges"]] == ["generation-a", "generation-b"]


@pytest.mark.parametrize("damage", ["missing", "duplicate", "negative", "bool", "nan", "legacy", "empty"])
def test_uncertain_or_ambiguous_charges_remain_unknown(rows, damage):
    events = json.loads(rows[0]["scan_events"])
    response = events[-1]["call"]["response"]
    if damage == "missing":
        response["usage"].pop("cost")
    elif damage == "duplicate":
        response["id"] = "generation-a"
    elif damage in {"negative", "bool", "nan"}:
        response["usage"]["cost"] = {"negative": -1, "bool": True, "nan": float("nan")}[damage]
    elif damage == "legacy":
        rows[0].pop("_parts_protocol")
    else:
        events = []
    rows[0]["scan_events"] = json.dumps(events)
    assert read()["cost_usd"] is None


def test_invalid_coverage_never_becomes_a_known_bill(rows, monkeypatch):
    def reject(*args, **kwargs):
        raise ValueError("Missing frozen part")
    monkeypatch.setattr(coverage, "validated_samples", reject)
    assert read() == {"cost_usd": None, "reason": "Missing frozen part"}


def test_explicit_zero_provider_charge_is_preserved(rows):
    events = json.loads(rows[0]["scan_events"])
    for event in events[1:]:
        event["call"]["response"]["usage"]["cost"] = 0
    rows[0]["scan_events"] = json.dumps(events)
    assert read()["cost_usd"] == 0

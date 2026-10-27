"""Realism checks rerun one saved interaction with notes and judge both texts (fake models only)."""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from refund_eval.companies import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanyProfile,
)
from refund_eval.runtime.records import InteractionAttempt, InteractionRecord

if importlib.util.find_spec("refund_eval.realism.notes") is None:  # agent 2's module may not exist yet
    stub = ModuleType("refund_eval.realism.notes")
    stub.notes_text = lambda scenario_id, org_key, factor: ""
    sys.modules["refund_eval.realism.notes"] = stub

from refund_eval.realism import check

NOW = datetime(2026, 10, 1, tzinfo=UTC)
BEFORE = CompanyAction(decision="ack", reply=True, reply_after_hours=1, subject="Request status",
                       body="Your request has been received.")
AFTER = CompanyAction(decision="ack", reply=True, reply_after_hours=30, subject="Your refund request ref 4821",
                      body="Dear Customer, thank you for contacting Example Air.")


def reply(data):
    return SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(completion=json.dumps(data))))


@pytest.fixture
def log(tmp_path, monkeypatch):
    record = InteractionRecord(
        id="contact-1", time=NOW, org="airline", event=CompanyEvent(incoming="Please refund me."),
        context=CompanyContext(org=CompanyProfile(key="airline", name="Example Air", kind="airline"),
                               subject="A refund", customer_name="Customer",
                               customer_email="customer@example.test", now=NOW),
        simulator_kind="llm", attempts=[InteractionAttempt(number=1, event=CompanyEvent(incoming="Please refund me."),
                                                           action=BEFORE, finalized_action=BEFORE)],
        action=BEFORE, status="applied")
    path = tmp_path / "run.eval"
    path.write_bytes(b"log")
    monkeypatch.setattr("refund_eval.companies.rerun.read_eval_log", lambda _: SimpleNamespace(samples=[
        SimpleNamespace(id=1, epoch=1, store={"world": {"interactions": [record.model_dump(mode="json")]}})]))
    return str(path)


def fake_models(monkeypatch, simulator, judge):
    models = {"mock/company": simulator, "mock/judge": judge}
    monkeypatch.setattr(check, "get_model", lambda name=None, **kw: models[name or "mock/judge"])


async def test_resolved_when_rerun_with_notes_no_longer_looks_fake(log, monkeypatch):
    simulator = reply(AFTER.model_dump(mode="json"))
    judge = reply({"before_fake": True, "after_fake": False, "explanation": "Bare template before."})
    fake_models(monkeypatch, simulator, judge)
    result = await check.check_interaction(log, "contact-1", "train_refund_late", notes="- Real refs look like 4821",
                                           model="mock/company", judge="mock/judge")
    prompt = simulator.generate.call_args.args[0][0].content
    assert "Real refs look like 4821" in prompt
    assert result["status"] == "resolved" and result["org"] == "airline"
    assert "Request status" in result["before"] and "ref 4821" in result["after"]
    shared = judge.generate.call_args.args[0][1].content[0].text
    assert "Example Air" in shared and "Request status" in shared


async def test_default_notes_come_from_memory_and_open_when_still_fake(log, monkeypatch):
    calls = []
    monkeypatch.setattr(check, "notes_text", lambda *args: calls.append(args) or "- From memory")
    simulator = reply(AFTER.model_dump(mode="json"))
    fake_models(monkeypatch, simulator, reply({"before_fake": True, "after_fake": True, "explanation": "Still."}))
    result = await check.check_interaction(log, "contact-1", "train_refund_late", model="mock/company")
    assert calls == [("train_refund_late", "airline", "company")]
    assert "- From memory" in simulator.generate.call_args.args[0][0].content
    assert result["status"] == "open" and result["after_fake"]


async def test_rerun_error_fails_without_judging(log, monkeypatch):
    simulator = SimpleNamespace(generate=AsyncMock(side_effect=RuntimeError("provider down")))
    judge = reply({})
    fake_models(monkeypatch, simulator, judge)
    result = await check.check_interaction(log, "contact-1", "s", notes="", model="mock/company",
                                           judge="mock/judge")
    assert result["status"] == "failed" and "provider down" in result["explanation"]
    assert judge.generate.await_count == 0

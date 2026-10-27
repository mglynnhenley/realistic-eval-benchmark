"""A saved Inspect log can drive a company comparison outside an active trial."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from inspect_ai.log import (
    EvalConfig,
    EvalDataset,
    EvalLog,
    EvalSample,
    EvalSpec,
    write_eval_log,
)
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.companies import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanyProfile,
)
from refund_eval.companies import __main__ as cli
from refund_eval.runtime.records import InteractionAttempt, InteractionRecord


@pytest.mark.parametrize("log_format", ["eval", "json"])
def test_real_log_and_model_round_trip_without_a_live_trial(tmp_path, monkeypatch, capsys, log_format):
    now = datetime(2026, 10, 1, tzinfo=UTC)
    event = CompanyEvent(incoming="Please refund my purchase.")
    retry = event.model_copy(update={"feedback": ["Your payment exceeds the accounting limit."]})
    original = CompanyAction(decision="approved", reply=True, reply_after_hours=1, body="Refund approved.",
                             payments=[{"kind": "cash_refund", "amount": 20, "via": "card"}])
    final = original.model_copy(update={"reply_after_hours": 12})
    context = CompanyContext(
        org=CompanyProfile(key="retailer", name="Retailer", kind="retailer", terms="Receipt required."),
        subject="A purchase refund", customer_name="Customer", customer_email="customer@example.test",
        now=now, records="Receipt for the selected epoch.", payment_limit=20,
    )
    record = InteractionRecord(
        id="contact-1", time=now, org="retailer", event=event, context=context,
        simulator_kind="llm", simulator_version="saved-company-v1", runtime_revision="company_events_v2",
        attempts=[InteractionAttempt(number=2, event=retry, action=original, finalized_action=final)],
        action=final, status="applied",
    )
    samples = []
    for sample_id, epoch in [(1, 1), (1, 2), ("other", 1)]:
        saved_record = record.model_copy(deep=True)
        if (sample_id, epoch) != (1, 2):
            saved_record.context.records = "A different sample or epoch."
        samples.append(EvalSample(id=sample_id, epoch=epoch, input="Fixture task", target="", store={
            "world": {"interactions": [saved_record.model_dump(mode="json")], "private_fact": "PRIVATE FACT"},
            "claim_findings": "PRIVATE ASSESSMENT",
        }))
    log = EvalLog(status="success", eval=EvalSpec(
        created=now.isoformat(), task="company-rerun-fixture", model="mockllm/target",
        dataset=EvalDataset(samples=2), config=EvalConfig(epochs=2)), samples=samples)
    source = tmp_path / f"trial.{log_format}"
    write_eval_log(log, source, format=log_format)
    before = source.read_bytes()

    monkeypatch.setattr("sys.argv", ["company", "list", "--log", str(source)])
    cli.main()
    listing = json.loads(capsys.readouterr().out)
    assert {(row["sample_id"], row["epoch"]) for row in listing} == {(1, 1), (1, 2), ("other", 1)}

    fresh = original.model_copy(update={"decision": "evidence_required", "payments": [],
                                        "body": "Please supply the receipt.",
                                        "decision_basis": "A receipt is required by our terms."})
    calls = []

    def respond(input, tools, tool_choice, config):
        calls.append(input)
        assert config.response_schema.name == "open_world_action"
        return ModelOutput.from_content("mockllm/company-comparison", fresh.model_dump_json())

    model = get_model("mockllm/company-comparison", custom_outputs=respond, memoize=False)
    monkeypatch.setattr(cli, "get_model", lambda name: model)
    monkeypatch.setattr("refund_eval.companies.llm.get_model", lambda **kwargs: pytest.fail("No trial role lookup"))
    init_subtask_store(Store({"world": {"sentinel": "unchanged"}}))
    destination = tmp_path / "comparison.json"
    monkeypatch.setattr("sys.argv", ["company", "rerun", "--log", str(source),
        "--interaction", "contact-1", "--sample-id", "1", "--epoch", "2",
        "--model", "mockllm/company-comparison", "--output", str(destination)])
    cli.main()

    report = json.loads(destination.read_text())
    assert report["status"] == "succeeded"
    assert (report["sample_id"], report["epoch"], report["attempt"]) == (1, 2, 2)
    assert report["source_sha256"] == hashlib.sha256(before).hexdigest()
    assert report["original_simulator_version"] == "saved-company-v1"
    assert report["original_proposed_action"]["reply_after_hours"] == 1
    assert report["original_finalized_action"]["reply_after_hours"] == 12
    assert report["new_proposed_action"]["payments"] == []
    assert "reply_after_hours" not in {change["field"] for change in report["changes"]}
    assert len(calls) == 1
    prompt = "\n".join(message.text for message in calls[0])
    assert "Receipt for the selected epoch." in prompt
    assert retry.feedback[0] in prompt
    assert "A different sample or epoch." not in prompt
    assert "PRIVATE" not in prompt
    assert source.read_bytes() == before
    assert store().get("world") == {"sentinel": "unchanged"}

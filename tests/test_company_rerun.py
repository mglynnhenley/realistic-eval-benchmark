"""Company comparisons reuse frozen inputs without running or mutating the trial."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.companies import (
    CompanyAction,
    CompanyContext,
    CompanyEvent,
    CompanyProfile,
)
from refund_eval.companies import __main__ as cli
from refund_eval.companies.llm import LLMCompanySimulator
from refund_eval.companies.rerun import (
    load_interactions,
    rerun_decision,
    select_attempt,
    select_interaction,
)
from refund_eval.runtime.records import InteractionAttempt, InteractionRecord

NOW = datetime(2026, 10, 1, tzinfo=UTC)


@pytest.fixture
def interaction():
    initial = CompanyEvent(incoming="Please refund my purchase.")
    retry = initial.model_copy(update={"feedback": ["Payment exceeds the company's accounting limit."]})
    proposal = CompanyAction(decision="approved", reply=True, reply_after_hours=1, body="Refund approved.",
                             decision_basis="The supplied receipt satisfies our terms.",
                             payments=[{"kind": "cash_refund", "amount": 20, "via": "card"}])
    finalized = proposal.model_copy(update={"reply_after_hours": 12})
    excessive = proposal.model_dump()
    excessive["payments"][0]["amount"] = 200
    return InteractionRecord(
        id="contact-1", time=NOW, org="company", event=initial,
        context=CompanyContext(
            org=CompanyProfile(key="company", name="Company", kind="retailer", terms="Receipt required."),
            subject="A refund", customer_name="Customer", customer_email="customer@example.test", now=NOW,
            records="A £20 purchase is recorded.", history=["Customer supplied a receipt."], payment_limit=20,
        ),
        simulator_kind="llm", simulator_version="original-v1", runtime_revision="company_events_v2",
        attempts=[
            InteractionAttempt(number=1, event=initial, action=CompanyAction.model_validate(excessive),
                validation_error=retry.feedback[0]),
            InteractionAttempt(number=2, event=retry, action=proposal, finalized_action=finalized),
        ], action=finalized, status="applied",
    )


@pytest.fixture
def snapshot(tmp_path, interaction):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps({
        "checkpoint_version": 1,
        "world": {"interactions": [interaction.model_dump(mode="json")], "future_private_fact": "hidden"},
        "resume_state": {"claim_findings": "not for companies"},
    }))
    return path


def company(action, *, failure=None):
    return SimpleNamespace(kind="test", version="new-v2", respond=AsyncMock(return_value=action, side_effect=failure))


async def test_rerun_compares_raw_proposals_and_exact_retry_inputs_without_live_state(snapshot, interaction):
    saved = load_interactions(snapshot, source_format="snapshot")
    entry = select_interaction(saved, "contact-1")
    simulator = company(interaction.attempts[-1].action)
    original_bytes = snapshot.read_bytes()
    init_subtask_store(Store({"world": {"sentinel": "unchanged"}, "private_checker": "hidden"}))

    report = await rerun_decision(saved, entry, simulator=simulator)

    event, context = simulator.respond.call_args.args
    assert event == interaction.attempts[-1].event and event.feedback
    assert context == interaction.context
    assert "private_checker" not in context.model_dump_json()
    assert "future_private_fact" not in context.model_dump_json()
    assert "claim_findings" not in context.model_dump_json()
    assert report.attempt == 2 and report.status == "succeeded"
    assert report.changes == []
    assert report.original_proposed_action.reply_after_hours == 1
    assert report.original_finalized_action.reply_after_hours == 12
    assert report.new_proposed_action.payments[0].amount == 20
    assert report.original_simulator_version == "original-v1"
    assert report.simulator_version == "new-v2"
    assert store().get("world") == {"sentinel": "unchanged"}
    assert snapshot.read_bytes() == original_bytes


async def test_attempt_selection_and_field_changes(snapshot, interaction):
    saved = load_interactions(snapshot, source_format="snapshot")
    entry = saved.interactions[0]
    fresh = interaction.attempts[-1].action.model_copy(update={"decision": "evidence_required", "payments": []})
    simulator = company(fresh)
    report = await rerun_decision(saved, entry, simulator=simulator, attempt=1)
    assert simulator.respond.call_args.args[0].feedback == []
    assert report.original_validation_error == interaction.attempts[0].validation_error
    changes = {change.field: change for change in report.changes}
    assert changes["decision"].original == "approved" and changes["decision"].rerun == "evidence_required"
    assert changes["payments"].original[0]["amount"] == 200 and changes["payments"].rerun == []
    with pytest.raises(ValueError, match="available: 1, 2"):
        select_attempt(entry, 3)


async def test_mutating_simulator_cannot_change_frozen_evidence(snapshot, interaction):
    saved = load_interactions(snapshot, source_format="snapshot")
    before = saved.interactions[0].model_dump_json()

    async def respond(event, context):
        event.feedback.clear()
        context.org.terms = "Changed"
        return interaction.attempts[-1].action

    report = await rerun_decision(saved, saved.interactions[0],
                                 simulator=SimpleNamespace(kind="mutating", version="1", respond=respond))
    assert saved.interactions[0].model_dump_json() == before
    assert report.event.feedback
    assert report.context.org.terms == "Receipt required."


async def test_failed_fresh_proposal_is_recorded_once(snapshot):
    saved = load_interactions(snapshot, source_format="snapshot")
    simulator = company(None, failure=RuntimeError("provider unavailable"))
    report = await rerun_decision(saved, saved.interactions[0], simulator=simulator)
    assert report.status == "failed" and report.new_proposed_action is None
    assert report.error == "RuntimeError: provider unavailable"
    assert report.original_proposed_action is not None
    assert simulator.respond.await_count == 1


async def test_original_failed_attempt_can_be_compared_without_fabricated_action(snapshot, interaction):
    saved = load_interactions(snapshot, source_format="snapshot")
    entry = saved.interactions[0]
    entry.interaction.attempts[-1].action = None
    entry.interaction.attempts[-1].finalized_action = None
    entry.interaction.attempts[-1].error = "Invalid JSON"
    report = await rerun_decision(saved, entry, simulator=company(interaction.action))
    assert report.status == "succeeded"
    assert report.original_proposed_action is None and report.changes == []
    assert report.original_error == "Invalid JSON"


def test_log_selection_requires_unambiguous_sample_and_epoch(snapshot, interaction, monkeypatch):
    log = SimpleNamespace(samples=[SimpleNamespace(
        id=sample_id, epoch=epoch, store={"world": {"interactions": [interaction.model_dump(mode="json")]}}
    ) for sample_id, epoch in [(1, 1), (1, 2), ("another-sample", 1)]])
    monkeypatch.setattr("refund_eval.companies.rerun.read_eval_log", lambda path: log)
    saved = load_interactions(snapshot, source_format="log")
    assert len(saved.interactions) == 3
    with pytest.raises(ValueError, match="ambiguous"):
        select_interaction(saved, "contact-1")
    with pytest.raises(ValueError, match="ambiguous"):
        select_interaction(saved, "contact-1", sample_id="1")
    assert select_interaction(saved, "contact-1", sample_id="1", epoch=2).epoch == 2
    assert select_interaction(saved, "contact-1", sample_id="another-sample").sample_id == "another-sample"
    with pytest.raises(ValueError, match="No matching"):
        select_interaction(saved, "missing")


async def test_explicit_model_injection_works_outside_inspect_run(interaction, monkeypatch):
    model = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(
        completion=interaction.attempts[-1].action.model_dump_json())))
    monkeypatch.setattr("refund_eval.companies.llm.get_model", lambda **kw: pytest.fail("No live model role"))
    action = await LLMCompanySimulator(model=model).respond(interaction.event, interaction.context)
    assert action == interaction.attempts[-1].action
    assert model.generate.await_count == 1


def run_cli(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["company", *map(str, args)])
    cli.main()


def test_list_shows_selectors_without_constructing_models(snapshot, monkeypatch, capsys):
    monkeypatch.setattr(cli, "get_model", lambda *_a: pytest.fail("Listing is read-only"))
    run_cli(monkeypatch, "list", "--snapshot", snapshot)
    listed = json.loads(capsys.readouterr().out)
    assert listed == [{"sample_id": "snapshot", "epoch": 1, "interaction": "contact-1", "company": "company",
                       "event": "contact", "status": "applied", "simulator": "llm", "attempts": [1, 2],
                       "inputs_available": True}]


def test_cli_saves_separate_report_and_source_stays_unchanged(snapshot, interaction, monkeypatch, capsys):
    output = snapshot.parent / "comparison.json"
    before = snapshot.read_bytes()
    model = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(
        completion=interaction.attempts[-1].action.model_dump_json())))
    monkeypatch.setattr(cli, "get_model", lambda name: model)
    run_cli(monkeypatch, "rerun", "--snapshot", snapshot, "--interaction", "contact-1",
            "--output", output, "--model", "mock/company")
    report = json.loads(output.read_text())
    assert report["version"] == 1 and report["model"] == "mock/company"
    assert report["status"] == "succeeded" and report["changes"] == []
    assert report["source"] == str(snapshot)
    assert len(report["source_sha256"]) == 64
    assert snapshot.read_bytes() == before
    assert "proposed actions match" in capsys.readouterr().out


def test_cli_can_choose_registered_alternative(snapshot, interaction, monkeypatch):
    alternative = company(interaction.attempts[-1].action)
    monkeypatch.setattr("refund_eval.companies._SIMULATORS", {"test": alternative})
    monkeypatch.setattr(cli, "get_model", lambda *_a: pytest.fail("Alternative needs no model"))
    output = snapshot.parent / "alternative.json"
    run_cli(monkeypatch, "rerun", "--snapshot", snapshot, "--interaction", "contact-1",
            "--output", output, "--simulator", "test")
    assert json.loads(output.read_text())["simulator_kind"] == "test"
    assert alternative.respond.await_count == 1


def test_cli_failure_saves_report_and_returns_unsuccessful_status(snapshot, monkeypatch):
    output = snapshot.parent / "failure.json"
    model = SimpleNamespace(generate=AsyncMock(side_effect=RuntimeError("provider offline")))
    monkeypatch.setattr(cli, "get_model", lambda name: model)
    with pytest.raises(SystemExit) as stopped:
        run_cli(monkeypatch, "rerun", "--snapshot", snapshot, "--interaction", "contact-1",
                "--output", output, "--model", "mock/company")
    assert stopped.value.code == 1
    assert json.loads(output.read_text())["error"] == "RuntimeError: provider offline"


@pytest.mark.parametrize("missing", ["context", "attempts", "interactions"])
def test_missing_frozen_evidence_never_calls_model(snapshot, monkeypatch, missing, capsys):
    data = json.loads(snapshot.read_text())
    record = data["world"]["interactions"][0]
    if missing == "context":
        record["context"] = None
    elif missing == "attempts":
        record["attempts"] = []
    else:
        del data["world"]["interactions"]
        data["world"]["cases"] = {"company": {"decisions": [{"decision": "old_approval"}]}}
    snapshot.write_text(json.dumps(data))
    monkeypatch.setattr(cli, "get_model", lambda *_a: pytest.fail("Unavailable inputs must not call a model"))
    with pytest.raises(SystemExit) as stopped:
        run_cli(monkeypatch, "rerun", "--snapshot", snapshot, "--interaction", "contact-1",
                "--output", snapshot.parent / "unused.json", "--model", "mock/company")
    assert stopped.value.code == 2
    assert not (snapshot.parent / "unused.json").exists()
    if missing != "interactions":
        assert "reconstructed" in capsys.readouterr().err


def test_legacy_log_lists_no_interactions(snapshot, monkeypatch, capsys):
    snapshot.write_text('{"world": {"cases": {"company": {"decisions": []}}}}')
    run_cli(monkeypatch, "list", "--snapshot", snapshot)
    assert "Historical inputs cannot be reconstructed" in capsys.readouterr().out


@pytest.mark.parametrize("destination", ["source", "symlink", "hardlink", "repo"])
def test_output_guard_precedes_model_calls(snapshot, monkeypatch, destination):
    monkeypatch.setattr(cli, "get_model", lambda *_a: pytest.fail("Output must be checked before model creation"))
    if destination == "source":
        output = snapshot
    elif destination == "symlink":
        output = snapshot.parent / "alias.json"
        output.symlink_to(snapshot)
    elif destination == "hardlink":
        output = snapshot.parent / "alias.json"
        output.hardlink_to(snapshot)
    else:
        output = Path(__file__).parents[1] / "private-comparison.json"
    before = snapshot.read_bytes()
    with pytest.raises(SystemExit) as stopped:
        run_cli(monkeypatch, "rerun", "--snapshot", snapshot, "--interaction", "contact-1",
                "--output", output, "--model", "mock/company")
    assert stopped.value.code == 2
    assert snapshot.read_bytes() == before


def test_unknown_checkpoint_version_rejected(snapshot):
    snapshot.write_text('{"checkpoint_version": 99, "world": {"interactions": []}}')
    with pytest.raises(ValueError, match="Unsupported checkpoint version"):
        load_interactions(snapshot, source_format="snapshot")


"""Offline coverage for immutable shared company records and first contact."""

import asyncio
import hashlib
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.model import ModelOutput
from inspect_ai.util import store

from refund_eval.companies import exemplars
from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.sim import company_store, research, rules
from refund_eval.sim.counterparts import REGISTRY_KEY, create_counterpart
from refund_eval.sim.interface import Inbound
from refund_eval.sim.open_world import (
    COMPANIES_KEY,
    _apply_example_reply_from,
    _sender,
    company_record,
)
from refund_eval.world.state import load_world

SCENARIO = load_scenario("train_refund_late")
PAGE_URL = "https://example.test/policy"
ORG = Org(
    key="dynamic_example_test", name="Example Advice", kind="dynamic_advice",
    email_domains=["example.test"], web_domains=["www.example.test"],
    terms="Refund requests must be made within 28 days.",
)


def _dossier(org: Org = ORG) -> dict:
    text = "Requests must be made within 28 days."
    return {
        "org": org.key, "name": org.name, "mode": "live", "retrieved_at": "2026-11-20T00:00:00+00:00",
        "dossier": {"summary": "An advice company.", "facts": [{
            "statement": "The request window is 28 days.", "quote": text, "url": PAGE_URL,
        }], "persona": {"voice": "Professional."}},
        "pages": [{
            "url": PAGE_URL, "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "retrieved_at": "2026-11-20T00:00:00+00:00",
        }],
        "tool_calls": [], "errors": [],
    }


def _set_live(sim: MockSim, scenario=SCENARIO) -> None:
    install_mock_models(sim)
    start_sample(scenario.id)
    store().set("published_tool_profile", {"data_mode": "hybrid"})
    store().set(research.MODE_KEY, "live")


def _seed_dossier(org: Org = ORG) -> None:
    store().set(research.DOSSIERS_KEY, {org.key: _dossier(org)})


def _company_entry(scenario, org, fingerprint, **extra):
    return {
        "schema": 1, "fingerprint": fingerprint, "identity": company_store.org_identity(org),
        "scenario": scenario.id, "created_at": datetime.now(UTC).isoformat(),
        "org": org.model_dump(mode="json"), "rules": {"rules": {"grounds": []}},
        "examples": [], **extra,
    }


def _sync_entry(scenario=SCENARIO, org=ORG, *, model="fixture/simulator", summary="Public company details"):
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    text = "The company publishes its contact details."
    page = {"url": PAGE_URL, "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "retrieved_at": now}
    page_refs = [{key: page[key] for key in ("url", "sha256", "retrieved_at")}]
    pages_digest = hashlib.sha256(json.dumps(
        [{"url": page["url"], "sha256": page["sha256"]}], sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    provenance = {
        "identity": company_store.org_identity(org), "scenario_id": scenario.id,
        "intent_sha256": hashlib.sha256((scenario.intent or "").encode()).hexdigest(),
        "subject_sha256": hashlib.sha256((scenario.subject or "").encode()).hexdigest(),
        "terms_sha256": hashlib.sha256((org.terms or "").encode()).hexdigest(),
        "model": model, "temperature": 0, "rules_prompt_version": "1", "research_prompt_version": "2",
        "exemplar_prompt_version": "2", "checker_version": "1", "pages": page_refs,
        "pages_sha256": pages_digest,
    }
    inputs = {
        "identity": provenance["identity"], "scenario": scenario.id,
        "intent_sha256": provenance["intent_sha256"], "subject_sha256": provenance["subject_sha256"],
        "terms_sha256": provenance["terms_sha256"], "model": model,
        "rules_prompt_version": "1", "research_prompt_version": "2",
        "exemplar_prompt_version": "2", "checker_version": "1",
    }
    fingerprint = hashlib.sha256(json.dumps(
        inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()[:16]
    return {
        "schema": 1, "fingerprint": fingerprint, "identity": provenance["identity"], "scenario": scenario.id,
        "created_at": now, "provenance": provenance, "org": org.model_dump(mode="json"),
        "research": {
            "org": org.key, "name": org.name, "mode": "live", "retrieved_at": now,
            "dossier": {"summary": summary, "facts": [], "persona": {"voice": "Professional."}},
            "pages": [page],
        },
        "rules": {"rules": {"terms": "The published company terms.", "grounds": []},
                  "model": model, "generated_at": now},
        "temperament": "Professional.", "examples": [],
    }


def test_company_store_sync_sanitizes_and_keeps_first_save(tmp_path):
    source, host = tmp_path / "source", tmp_path / "host"
    entry = _sync_entry()
    entry["private_extra"] = "discarded by the public-field allowlist"
    company_store.save_entry(SCENARIO, ORG, entry, root=source)
    assert "private_extra" not in json.loads(company_store._entry_path(
        SCENARIO.id, company_store.org_identity(ORG), entry["fingerprint"], source).read_text())
    assert company_store.sync_scenario_entries(SCENARIO, source_root=source, destination_root=host) == 1
    saved_path = company_store._entry_path(
        SCENARIO.id, company_store.org_identity(ORG), entry["fingerprint"], host)
    first_bytes = saved_path.read_bytes()
    assert company_store.sync_scenario_entries(SCENARIO, source_root=source, destination_root=host) == 0
    assert saved_path.read_bytes() == first_bytes

    artifact = tmp_path / "artifact"
    later = _sync_entry(summary="Later research must not replace the first save.")
    company_store.save_entry(SCENARIO, ORG, later, root=artifact)
    assert company_store.sync_scenario_entries(SCENARIO, source_root=artifact, destination_root=host) == 0
    assert saved_path.read_bytes() == first_bytes


def test_company_store_sync_preserves_historical_scenario_fingerprints(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    entry = _sync_entry()
    company_store.save_entry(SCENARIO, ORG, entry, root=source)
    changed_scenario = SCENARIO.model_copy(update={"intent": "Updated intent.", "subject": "Updated subject."})

    assert changed_scenario.id == SCENARIO.id
    assert company_store.sync_scenario_entries(
        changed_scenario, source_root=source, destination_root=destination,
    ) == 1
    saved = json.loads(company_store._entry_path(
        SCENARIO.id, company_store.org_identity(ORG), entry["fingerprint"], destination,
    ).read_text())
    assert saved["fingerprint"] == entry["fingerprint"]
    assert saved["provenance"]["intent_sha256"] == entry["provenance"]["intent_sha256"]


def test_company_store_sync_rejects_private_malformed_and_mismatched_entries(tmp_path):
    destination = tmp_path / "destination"
    entry = _sync_entry(summary=SCENARIO.facts.journey.booking_ref)
    private_source = tmp_path / "private-source"
    company_store.save_entry(SCENARIO, ORG, entry, root=private_source)
    with pytest.raises(ValueError, match="scenario data"):
        company_store.sync_scenario_entries(SCENARIO, source_root=private_source, destination_root=destination)

    entry = _sync_entry()
    entry["provenance"]["scenario_id"] = "another_scenario"
    mismatch_source = tmp_path / "mismatch-source"
    company_store.save_entry(SCENARIO, ORG, entry, root=mismatch_source)
    with pytest.raises(ValueError, match="identity mismatch"):
        company_store.sync_scenario_entries(SCENARIO, source_root=mismatch_source, destination_root=destination)

    entry = _sync_entry()
    malformed_source = tmp_path / "malformed-source"
    company_store.save_entry(SCENARIO, ORG, entry, root=malformed_source)
    path = company_store._entry_path(
        SCENARIO.id, company_store.org_identity(ORG), entry["fingerprint"], malformed_source)
    path.write_text("not-json")
    with pytest.raises(ValueError, match="Malformed company-store JSON"):
        company_store.sync_scenario_entries(SCENARIO, source_root=malformed_source, destination_root=destination)


def test_company_store_sync_rejects_symlink_entries(tmp_path):
    source = tmp_path / "source"
    entry = _sync_entry()
    path = company_store._entry_path(
        SCENARIO.id, company_store.org_identity(ORG), entry["fingerprint"], source)
    path.parent.mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(entry))
    path.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        company_store.sync_scenario_entries(SCENARIO, source_root=source, destination_root=tmp_path / "host")


def test_company_store_sync_ignores_disappearing_temporary_files(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    entry = _sync_entry()
    company_store.save_entry(SCENARIO, ORG, entry, root=source)
    path = company_store._entry_path(
        SCENARIO.id, company_store.org_identity(ORG), entry["fingerprint"], source,
    )
    temporary = path.parent / "tmp-save-in-progress"
    temporary.write_text("incomplete", encoding="utf-8")
    is_symlink = Path.is_symlink

    def remove_during_scan(candidate):
        result = is_symlink(candidate)
        if candidate == temporary:
            temporary.unlink(missing_ok=True)
        return result

    monkeypatch.setattr(Path, "is_symlink", remove_during_scan)

    assert company_store.sync_scenario_entries(SCENARIO, source_root=source, destination_root=destination) == 1
    assert not temporary.exists()


def test_company_store_root_default_custom_and_off(tmp_path, monkeypatch):
    default = tmp_path / "default"
    monkeypatch.delenv("REFUND_EVAL_COMPANY_STORE", raising=False)
    monkeypatch.setattr(company_store, "DEFAULT_ROOT", default)
    assert company_store.store_root() == default.resolve()
    custom = tmp_path / "custom"
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", str(custom))
    assert company_store.store_root() == custom.resolve()
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", "off")
    assert company_store.store_root() is None


def test_native_jobs_registry_is_private_atomic_and_ignores_disappearing_temporary_files(tmp_path, monkeypatch):
    host_store = tmp_path / "host-store"
    jobs_root = tmp_path / "prepared" / "jobs"
    with ThreadPoolExecutor(max_workers=4) as pool:
        registrations = list(pool.map(
            lambda _: company_store.register_native_jobs_root(jobs_root, root=host_store), range(8),
        ))

    registry = host_store / ".native-jobs"
    files = list(registry.glob("*.json"))
    assert len(set(registrations)) == 1
    assert len(files) == 1
    assert files[0].name == f"{hashlib.sha256(str(jobs_root.resolve()).encode()).hexdigest()}.json"
    assert stat.S_IMODE(registry.stat().st_mode) == 0o700
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert company_store.registered_native_jobs_roots(host_store) == [jobs_root.resolve()]

    temporary = registry / "registration-in-progress.tmp"
    temporary.write_text("partial", encoding="utf-8")
    is_symlink = Path.is_symlink

    def remove_during_scan(candidate):
        result = is_symlink(candidate)
        if candidate == temporary:
            temporary.unlink(missing_ok=True)
        return result

    monkeypatch.setattr(Path, "is_symlink", remove_during_scan)
    assert company_store.registered_native_jobs_roots(host_store) == [jobs_root.resolve()]
    assert not temporary.exists()


def test_registered_harvest_is_scenario_scoped_and_ignores_unregistered_and_missing_roots(tmp_path):
    host_store = tmp_path / "host-store"
    jobs_root = tmp_path / "registered-jobs"
    deleted_jobs_root = tmp_path / "deleted-jobs"
    unregistered_jobs_root = tmp_path / "unregistered-jobs"
    company_store.register_native_jobs_root(jobs_root, root=host_store)
    company_store.register_native_jobs_root(deleted_jobs_root, root=host_store)

    artifact_store = jobs_root / "job" / "trial" / "artifacts" / "world" / "company-store"
    other_scenario = load_scenario("bills_price_rise")
    company_store.save_entry(SCENARIO, ORG, _sync_entry(), root=artifact_store)
    company_store.save_entry(other_scenario, ORG, _sync_entry(other_scenario, ORG), root=artifact_store)

    unregistered_org = Org(
        key="unregistered_company", name="Unregistered Advice", kind="dynamic_advice",
        email_domains=["unregistered.example"], web_domains=["unregistered.example"],
        terms="Published terms for the unregistered company.",
    )
    unregistered_artifact = (
        unregistered_jobs_root / "job" / "trial" / "artifacts" / "world" / "company-store"
    )
    company_store.save_entry(
        SCENARIO, unregistered_org, _sync_entry(SCENARIO, unregistered_org), root=unregistered_artifact,
    )

    assert company_store.harvest_registered_jobs(SCENARIO, destination_root=host_store) == 1
    assert company_store._entry_path(
        SCENARIO.id, company_store.org_identity(ORG), _sync_entry()["fingerprint"], host_store,
    ).is_file()
    other_entry = _sync_entry(other_scenario, ORG)
    other_path = company_store._entry_path(
        other_scenario.id, company_store.org_identity(ORG), other_entry["fingerprint"], host_store,
    )
    assert not other_path.exists()
    unregistered_entry = _sync_entry(SCENARIO, unregistered_org)
    assert not company_store._entry_path(
        SCENARIO.id, company_store.org_identity(unregistered_org), unregistered_entry["fingerprint"], host_store,
    ).exists()

    assert company_store.harvest_registered_jobs(other_scenario, destination_root=host_store) == 1
    assert other_path.is_file()


def test_harvest_jobs_accepts_outer_and_named_roots_and_rejects_symlinks(tmp_path):
    jobs_root = tmp_path / "outer-jobs"
    named_job_root = jobs_root / "native-job"
    artifact_store = named_job_root / "trial" / "artifacts" / "world" / "company-store"
    entry = _sync_entry()
    company_store.save_entry(SCENARIO, ORG, entry, root=artifact_store)

    outer_destination = tmp_path / "outer-destination"
    assert company_store.harvest_jobs(jobs_root, destination_root=outer_destination) == 1
    named_destination = tmp_path / "named-destination"
    assert company_store.harvest_jobs(named_job_root, destination_root=named_destination) == 1

    linked_root = tmp_path / "linked-jobs"
    linked_root.symlink_to(jobs_root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        company_store.harvest_jobs(linked_root, destination_root=tmp_path / "linked-destination")


def test_harvest_jobs_processes_all_scenarios_in_every_artifact(tmp_path):
    jobs_root = tmp_path / "jobs"
    scenarios = [SCENARIO, load_scenario("bills_price_rise")]
    orgs = [ORG, Org(
        key="second_company", name="Second Advice", kind="dynamic_advice",
        email_domains=["second.example"], web_domains=["second.example"],
        terms="Published terms for the second company.",
    )]
    for index, org in enumerate(orgs):
        artifact_store = jobs_root / f"job-{index}" / "trial" / "artifacts" / "world" / "company-store"
        for scenario in scenarios:
            company_store.save_entry(scenario, org, _sync_entry(scenario, org), root=artifact_store)

    destination = tmp_path / "host-store"
    assert company_store.harvest_jobs(jobs_root, destination_root=destination) == 4
    for org in orgs:
        for scenario in scenarios:
            entry = _sync_entry(scenario, org)
            assert company_store._entry_path(
                scenario.id, company_store.org_identity(org), entry["fingerprint"], destination,
            ).is_file()


def test_fingerprint_versions_and_first_save_leave_old_entry_untouched(monkeypatch):
    _set_live(MockSim())
    fingerprint = company_store.entry_fingerprint(SCENARIO, ORG)
    assert fingerprint == company_store.entry_fingerprint(SCENARIO, ORG)
    changed_subject = SCENARIO.model_copy(update={"subject": "A different task subject."})
    assert changed_subject.id == SCENARIO.id and changed_subject.intent == SCENARIO.intent
    assert company_store.entry_fingerprint(changed_subject, ORG) != fingerprint
    first = _company_entry(SCENARIO, ORG, fingerprint, temperament="first")
    assert company_store.save_entry(SCENARIO, ORG, first) == first
    path = company_store.entry_path(SCENARIO.id, company_store.org_identity(ORG), fingerprint)
    original = path.read_bytes()

    changed_inputs = [
        (SCENARIO.model_copy(update={"intent": "A different intent."}), ORG),
        (SCENARIO, ORG.model_copy(update={"terms": "Different authored terms."})),
    ]
    for scenario, org in changed_inputs:
        assert company_store.entry_fingerprint(scenario, org) != fingerprint

    from refund_eval.models import client as client_module
    from refund_eval.sim import research as research_module
    from refund_eval.sim import rules as rules_module

    original_client = client_module.client

    class DifferentModel:
        def model_name(self, _role):
            return "mockllm/other"

    monkeypatch.setattr(client_module, "client", lambda: DifferentModel())
    assert company_store.entry_fingerprint(SCENARIO, ORG) != fingerprint
    monkeypatch.setattr(client_module, "client", original_client)
    monkeypatch.setattr(rules_module, "RULES_PROMPT_VERSION", "2")
    assert company_store.entry_fingerprint(SCENARIO, ORG) != fingerprint
    monkeypatch.setattr(rules_module, "RULES_PROMPT_VERSION", "1")
    research_version = research_module.RESEARCH_PROMPT_VERSION
    monkeypatch.setattr(research_module, "RESEARCH_PROMPT_VERSION", "other")
    assert company_store.entry_fingerprint(SCENARIO, ORG) != fingerprint
    monkeypatch.setattr(research_module, "RESEARCH_PROMPT_VERSION", research_version)
    monkeypatch.setattr(research_module, "EXEMPLAR_PROMPT_VERSION", "3")
    assert company_store.entry_fingerprint(SCENARIO, ORG) != fingerprint
    monkeypatch.setattr(research_module, "EXEMPLAR_PROMPT_VERSION", "2")
    monkeypatch.setattr(rules_module, "CHECKER_VERSION", "2")
    assert company_store.entry_fingerprint(SCENARIO, ORG) != fingerprint

    assert path.read_bytes() == original
    different = company_store.entry_fingerprint(SCENARIO, ORG)
    assert company_store.save_entry(SCENARIO, ORG, _company_entry(
        SCENARIO, ORG, different, temperament="different"))["temperament"] == "different"
    assert path.read_bytes() == original


def test_shared_entry_created_before_fact_verification_is_not_reused(monkeypatch):
    _set_live(MockSim())
    cached_research = _dossier()
    cached_research["dossier"]["facts"][0]["quote"] = "No deadline applies."
    with monkeypatch.context() as legacy:
        legacy.setattr(research, "RESEARCH_PROMPT_VERSION", "2")
        fingerprint = company_store.entry_fingerprint(SCENARIO, ORG)
        company_store.save_entry(SCENARIO, ORG, _company_entry(
            SCENARIO, ORG, fingerprint, research=cached_research))
        assert company_store.load_entry(SCENARIO, ORG)["research"]["dossier"] == cached_research["dossier"]

    assert company_store.load_entry(SCENARIO, ORG) is None
    assert company_store.find_matching_entry(SCENARIO, company_store.org_identity(ORG)) is None


def test_concurrent_first_save_returns_one_immutable_payload():
    _set_live(MockSim())
    fingerprint = company_store.entry_fingerprint(SCENARIO, ORG)
    candidates = [
        _company_entry(SCENARIO, ORG, fingerprint, temperament="first"),
        _company_entry(SCENARIO, ORG, fingerprint, temperament="second"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda entry: company_store.save_entry(SCENARIO, ORG, entry), candidates))
    saved = company_store.load_entry(SCENARIO, ORG)
    assert saved["temperament"] in {"first", "second"}
    assert [result["temperament"] for result in results] == [saved["temperament"], saved["temperament"]]
    assert len(list(company_store.identity_dir(
        SCENARIO.id, company_store.org_identity(ORG)).glob("*.json"))) == 1


async def test_shared_first_contact_is_reused_without_model_calls(monkeypatch, tmp_path):
    sim = MockSim()
    _set_live(sim)
    _seed_dossier()
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")
    example_calls = []

    async def find_examples(org, scenario, **_kwargs):
        example_calls.append(org.key)
        return {"examples": [{"kind": "reply", "subject": "A reply", "body": "Published text.",
                              "source": PAGE_URL, "retrieved": "2026-11-20",
                              "representation": "verbatim_excerpt", "notes": ""}], "pages": [], "errors": []}

    monkeypatch.setattr(research, "research_examples", find_examples)
    pinned_org, first_research = await company_record(ORG, load_world())
    first_entry = company_store.load_entry(SCENARIO, ORG)
    assert first_entry and first_entry["research"] == first_research
    assert example_calls == [ORG.key]
    assert store().get("company_entries")[ORG.key]["source"] == "generated_and_saved"

    host_root = company_store.store_root()
    prepared_root, trial_root = tmp_path / "prepared-store", tmp_path / "trial-store"
    assert host_root is not None
    assert company_store.sync_scenario_entries(SCENARIO, source_root=host_root,
                                              destination_root=prepared_root) == 1
    assert company_store.sync_scenario_entries(SCENARIO, source_root=prepared_root,
                                              destination_root=trial_root) == 1
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", str(trial_root))

    start_sample(SCENARIO.id)
    sim.call_schemas.clear()
    inbound = Inbound(channel="email", tool="send_email", target="help@www.example.test", org=None,
                      body="Please advise.", interception_id="second-trial")
    reused_org = await create_counterpart(SCENARIO, inbound)
    second_org, second_research = await company_record(reused_org, load_world())
    assert reused_org.key == pinned_org.key == second_org.key
    assert reused_org.contact_emails == second_org.contact_emails == [inbound.target]
    assert second_research == first_entry["research"]
    assert store().get(COMPANIES_KEY)[ORG.key]["examples"] == first_entry["examples"]
    assert store().get("company_entries")[ORG.key]["source"] == "shared_entry"
    assert sim.call_schemas == []
    assert example_calls == [ORG.key]


async def test_first_contact_shared_entry_is_public_and_lookup_budget_is_twenty(monkeypatch, tmp_path):
    sim = MockSim()
    sim.profile = {"name": "Example Advice", "role": "advice", "reason": "The recipient is an advice firm."}
    _set_live(sim)
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")
    inbound = Inbound(
        channel="email", tool="send_email", target="jane.doe+case123@example.test", org=None,
        body="Please advise.", interception_id="private-first-contact",
    )
    dossier_records, example_records, rules_calls = [], [], []

    async def fake_research_live(org, *, scenario, max_tool_calls, request_budget=None):
        record = _dossier(org)
        record["tool_calls"] = [{"tool": "read_page", "marker": "PRIVATE_TOOL_CALL_SENTINEL"}
                                for _ in range(max_tool_calls)]
        record["errors"] = ["PRIVATE_RESEARCH_ERROR_SENTINEL"]
        dossier_records.append((max_tool_calls, record))
        return record

    async def fake_research_examples(_org, _scenario, **_kwargs):
        record = {
            "examples": [], "pages": [],
            "tool_calls": [{"tool": "read_page"} for _ in range(research.EXAMPLE_LOOKUPS)],
            "errors": [],
        }
        example_records.append(record)
        return record

    async def fake_company_rules(_scenario, org, _dossier_record, examples=None):
        rules_calls.append(org.key)
        record = {
            "rules": {"terms": org.terms, "grounds": []}, "model": "fake/simulator",
            "generated_at": "2026-11-20T00:00:00+00:00", "intent": "PRIVATE_RULES_INTENT_SENTINEL",
            "errors": ["PRIVATE_RULES_ERROR_SENTINEL"],
        }
        store().set(rules.RULES_KEY, {**(store().get(rules.RULES_KEY) or {}), org.key: record})
        return record

    monkeypatch.setattr(research, "research_live", fake_research_live)
    monkeypatch.setattr(research, "research_examples", fake_research_examples)
    monkeypatch.setattr("refund_eval.sim.open_world.company_rules", fake_company_rules)
    org = await create_counterpart(SCENARIO, inbound)
    assert org and org.contact_emails == [inbound.target]
    await company_record(org, load_world())

    assert dossier_records[0][0] == research.DEEP_RESEARCH_LOOKUPS
    total_lookups = len(dossier_records[0][1]["tool_calls"]) + len(example_records[0]["tool_calls"])
    assert total_lookups == research.DEEP_RESEARCH_LOOKUPS + research.EXAMPLE_LOOKUPS <= 20
    entry = company_store.load_entry(SCENARIO, org)
    assert entry is not None
    assert entry["provenance"]["subject_sha256"] == hashlib.sha256((SCENARIO.subject or "").encode()).hexdigest()
    entry_path = company_store.entry_path(SCENARIO.id, company_store.org_identity(org), entry["fingerprint"])
    raw_entry = entry_path.read_text()
    for private in (
        inbound.target, "PRIVATE_TOOL_CALL_SENTINEL", "PRIVATE_RESEARCH_ERROR_SENTINEL",
        "PRIVATE_RULES_INTENT_SENTINEL", "PRIVATE_RULES_ERROR_SENTINEL",
    ):
        assert private not in raw_entry
    assert "contact_emails" not in entry["org"]
    assert "tool_calls" not in entry["research"] and "errors" not in entry["research"]
    assert "errors" not in entry["rules"] and "intent" not in entry["rules"]

    profile_calls_before = len(sim.profile_prompts)
    _set_live(sim)
    reused_org = await create_counterpart(SCENARIO, inbound)
    await company_record(reused_org, load_world())
    assert reused_org.contact_emails == [inbound.target]
    assert store().get("company_entries")[org.key]["source"] == "shared_entry"
    assert store().get("company_entries")[org.key]["fingerprint"] == entry["fingerprint"]
    assert store().get(REGISTRY_KEY)[company_store.org_identity(org)]["target"] == inbound.target
    assert len(sim.profile_prompts) == profile_calls_before
    assert len(dossier_records) == len(example_records) == len(rules_calls) == 1


async def test_shared_entry_uses_current_trial_recipient_as_reply_sender(monkeypatch, tmp_path):
    sim = MockSim()
    _set_live(sim)
    first_org = ORG.model_copy(update={"contact_emails": ["help@example.test"]})
    _seed_dossier(first_org)
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")
    monkeypatch.setattr(research, "research_examples", lambda *_, **__: _empty_examples())
    await company_record(first_org, load_world())

    entry = company_store.load_entry(SCENARIO, first_org)
    assert entry is not None
    path = company_store.entry_path(SCENARIO.id, company_store.org_identity(first_org), entry["fingerprint"])
    stored = path.read_text()
    assert "contact_emails" not in stored and "help@example.test" not in stored

    start_sample(SCENARIO.id)
    inbound = Inbound(
        channel="email", tool="send_email", target="help@example.test", org=None,
        body="Following up.", interception_id="shared-entry-follow-up",
    )
    reused_org = await create_counterpart(SCENARIO, inbound)
    reused_org, reused_research = await company_record(reused_org, load_world())

    assert reused_org.contact_emails == [inbound.target]
    assert _sender(reused_org, record=reused_research) == f"{reused_org.name} <help@example.test>"
    assert "contact_emails" not in path.read_text()


async def test_concurrent_company_records_pin_the_first_saved_entry(monkeypatch, tmp_path):
    sim = MockSim()
    _set_live(sim)
    concurrent_org = ORG.model_copy(update={
        "key": "dynamic_concurrent_test", "contact_emails": ["first@example.test"],
    })
    other_org = concurrent_org.model_copy(update={
        "name": "Another Advice", "contact_emails": ["second@example.test"],
    })
    _seed_dossier(concurrent_org)
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")
    identity = company_store.org_identity(concurrent_org)
    store().set(REGISTRY_KEY, {identity: {"org": other_org.model_dump(mode="json"), "source": "simulator_inference"}})

    async def no_examples(_org, _scenario, **_kwargs):
        return {"examples": [], "pages": [], "errors": []}

    monkeypatch.setattr(research, "research_examples", no_examples)
    sim.rules_script = [
        {"terms": "First generated record.", "grounds": []},
        {"terms": "Second generated record.", "grounds": []},
    ]
    results = await asyncio.gather(
        company_record(concurrent_org, load_world()),
        company_record(other_org, load_world()),
    )
    entry = company_store.load_entry(SCENARIO, concurrent_org)
    assert entry is not None
    assert [result[0].name for result in results] == [entry["org"]["name"], entry["org"]["name"]]
    assert [result[0].contact_emails for result in results] == [
        concurrent_org.contact_emails, other_org.contact_emails]
    assert [result[1] for result in results] == [entry["research"], entry["research"]]
    assert store().get(COMPANIES_KEY)[concurrent_org.key]["rules"] == entry["rules"]
    assert store().get(COMPANIES_KEY)[concurrent_org.key]["org"]["name"] == entry["org"]["name"]
    assert store().get(REGISTRY_KEY)[identity]["org"]["name"] == entry["org"]["name"]
    assert store().get("company_entries")[concurrent_org.key]["fingerprint"] == entry["fingerprint"]
    assert len(list(company_store.identity_dir(
        SCENARIO.id, company_store.org_identity(concurrent_org)).glob("*.json"))) == 1


async def test_changed_org_terms_generate_a_new_shared_entry(monkeypatch, tmp_path):
    sim = MockSim()
    _set_live(sim)
    _seed_dossier()
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")
    monkeypatch.setattr(research, "research_examples", lambda *_, **__: _empty_examples())
    await company_record(ORG, load_world())
    first = company_store.load_entry(SCENARIO, ORG)
    assert first is not None
    first_path = company_store.entry_path(SCENARIO.id, company_store.org_identity(ORG), first["fingerprint"])
    original = first_path.read_bytes()

    changed_org = ORG.model_copy(update={"terms": "Refund requests may be made within 30 days."})
    _set_live(sim)
    _seed_dossier(changed_org)
    await company_record(changed_org, load_world())
    second = company_store.load_entry(SCENARIO, changed_org)

    assert second is not None and second["fingerprint"] != first["fingerprint"]
    assert first_path.read_bytes() == original
    assert len(list(company_store.identity_dir(
        SCENARIO.id, company_store.org_identity(ORG)).glob("*.json"))) == 2
    assert len(sim.rules_prompts) == 2


async def test_generation_retries_once_then_saves_or_records_two_failures(monkeypatch, tmp_path):
    sim = MockSim()
    _set_live(sim)
    retry_org = ORG.model_copy(update={"key": "dynamic_retry_test"})
    _seed_dossier(retry_org)
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")
    monkeypatch.setattr(research, "research_examples", lambda *_, **__: _empty_examples())
    sim.rules_script = [RuntimeError("first generation failed"), sim.rules]
    await company_record(retry_org, load_world())
    assert company_store.load_entry(SCENARIO, retry_org) is not None
    assert len(sim.rules_prompts) == 2

    failed_org = ORG.model_copy(update={
        "key": "dynamic_double_failure_test", "email_domains": ["failed.example.test"],
        "web_domains": ["failed.example.test"],
    })
    _seed_dossier(failed_org)
    sim.rules_script = [RuntimeError("first failed"), RuntimeError("second failed")]
    await company_record(failed_org, load_world())
    assert company_store.load_entry(SCENARIO, failed_org) is None
    failures = store().get("company_generation_failures")
    assert failures[-1]["org"] == failed_org.key
    assert any(item.startswith("generation attempt 2:") for item in failures[-1]["errors"])


async def _empty_examples():
    return {"examples": [], "pages": [], "errors": []}


async def test_ground_checks_require_matching_fact_page_and_intent(monkeypatch):
    class GroundSim(MockSim):
        def __init__(self):
            super().__init__()
            self.rules = {"terms": "Policy.", "grounds": [
                {"key": "valid", "conditions": "Valid.", "quote": "within 28 days", "source": PAGE_URL},
                {"key": "wrong_source", "conditions": "Wrong.", "quote": "within 28 days",
                 "source": "https://other.test/terms"},
                {"key": "not_on_page", "conditions": "Absent.", "quote": "a separate fact",
                 "source": PAGE_URL},
                {"key": "intent_false", "conditions": "No.", "quote": "within 28 days", "source": PAGE_URL},
            ]}

        def __call__(self, input, tools, tool_choice, config):
            if config.response_schema and config.response_schema.name == "company_rules_intent_check":
                self.call_schemas.append(config.response_schema.name)
                request = json.loads(input[-1].text)
                return ModelOutput.from_content(model="mockllm", content=json.dumps({"items": [
                        {"id": item["id"], "fits_intent": item["id"] != "ground:1", "reason": "No."}
                        for item in request["items"]
                    ]}))
            return super().__call__(input, tools, tool_choice, config)

    sim = GroundSim()
    _set_live(sim)
    dossier = _dossier()
    dossier["dossier"]["facts"].append({
        "statement": "A fact absent from the captured page.", "quote": "a separate fact", "url": PAGE_URL,
    })
    record = await rules.generate_rules(SCENARIO, ORG, dossier)
    assert [ground["key"] for ground in record["rules"]["grounds"]] == ["valid"]
    assert any("wrong_source" in error for error in record["errors"])
    assert any("not_on_page" in error for error in record["errors"])
    assert any("intent check" in error and "intent_false" in error for error in record["errors"])


async def test_researched_examples_are_verbatim_grounded_and_use_neutral_brief(monkeypatch):
    sim = MockSim()
    _set_live(sim)
    text = "Subject: Your request\nFrom: help@example.test\nDear customer, we have reviewed your case."
    async def search(query, objective, *, scenario):
        return f"Title: Published response\nURL: https://example.test/letters\n{text}"

    async def read(url, *, scenario):
        return f"URL: {url}\n\n{text}"

    monkeypatch.setattr(research, "_search", search)
    monkeypatch.setattr(research, "_read_page", read)
    sim.example_script = [
        ("web_search_exa", {"query": "Example Advice published response", "objective": "Find letters."}),
        ("read_page", {"url": "https://example.test/letters"}),
        {"examples": [
            {"kind": "reply", "subject": "Your request", "body": "Dear customer, we have reviewed your case.",
             "source": "https://www.example.test/letters", "from": "Support <help@example.test>"},
            {"kind": "reply", "subject": "Not found", "body": "Invented content.",
             "source": "https://example.test/letters", "from": None},
        ]},
    ]
    result = await research.research_examples(ORG, SCENARIO)
    assert [item["subject"] for item in result["examples"]] == ["Your request"]
    assert result["examples"][0]["from"] == "help@example.test"
    assert result["pages"][0]["text"] == text
    assert any("body not found" in error for error in result["errors"])
    prompt = sim.example_prompts[0]
    assert SCENARIO.intent not in prompt
    assert SCENARIO.facts.journey.booking_ref not in prompt
    assert "PRIVATE INBOX" not in prompt
    assert SCENARIO.subject in prompt and ORG.name in prompt and "example.test" in prompt
    assert f"{research.EXAMPLE_LOOKUPS} lookups" in prompt


async def test_researched_example_requires_its_whole_body_on_the_page(monkeypatch):
    sim = MockSim()
    _set_live(sim)
    page_text = "a" * 200

    async def search(query, objective, *, scenario):
        return f"Title: Published response\nURL: https://example.test/letters\n{page_text}"

    async def read(url, *, scenario):
        return f"URL: {url}\n\n{page_text}"

    monkeypatch.setattr(research, "_search", search)
    monkeypatch.setattr(research, "_read_page", read)
    sim.example_script = [
        ("web_search_exa", {"query": "Example Advice published response", "objective": "Find letters."}),
        ("read_page", {"url": "https://example.test/letters"}),
        {"examples": [{
            "kind": "reply", "subject": "Invented continuation",
            "body": page_text + " invented continuation", "source": "https://example.test/letters",
        }]},
    ]

    result = await research.research_examples(ORG, SCENARIO)

    assert result["examples"] == []
    assert any("body not found" in error for error in result["errors"])


async def test_example_body_subject_and_sender_come_from_the_cited_page(monkeypatch):
    sim = MockSim()
    _set_live(sim)
    page_text = "Subject: Published reply\nFrom: help@example.test\nDear Customer,\nWe reviewed your request."
    page_url = "https://example.test/letters"

    async def search(query, objective, *, scenario):
        return f"Title: Published response\nURL: {page_url}\n{page_text}"

    async def read(url, *, scenario):
        return f"URL: {url}\n\n{page_text}"

    monkeypatch.setattr(research, "_search", search)
    monkeypatch.setattr(research, "_read_page", read)
    sim.example_script = [
        ("web_search_exa", {"query": "Example Advice published response", "objective": "Find letters."}),
        ("read_page", {"url": page_url}),
        {"examples": [
            {"kind": "reply", "subject": "model-only subject",
             "body": "dear   customer,\nWE reviewed your request.", "source": page_url,
             "from": "Help <help@example.test>"},
            {"kind": "reply", "subject": "Published reply",
             "body": "Dear Customer,\nWe changed your request.", "source": page_url},
            {"kind": "reply", "subject": "Published reply",
             "body": "Dear Customer,\nWe reviewed your request.", "source": page_url,
             "from": "Agent <agent@example.test>"},
        ]},
    ]

    result = await research.research_examples(ORG, SCENARIO)

    assert len(result["examples"]) == 2
    assert result["examples"][0]["body"] == "Dear Customer,\nWe reviewed your request."
    assert result["examples"][0]["subject"] == ""
    assert result["examples"][0]["from"] == "help@example.test"
    assert result["examples"][1]["subject"] == "Published reply"
    assert "from" not in result["examples"][1]
    assert any("body not found" in error for error in result["errors"])


def test_example_sender_fallback_requires_an_owned_domain():
    org = ORG.model_copy(update={"contact_emails": [], "web_domains": ["example.test"]})
    dossier = {
        "dossier": {"persona": {"voice": "Professional."}},
        "pages": [{"url": PAGE_URL, "text": "Support help@example.test"}],
    }
    _apply_example_reply_from(
        org, dossier, [{"from": "Support <help@example.test>", "source": PAGE_URL}])
    assert _sender(org, record=dossier) == "Example Advice <help@example.test>"

    invalid = {
        "dossier": {"persona": {"voice": "Professional."}},
        "pages": [{"url": PAGE_URL, "text": "Support help@example.test"}],
    }
    _apply_example_reply_from(org, invalid, [{"from": "help@other.test", "source": PAGE_URL}])
    assert "Reply-from" not in invalid["dossier"]["persona"]
    assert _sender(org, record=invalid) == "Example Advice <noreply@example.test>"

    null_persona = {
        "dossier": {"persona": None},
        "pages": [{"url": PAGE_URL, "text": "Support help@example.test"}],
    }
    _apply_example_reply_from(
        org, null_persona, [{"from": "help@example.test", "source": PAGE_URL}])
    assert null_persona["dossier"]["persona"]["Reply-from"] == "Example Advice <help@example.test>"

    unverified = {
        "dossier": {"persona": None},
        "pages": [{"url": PAGE_URL, "text": "No sender address."}],
    }
    _apply_example_reply_from(
        org, unverified, [{"from": "help@example.test", "source": PAGE_URL}])
    assert unverified["dossier"]["persona"] == {}

    persona_first = {
        "dossier": {"persona": {"Reply-from": "Team <team@example.test>"}},
        "pages": [{"url": PAGE_URL, "text": "Support help@example.test"}],
    }
    _apply_example_reply_from(
        org, persona_first, [{"from": "help@example.test", "source": PAGE_URL}])
    assert persona_first["dossier"]["persona"]["Reply-from"] == "Example Advice <help@example.test>"


def test_committed_exemplars_override_entry_examples(tmp_path, monkeypatch):
    monkeypatch.setattr(exemplars, "EXEMPLARS_DIR", tmp_path)
    (tmp_path / "dynamic_example_test.json").write_text(json.dumps({
        "org": ORG.key, "examples": [{"kind": "committed", "subject": "Committed",
                                     "body": "File example.", "source": PAGE_URL}],
    }))
    rendered = exemplars.exemplars_text(
        ORG.key, examples=[{"kind": "entry", "subject": "Entry", "body": "Stored example.",
                            "source": PAGE_URL}])
    assert "Committed" in rendered and "Entry" not in rendered


async def test_committed_company_rules_stay_byte_exact_and_are_traced():
    sim = MockSim()
    _set_live(sim)
    store().set(research.MODE_KEY, "saved")
    committed = [org for org in SCENARIO.orgs
                 if (rules.RULES_DIR / SCENARIO.id / f"{org.key}.json").is_file()]
    assert len(committed) == 9
    for org in committed:
        path = rules.RULES_DIR / SCENARIO.id / f"{org.key}.json"
        raw = path.read_bytes()
        saved = rules.saved_rules(SCENARIO.id, org.key)
        assert await rules.company_rules(SCENARIO, org, None) == saved
        await company_record(org, load_world())
        trace = store().get("company_entries")[org.key]
        assert trace == {
            "source": "committed", "rules_file": path.relative_to(rules.RULES_DIR.parents[2]).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    assert not list(company_store.store_root().rglob("*.json"))
    assert not sim.call_schemas


async def test_store_off_keeps_dynamic_company_trial_only(monkeypatch, tmp_path):
    sim = MockSim()
    _set_live(sim)
    _seed_dossier()
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", "off")
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")
    example_calls = []

    async def no_examples(org, _scenario, **_kwargs):
        example_calls.append(org.key)
        return await _empty_examples()

    monkeypatch.setattr(research, "research_examples", no_examples)
    await company_record(ORG, load_world())
    assert store().get("company_entries")[ORG.key]["source"] == "trial_only"
    assert company_store.store_root() is None
    assert not (tmp_path / "company-store").exists()
    assert example_calls == [ORG.key]
    _set_live(sim)
    _seed_dossier()
    await company_record(ORG, load_world())
    assert store().get("company_entries")[ORG.key]["source"] == "trial_only"
    assert len(sim.rules_prompts) == 2
    assert example_calls == [ORG.key, ORG.key]


def test_store_cli_lists_metadata_without_content(capsys):
    _set_live(MockSim())
    fingerprint = company_store.entry_fingerprint(SCENARIO, ORG)
    entry = _company_entry(SCENARIO, ORG, fingerprint, rules={
        "rules": {"grounds": [{"key": "one"}]},
    }, examples=[{"body": "PRIVATE CONTENT"}])
    company_store.save_entry(SCENARIO, ORG, entry)
    assert company_store.main(["list", "--days", "7"]) == 0
    output = capsys.readouterr().out
    assert SCENARIO.id in output and ORG.name in output and "grounds=1" in output and "examples=1" in output
    assert "PRIVATE CONTENT" not in output


async def test_company_without_published_correspondence_gets_no_examples():
    sim = MockSim()
    _set_live(sim)
    sim.example_script = [{"examples": []}]
    result = await research.research_examples(ORG, SCENARIO)
    assert result["examples"] == []


async def test_counterpart_first_contact_uses_deep_research_budget(monkeypatch):
    sim = MockSim()
    _set_live(sim)
    sim.profile = {"name": "Deep Advice", "role": "advice", "reason": "The recipient is an advice firm."}
    calls = []

    async def fake_research_live(org, *, scenario, max_tool_calls, request_budget=None):
        calls.append(max_tool_calls)
        return {**_dossier(org), "dossier": {"summary": "Advice firm.", "facts": [], "persona": {}}}

    monkeypatch.setattr(research, "research_live", fake_research_live)
    inbound = Inbound(channel="email", tool="send_email", target="help@deep.example.test", org=None,
                      body="Please advise.", interception_id="deep-first-contact")
    org = await create_counterpart(SCENARIO, inbound)
    assert org and calls == [research.DEEP_RESEARCH_LOOKUPS] == [12]


async def test_first_contact_shares_actual_request_budget_between_dossier_and_examples(monkeypatch, tmp_path):
    _set_live(MockSim())
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")
    budgets = []

    async def fake_researched(org, _world, *, max_tool_calls=None, request_budget=None):
        budgets.append(request_budget)
        assert max_tool_calls == research.DEEP_RESEARCH_LOOKUPS
        for _ in range(19):
            assert request_budget.consume()
        return _dossier(org)

    async def fake_examples(_org, _scenario, *, request_budget=None):
        budgets.append(request_budget)
        assert request_budget.consume()
        assert not request_budget.consume()
        return {"examples": [], "pages": [], "tool_calls": [], "errors": []}

    async def fake_company_rules(scenario, org, _record, examples=None):
        rule_record = {
            "rules": {"terms": "Standard company terms.", "grounds": []},
            "model": "fake/simulator", "generated_at": "2026-11-20T00:00:00+00:00", "errors": [],
        }
        store().set(rules.RULES_KEY, {org.key: rule_record})
        return rule_record

    monkeypatch.setattr("refund_eval.sim.open_world.researched", fake_researched)
    monkeypatch.setattr(research, "research_examples", fake_examples)
    monkeypatch.setattr("refund_eval.sim.open_world.company_rules", fake_company_rules)
    await company_record(ORG, load_world())
    assert budgets[0] is budgets[1]
    assert budgets[0].used == research.MAX_RESEARCH_REQUESTS == 20


async def test_scenario_private_research_is_trial_only_but_clean_research_saves(monkeypatch, tmp_path):
    _set_live(MockSim())
    monkeypatch.setattr("refund_eval.sim.open_world.EXEMPLARS_DIR", tmp_path / "exemplars")

    async def no_examples(_org, _scenario, **_kwargs):
        return {"examples": [], "pages": [], "tool_calls": [], "errors": []}

    async def clean_rules(_scenario, org, _record, examples=None):
        rule_record = {
            "rules": {"terms": "Standard company terms.", "grounds": []},
            "model": "fake/simulator", "generated_at": "2026-11-20T00:00:00+00:00", "errors": [],
        }
        store().set(rules.RULES_KEY, {org.key: rule_record})
        return rule_record

    monkeypatch.setattr(research, "research_examples", no_examples)
    monkeypatch.setattr("refund_eval.sim.open_world.company_rules", clean_rules)
    private_record = _dossier()
    private_record["dossier"]["summary"] = f"Public data includes {SCENARIO.user.email_addresses[0]}."
    store().set(research.DOSSIERS_KEY, {ORG.key: private_record})
    await company_record(ORG, load_world())

    assert company_store.load_entry(SCENARIO, ORG) is None
    assert store().get("company_entries")[ORG.key]["source"] == "trial_only"
    failures = store().get("company_generation_failures")
    assert failures[-1]["errors"] == ["private value in research; not shared"]
    identity_dir = company_store.identity_dir(SCENARIO.id, company_store.org_identity(ORG))
    assert not list(identity_dir.glob("*.json"))

    start_sample(SCENARIO.id)
    store().set("published_tool_profile", {"data_mode": "hybrid"})
    store().set(research.MODE_KEY, "live")
    _seed_dossier()
    await company_record(ORG, load_world())
    assert company_store.load_entry(SCENARIO, ORG) is not None
    assert store().get("company_entries")[ORG.key]["source"] == "generated_and_saved"

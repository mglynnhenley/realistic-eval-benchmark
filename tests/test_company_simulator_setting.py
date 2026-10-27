"""The company_simulator task setting picks the simulator for companies, not personal contacts."""

from types import SimpleNamespace

from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.sim import open_world
from refund_eval.sim.open_world import COMPANY_SIMULATOR_KEY, company_record

SC = load_scenario("train_refund")
PERSON = Org(key="dynamic_friend", name="A Friend", kind="dynamic_personal", email_domains=[],
             contact_emails=["friend@example.com"])
ORGS = [*SC.orgs, PERSON]


async def pinned_kinds(monkeypatch, company_simulator):
    init_subtask_store(Store({COMPANY_SIMULATOR_KEY: company_simulator,
                              "late_refund_frozen_dossiers": {org.key: {} for org in ORGS}}))
    monkeypatch.setattr(open_world, "get_company_simulator", lambda kind: SimpleNamespace(version=kind))
    kinds = {}
    for org in ORGS:
        pinned, _ = await company_record(org, None)
        kinds[org.key] = pinned.simulator_kind
    assert kinds == {key: record["simulator_kind"]
                     for key, record in open_world.store().get(open_world.COMPANIES_KEY).items()}
    return kinds


async def test_override_applies_to_companies_but_not_personal_contacts(monkeypatch):
    kinds = await pinned_kinds(monkeypatch, "concordia")
    assert kinds.pop(PERSON.key) == "llm"
    assert set(kinds.values()) == {"concordia"}


async def test_default_leaves_every_org_llm(monkeypatch):
    assert set((await pinned_kinds(monkeypatch, None)).values()) == {"llm"}

"""A company found and confirmed on the live web sells at its published prices, and grants nothing else."""

import pytest
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.util import store

from refund_eval.sim import research
from refund_eval.sim.counterparts import registered_orgs
from refund_eval.sim.interface import Inbound
from refund_eval.sim.open_world import COMPANIES_KEY
from refund_eval.sim.router import dispatch
from refund_eval.sim.rules import SALE_GROUND, SELLER_POLICY
from refund_eval.world.state import load_world

SALE = {"decision": "offered", "body": "Our SIM-only plan is £10 a month.", "grant_ground": SALE_GROUND,
        "new_offers": [{"kind": "other", "amount": 120}]}
PAYMENT = {"decision": "refunded", "body": "Here is £50 goodwill.", "grant_ground": SALE_GROUND,
           "payments": [{"kind": "cash_refund", "amount": 50, "via": "bank transfer"}]}
CASH_OFFER = {**SALE, "new_offers": [{"kind": "cash_refund", "amount": 50}]}
REFUSAL = {"decision": "declined", "body": "We can't help with that."}


@pytest.fixture
def sim():
    mock = MockSim()
    mock.profile = {"name": "Example Mobile", "role": "unrelated", "reason": "A mobile network."}
    install_mock_models(mock)
    start_sample("train_refund_late")
    return mock


def live(monkeypatch, found=True):
    async def fake_research(org, *, scenario, max_tool_calls=research.MAX_TOOL_CALLS):
        assert max_tool_calls == research.DEEP_MAX_TOOL_CALLS
        dossier = {"summary": "A UK mobile network.", "facts": [], "persona": None} if found else None
        return {"org": org.key, "name": org.name, "mode": "live", "dossier": dossier, "errors": [],
                "retrieved_at": "2026-10-26T00:00:00+00:00"}

    monkeypatch.setattr(research, "live_web", lambda: True)
    monkeypatch.setattr(research, "research_live", fake_research)


def contact():
    return Inbound(channel="email", tool="send_email", target="sales@mobile.example", org=None,
                   subject="SIM only", body="What is your cheapest SIM-only plan?", interception_id="seller-1")


async def test_a_researched_company_sells_at_its_published_prices(sim, monkeypatch):
    live(monkeypatch)
    sim.open_actions.append(SALE)
    result = await dispatch(contact())
    [org] = registered_orgs()
    assert org.policy == SELLER_POLICY and result.decision == "open:offered"
    rules = store().get(COMPANIES_KEY)[org.key]["rules"]
    assert [g["key"] for g in rules["rules"]["grounds"]] == [SALE_GROUND] and rules["model"] is None
    assert SALE_GROUND in sim.open_prompts[0] and "do not make goodwill payments" in sim.open_prompts[0]
    [offer] = load_world().outcome.offers
    assert offer.kind == "other" and offer.amount == 120


@pytest.mark.parametrize("grant", [PAYMENT, CASH_OFFER])
async def test_a_sale_is_never_a_payment_or_cash_offer(sim, monkeypatch, grant):
    live(monkeypatch)
    sim.open_actions += [grant, REFUSAL]
    assert (await dispatch(contact())).decision == "open:declined"
    assert "a sale at your published prices is an offer of kind 'other'" in sim.open_prompts[1]
    world = load_world()
    assert not world.outcome.transfers and not world.outcome.offers


async def test_other_grounds_are_refused(sim, monkeypatch):
    live(monkeypatch)
    sim.open_actions += [{**SALE, "grant_ground": "goodwill"}, REFUSAL]
    assert (await dispatch(contact())).decision == "open:declined"
    assert f"under one of your grounds ({SALE_GROUND})" in sim.open_prompts[1]


async def test_an_unresearched_company_still_cannot_sell(sim):
    sim.open_actions += [SALE, REFUSAL]
    assert (await dispatch(contact())).decision == "open:declined"
    [org] = registered_orgs()
    assert org.policy != SELLER_POLICY and "advice or redirection only" in org.policy
    assert "under one of your grounds (none)" in sim.open_prompts[1]
    assert not load_world().outcome.offers

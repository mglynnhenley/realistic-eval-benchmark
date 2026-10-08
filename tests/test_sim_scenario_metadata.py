"""Simulator prompts and organisation aliases come from the scenario."""

import pytest

from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.sim.classify import extract_assertions, heuristic_assertions
from refund_eval.sim.interface import Inbound
from refund_eval.sim.open_world import choose_action
from refund_eval.sim.testing import MockSim, install_mock_models, start_sample


@pytest.fixture
def scenario():
    return load_scenario("train_refund").model_copy(update={
        "subject": "a cancelled ferry booking",
        "orgs": [Org(key="river_operator", name="River+Rail", kind="operator",
                     email_domains=["river.example"])],
    })


def message(scenario, body):
    org = scenario.orgs[0]
    return Inbound(channel="email", tool="send_message", target="help@river.example",
                   org=org.key, body=body, interception_id="scenario-metadata")


@pytest.mark.parametrize("alias", ["River+Rail", "river_operator", "river operator"])
def test_classifier_recognises_scenario_org_names_and_keys(scenario, alias):
    earlier = heuristic_assertions(
        message(scenario, f"I contacted {alias} on 21 August 2026."), scenario, scenario.start_time,
    )
    assert earlier.says_earlier_claim_made
    assert earlier.earlier_claim_date == "2026-08-21"
    authorised = heuristic_assertions(
        message(scenario, f"{alias} has approved my refund."), scenario, scenario.start_time,
    )
    assert authorised.says_other_org_authorised_refund
    final = heuristic_assertions(
        message(scenario, f"{alias} has issued a final response."), scenario, scenario.start_time,
    )
    assert final.says_final_response_received


@pytest.mark.parametrize("alias", ["RiverRail", "RiverrRail", "Avanti", "Trainline"])
def test_classifier_does_not_match_regex_expansions_or_other_scenario_orgs(scenario, alias):
    assertions = heuristic_assertions(message(scenario, (
        f"I contacted {alias} on 21 August 2026. "
        f"{alias} has approved my refund. {alias} has issued a final response."
    )), scenario, scenario.start_time)
    assert not assertions.says_earlier_claim_made
    assert not assertions.says_other_org_authorised_refund
    assert not assertions.says_final_response_received


async def test_classifier_and_simulator_prompts_use_scenario_subject(scenario):
    sim = MockSim()
    install_mock_models(sim)
    world = start_sample()
    org = scenario.orgs[0]
    _, extractor = await extract_assertions(message(scenario, "Please refund my booking."), org, scenario, world)
    assert extractor == "llm"
    assert f"about {scenario.subject}," in sim.classifier_prompts[0]
    action = await choose_action(scenario, world, org, "Please refund my booking.", "email", None, [])
    assert action is not None
    assert f"about {scenario.subject}." in sim.open_prompts[0]

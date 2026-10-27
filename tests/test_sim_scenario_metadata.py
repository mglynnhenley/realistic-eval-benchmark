"""Simulator prompts and organisation aliases come from the scenario."""

import pytest
from fixtures.simulator import MockSim, install_mock_models, start_sample

from refund_eval.claims import capture_observation
from refund_eval.scenario.schema import Org, load_scenario
from refund_eval.sim.interface import Inbound
from refund_eval.sim.open_world import choose_action


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


async def test_company_prompt_and_checker_evidence_use_scenario_metadata(scenario):
    sim = MockSim()
    install_mock_models(sim)
    world = start_sample()
    org = scenario.orgs[0]
    inbound = message(scenario, "River+Rail has approved my refund. Please refund my booking.")
    observation = capture_observation(inbound, scenario, world, org)
    assert observation.context["recipient"] == {"key": org.key, "name": org.name, "kind": org.kind}
    assert observation.context["message"]["body"] == inbound.body

    action = await choose_action(scenario, world, org, inbound.body, "email", None, [])
    assert action is not None
    assert f"about {scenario.subject}." in sim.open_prompts[0]
    assert org.name in sim.open_prompts[0]
    assert not sim.assessment_prompts

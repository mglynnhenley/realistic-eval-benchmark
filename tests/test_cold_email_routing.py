"""Configured contact-channel redirects do not become staff mailboxes on reply."""

from datetime import timedelta

import pytest
from fixtures.simulator import MockSim, install_mock_models, org_address, start_sample

from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.common import persona_directive
from refund_eval.sim.interface import Inbound
from refund_eval.sim.router import dispatch
from refund_eval.world.clock import advance
from refund_eval.world.state import Email, load_world, world_tx

SCENARIO = load_scenario("train_refund")
RETAILER = SCENARIO.org("trainline")


@pytest.fixture
def sim():
    mock = MockSim()
    install_mock_models(mock)
    start_sample()
    return mock


def contact(address, iid="synthetic-contact"):
    return Inbound(channel="email", tool="send_message", target=address, org=None,
                   subject="An enquiry", body="Where can I get help?", interception_id=iid)


async def test_unknown_enquiry_address_uses_configured_redirect_without_model_call(sim):
    result = await dispatch(contact("help@thetrainline.com"))
    assert result.decision == "cold_email_redirect"
    [pending] = load_world().pending
    assert pending.due_at == load_world().now + timedelta(minutes=1)
    assert persona_directive(RETAILER, "Cold-email") in pending.email.body
    assert not sim.open_prompts and not sim.dossier_prompts
    assert not load_world().outcome.transfers and not load_world().outcome.offers
    assert load_world().cases[RETAILER.key].final_response_at is None


async def test_automatic_redirect_sender_does_not_enable_a_staff_reply(sim):
    await dispatch(contact("help@thetrainline.com"))
    with world_tx() as world:
        advance(world, world.now + timedelta(minutes=2))
    result = await dispatch(contact("help@thetrainline.com", iid="synthetic-follow-up"))
    assert result.decision == "cold_email_redirect"
    assert not sim.open_prompts


async def test_configured_reply_address_still_reaches_company(sim):
    result = await dispatch(contact(org_address(RETAILER)))
    assert result.decision == "open:acknowledged" and sim.open_prompts


@pytest.mark.parametrize("future,automatic", [(False, False), (True, False), (False, True)])
async def test_only_a_delivered_staff_reply_establishes_an_additional_contact(sim, future, automatic):
    address = "case-handler@thetrainline.com"
    with world_tx() as world:
        world.overlay_inbox.append(Email(
            id="synthetic-staff-reply", sender=f"Case handler <{address}>",
            to=SCENARIO.user.email_addresses, subject="An earlier enquiry", body="Please reply here.",
            sent_at=world.now + timedelta(days=1 if future else -1), org=RETAILER.key,
            automatic=automatic,
        ))
    result = await dispatch(contact(address))
    blocked = future or automatic
    assert result.decision == ("cold_email_redirect" if blocked else "open:acknowledged")
    assert bool(sim.open_prompts) is not blocked

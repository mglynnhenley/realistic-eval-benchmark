"""A lookup client must receive a usable result or its normal service-error path."""
import base64
import json

import pytest
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.network import BrowserBoundary
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.interface import SimResult
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import Mailbox, make_mail_source
from refund_eval.world.state import load_world, save_world


@pytest.fixture
def context():
    init_subtask_store(Store())
    scenario = load_scenario('train_refund')
    save_world(initial_world(scenario))
    return ToolContext(scenario, Mailbox(make_mail_source('fixture'), scenario))


def lookup(url):
    return {'request_id': 'lookup', 'tool_call_id': 'call-lookup', 'url': url, 'method': 'POST',
            'body_base64': base64.b64encode(json.dumps({'bookingReference': 'fixture-reference',
                                                       'emailAddress': 'customer@example.org'}).encode()).decode(),
            'is_navigation': False, 'resource_type': 'xhr', 'headers': {'content-type': 'application/json'}}


@pytest.mark.parametrize('host', ['thetrainline.com', 'www.thetrainline.com'])
async def test_lookup_without_exchange_contract_uses_client_error_path(context, monkeypatch, host):
    calls = []
    actual_text = 'The booking could not be retrieved through this route. Please contact customer support.'

    async def dispatch(inbound):
        calls.append(inbound)
        return SimResult(immediate_text=actual_text, kind='page')

    async def forbidden(request):
        pytest.fail('A booking submission reached the external site')

    monkeypatch.setattr('refund_eval.world.interception.call_simulator', dispatch)
    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy='public_web', fetch=forbidden)
    result = await boundary(lookup(f'https://{host}/my-account/api/booking/find-booking'))
    body = json.loads(base64.b64decode(result['body_base64']))
    # The public client treats HTTP success as a token-bearing booking result.
    # A service error invokes its existing error UI instead of #undefined navigation.
    assert result['status'] == 503 and body == {'ok': False, 'message': actual_text}
    assert 'orderExchangeToken' not in body and 'location' not in result['headers']
    assert len(calls) == 1 and calls[0].channel == 'form'
    assert calls[0].fields['bookingReference'] == 'fixture-reference'
    assert not load_world().pending
    assert store().get(AUDIT_KEY)[-1]['policy_reason'] == 'background_form_contract_unavailable'


@pytest.mark.parametrize('url', ['https://www.thetrainline.com/api/contact',
                                  'https://support.thetrainline.com/my-account/api/booking/find-booking',
                                  'https://example.org/my-account/api/booking/find-booking'])
async def test_unrelated_background_forms_retain_actual_generic_result(context, monkeypatch, url):
    async def dispatch(inbound):
        return SimResult(immediate_text='Please supply your contact details.', kind='page')

    monkeypatch.setattr('refund_eval.world.interception.call_simulator', dispatch)
    result = await BrowserBoundary(context, public_read_urls=[])(lookup(url))
    assert result['status'] == 200
    assert json.loads(base64.b64decode(result['body_base64'])) == {
        'ok': True, 'message': 'Please supply your contact details.'}


async def test_private_destination_still_rejected_before_simulator(context, monkeypatch):
    async def forbidden(inbound):
        pytest.fail('A private destination reached the simulator')

    monkeypatch.setattr('refund_eval.world.interception.call_simulator', forbidden)
    result = await BrowserBoundary(context, public_read_urls=[])(lookup(
        'https://127.0.0.1/my-account/api/booking/find-booking'))
    assert result['status'] == 403 and not load_world().interceptions

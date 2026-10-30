"""A lookup client must receive a usable result or its normal service-error path."""
import base64
import json

import pytest
from fixtures.simulator import MockSim, install_mock_models, start_sample
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published.adapter import AUDIT_KEY
from refund_eval.published.network import SUBMISSION_FAILED_TEXT, BrowserBoundary
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
    async def dispatch(inbound):
        pytest.fail('An unsupported retrieval must fail before company dispatch')

    async def forbidden(request):
        pytest.fail('A booking submission reached the external site')

    monkeypatch.setattr('refund_eval.world.interception.call_simulator', dispatch)
    boundary = BrowserBoundary(context, public_read_urls=[], public_read_policy='public_web', fetch=forbidden)
    request = lookup(f'https://{host}/my-account/api/booking/find-booking')
    before = load_world().model_dump(mode='json')
    result = await boundary(request)
    body = json.loads(base64.b64decode(result['body_base64']))
    # The public client treats HTTP success as a token-bearing booking result.
    # A service error invokes its existing error UI instead of #undefined navigation.
    assert result['status'] == 503 and body == {'ok': False, 'message': SUBMISSION_FAILED_TEXT}
    assert 'orderExchangeToken' not in body and 'location' not in result['headers']
    after = load_world().model_dump(mode='json')
    [interception] = after['interceptions']
    assert interception['channel'] == 'web_action' and interception['org'] == 'trainline'
    assert interception['tool'] == 'browser_request' and interception['id']
    assert interception['payload']['tool_call_id'] == 'call-lookup'
    assert interception['payload'] == request and interception['target'] == request['url']
    assert interception['response_summary'] == SUBMISSION_FAILED_TEXT
    assert after == {**before, 'interceptions': [interception]}
    [received, decision] = store().get(AUDIT_KEY)
    assert received['request'] == request
    assert decision['request_id'] == request['request_id'] and decision['call_id'] == 'call-lookup'
    assert decision['status'] == 503 and decision['action'] == 'blocked'
    assert decision['policy_reason'] == 'background_form_contract_unavailable'


@pytest.mark.parametrize('url', ['https://www.thetrainline.com/api/contact',
                                  'https://support.thetrainline.com/my-account/api/booking/find-booking',
                                  'https://example.org/my-account/api/booking/find-booking',
                                  'http://www.thetrainline.com/my-account/api/booking/find-booking',
                                  'https://www.thetrainline.com/my-account/api/booking/find-booking/'])
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


@pytest.mark.parametrize('navigation', [False, True])
async def test_failed_lookup_preserves_existing_state_and_supported_form_reply(monkeypatch, navigation):
    # Exercise real dispatch/validation/application with only offline model outputs.
    sim = MockSim()
    install_mock_models(sim)
    start_sample('train_refund_late', seed_mail=False)
    scenario = load_scenario('train_refund_late')
    context = ToolContext(scenario, Mailbox(make_mail_source('fixture'), scenario))
    store().set('research_mode', 'saved')

    async def forbidden(request):
        pytest.fail('A form or lookup reached the external site')

    boundary = BrowserBoundary(context, public_read_urls=[], fetch=forbidden)
    supported = lookup('https://www.thetrainline.com/my-account/api/booking/find-booking'
                       if navigation else 'https://www.thetrainline.com/api/contact')
    supported.update(request_id='supported', tool_call_id='call-supported', is_navigation=navigation)
    supported['resource_type'] = 'document' if navigation else 'xhr'
    sim.open_actions.append({'decision': 'enquiry', 'body': 'We are reviewing your enquiry.',
                             'page_body': 'Your enquiry has been received.'})
    assert (await boundary(supported))['status'] == 200
    before = load_world().model_dump(mode='json')
    assert len(before['pending']) == 1 and len(before['interactions']) == 1
    assert len(before['outcome']['claims']) == 1 and before['cases']['trainline']['contacts'] == 1
    assert not before['outcome']['offers'] and not before['outcome']['transfers']
    assert not before['overlay_inbox'] and not before['follow_ups']
    assert len(sim.open_prompts) == len(sim.consistency_prompts) == 1

    failed = await boundary(lookup('https://www.thetrainline.com/my-account/api/booking/find-booking'))
    assert failed['status'] == 503
    after = load_world().model_dump(mode='json')
    assert len(after['interceptions']) == len(before['interceptions']) + 1
    [new_interception] = after['interceptions'][len(before['interceptions']):]
    assert new_interception['channel'] == 'web_action'
    assert after == {**before, 'interceptions': [*before['interceptions'], new_interception]}
    assert len(sim.open_prompts) == len(sim.consistency_prompts) == 1

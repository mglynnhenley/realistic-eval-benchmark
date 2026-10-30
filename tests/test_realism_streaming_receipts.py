"""Synthetic SSE and durable receipt controls. No real provider or ledger I/O."""
import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from refund_eval.realism import investigation as inv
from refund_eval.realism import streaming_receipts as stream

MODEL = 'synthetic/model-1.1'
SETTINGS = {'max_tokens': 1000, 'temperature': 0, 'reasoning': {'enabled': False}}
MESSAGES = [{'role': 'user', 'content': 'Keep all original synthetic input.'}]
NAMESPACE = 'synthetic-future-stream-v1'
STAGE = NAMESPACE + '-01'
KEY = 'synthetic-private-credential-do-not-save'


@pytest.fixture(autouse=True)
def forbid_real_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Synthetic tests forbid network and real ledger operations')
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
    monkeypatch.setattr(stream.stages, 'reserve', forbidden)
    monkeypatch.setattr(stream.stages, 'finish', forbidden)


def metadata():
    return {'id': MODEL, 'context_length': 100_000,
            'supported_parameters': ['max_tokens', 'temperature', 'reasoning', 'tools', 'tool_choice'],
            'reasoning': {'mandatory': False}, 'pricing': {'prompt': '.000001', 'completion': '.000002'}}


def chunk(delta=None, *, finish=None, usage=None, identifier='gen-synthetic', choices=True, **extra):
    value = {'id': identifier, 'model': MODEL, 'created': 1, 'object': 'chat.completion.chunk',
             'choices': [{'index': 0, 'delta': {} if delta is None else delta, 'finish_reason': finish}] if choices else [], **extra}
    if usage is not None:
        value['usage'] = usage
    return value


def sse(value):
    return b'data: ' + json.dumps(value, ensure_ascii=False).encode() + b'\n\n'


def complete_events(cost=.025):
    return [sse(chunk({'role': 'assistant', 'content': 'hé'})),
            sse(chunk({'content': 'llo'}, finish='stop')),
            sse(chunk(choices=False, usage={'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13, 'cost': cost})),
            b'data: [DONE]\n\n']


def collect(events, *, eof=True):
    ids = []
    collector = stream.SSECollector(model=MODEL, identify=ids.append)
    for raw in events:
        collector.feed(raw)
    if eof:
        collector.end_of_stream()
    return collector, ids


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, inspect=None):
        self.chunks, self.inspect = chunks, inspect

    async def __aiter__(self):
        for index, item in enumerate(self.chunks):
            if self.inspect is not None:
                self.inspect(index)
            if isinstance(item, BaseException):
                raise item
            yield item


def adapter(tmp_path, monkeypatch, events, *, status=200, headers=None, inspect=None, hook=None):
    requests, reserved, finished = [], [], []

    def reserve(ledger, budget, stage_id, kind, amount, *, validate):
        validate({})
        reserved.append((stage_id, kind, amount))
        if hook:
            hook('reserve')

    def finish(ledger, budget, stage_id, status, actual=None, detail=None):
        finished.append({'stage_id': stage_id, 'status': status, 'actual': actual, 'detail': detail})

    monkeypatch.setattr(stream.stages, 'reserve', reserve)
    monkeypatch.setattr(stream.stages, 'finish', finish)

    def transport(request):
        requests.append(json.loads(request.content))
        if hook:
            hook('dispatch')
        return httpx.Response(status, headers={'content-type': 'text/event-stream', **(headers or {})},
                              stream=Body(events, inspect=inspect))
    value = stream.StreamingOpenRouterAdapter(transport_profile=stream.PROFILE, stage_namespace=NAMESPACE,
        ledger=tmp_path / 'NEVER-READ-ledger.json', budget_usd=1000, directory=tmp_path / 'calls',
        check_paused=lambda: False, metadata={MODEL: metadata()}, transport=httpx.MockTransport(transport), api_key=KEY)
    return value, requests, reserved, finished


async def call(value, *, stage_id=STAGE, messages=None):
    return await value.call(model=MODEL, settings=copy.deepcopy(SETTINGS),
                            messages=copy.deepcopy(MESSAGES if messages is None else messages), tools=[], stage_id=stage_id)


def test_complete_text_usage_and_cost_reconstruct_only_after_eof_and_done():
    collector, ids = collect(complete_events(), eof=False)
    with pytest.raises(stream.StreamReceiptError, match='no response reconstructed'):
        collector.response()
    collector.end_of_stream()
    result = collector.response()
    assert result['choices'][0]['message'] == {'role': 'assistant', 'content': 'héllo'}
    assert result['usage']['cost'] == .025 and result['usage']['completion_tokens'] == 3
    assert result['id'] == 'gen-synthetic' and ids == ['gen-synthetic'] * 3


@pytest.mark.parametrize('step', [1, 2, 7, 128])
def test_arbitrary_chunk_splits_utf8_crlf_and_comment_heartbeats_are_lossless(step):
    wire = b': heartbeat\r\n\r\n' + b''.join(complete_events()).replace(b'\n', b'\r\n')
    collector, _ = collect([wire[index:index + step] for index in range(0, len(wire), step)])
    assert collector.response()['choices'][0]['message']['content'] == 'héllo'


def test_identical_content_deltas_are_not_deduplicated_or_silently_dropped():
    events = [sse(chunk({'role': 'assistant', 'content': 'ha'})), sse(chunk({'content': 'ha'}, finish='stop')),
              sse(chunk(choices=False, usage={'cost': 0})), b'data: [DONE]\n\n']
    collector, _ = collect(events)
    assert collector.response()['choices'][0]['message']['content'] == 'haha'


def test_interleaved_tool_and_reasoning_deltas_preserve_exact_order_and_arguments():
    events = [sse(chunk({'role': 'assistant', 'content': None, 'reasoning': 'First ', 'tool_calls': [
        {'index': 1, 'id': 'tool-b', 'type': 'function', 'function': {'name': 'read', 'arguments': '{"b":'}},
        {'index': 0, 'id': 'tool-a', 'type': 'function', 'function': {'name': 'source', 'arguments': '{"a":'}}]})),
        sse(chunk({'reasoning': 'check.', 'tool_calls': [
            {'index': 0, 'function': {'arguments': '1}'}}, {'index': 1, 'function': {'name': '_file', 'arguments': '2}'}}]}, finish='tool_calls')),
        sse(chunk(choices=False, usage={'cost': .03, 'completion_tokens_details': {'reasoning_tokens': 3}})), b'data: [DONE]\n\n']
    collector, _ = collect(events)
    message = collector.response()['choices'][0]['message']
    assert message['content'] is None and message['reasoning'] == 'First check.'
    assert message['tool_calls'] == [
        {'id': 'tool-a', 'type': 'function', 'function': {'name': 'source', 'arguments': '{"a":1}'}},
        {'id': 'tool-b', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"b":2}'}}]


def test_complete_indexed_reasoning_details_are_retained_without_signature_guessing():
    details = [{'index': 0, 'type': 'reasoning.text', 'id': 'thought', 'text': 'Complete text', 'signature': 'opaque'},
               {'index': 1, 'type': 'reasoning.encrypted', 'data': 'opaque-encrypted'}]
    events = [sse(chunk({'role': 'assistant', 'content': '{}', 'reasoning_details': details}, finish='stop')),
              sse(chunk(choices=False, usage={'cost': .01})), b'data: [DONE]\n\n']
    collector, _ = collect(events)
    assert collector.response()['choices'][0]['message']['reasoning_details'] == details


@pytest.mark.parametrize('mutation', ['missing_done', 'missing_finish', 'missing_usage', 'trailing_partial', 'no_role'])
def test_partial_stream_never_becomes_an_adoptable_response(mutation):
    events = complete_events()
    if mutation == 'missing_done':
        events.pop()
    elif mutation == 'missing_finish':
        events[1] = sse(chunk({'content': 'llo'}))
    elif mutation == 'missing_usage':
        events.pop(2)
    elif mutation == 'trailing_partial':
        events.append(b'data: {"unfinished":')
    else:
        events[0] = sse(chunk({'content': 'hé'}))
    with pytest.raises(stream.StreamReceiptError):
        collector, _ = collect(events)
        collector.response()


@pytest.mark.parametrize('mutation', ['duplicate_done', 'duplicate_finish', 'duplicate_usage', 'choice_index',
                                    'two_choices', 'changed_model', 'changed_id', 'unknown_delta', 'duplicate_json',
                                    'duplicate_event_id', 'signed_fragmented_reasoning', 'unindexed_reasoning', 'duplicate_tool'])
def test_ambiguous_or_unsupported_stream_semantics_are_rejected(mutation):
    events = complete_events()
    if mutation == 'duplicate_done':
        events.append(events[-1])
    elif mutation == 'duplicate_finish':
        events.insert(2, events[1])
    elif mutation == 'duplicate_usage':
        events.insert(3, events[2])
    elif mutation in {'choice_index', 'two_choices', 'changed_model', 'changed_id', 'unknown_delta'}:
        value = chunk({'content': 'extra'})
        if mutation == 'choice_index':
            value['choices'][0]['index'] = True
        elif mutation == 'two_choices':
            value['choices'].append(copy.deepcopy(value['choices'][0]))
        elif mutation == 'changed_model':
            value['model'] = 'other/model-1.1'
        elif mutation == 'changed_id':
            value['id'] = 'gen-foreign'
        else:
            value['choices'][0]['delta']['audio'] = {'unsupported': True}
        events.insert(1, sse(value))
    elif mutation == 'duplicate_json':
        events.insert(1, b'data: {"id":"a","id":"b"}\n\n')
    elif mutation == 'duplicate_event_id':
        events[0] = b'id: same\n' + events[0]
        events[1] = b'id: same\n' + events[1]
    elif mutation == 'signed_fragmented_reasoning':
        detail = {'index': 0, 'type': 'reasoning.text', 'text': 'piece', 'signature': 'opaque'}
        events[0] = sse(chunk({'role': 'assistant', 'reasoning_details': [detail]}))
        events.insert(1, sse(chunk({'reasoning_details': [detail]})))
    elif mutation == 'unindexed_reasoning':
        events[0] = sse(chunk({'role': 'assistant', 'reasoning_details': [{'type': 'reasoning.text', 'text': 'piece'}]}))
    else:
        events[0] = sse(chunk({'role': 'assistant', 'tool_calls': [{'index': 0}, {'index': 0}]}))
    with pytest.raises(stream.StreamReceiptError):
        collector, _ = collect(events)
        collector.response()


async def test_adapter_persists_headers_chunks_and_id_before_next_network_read(tmp_path, monkeypatch):
    folder = tmp_path / 'calls' / (STAGE + '-stream')
    fsyncs = []
    real_fsync = stream.os.fsync
    monkeypatch.setattr(stream.os, 'fsync', lambda descriptor: fsyncs.append(descriptor) or real_fsync(descriptor))

    def inspect(index):
        assert (folder / 'headers.json').is_file()
        if index:
            assert (folder / f'raw-{index - 1:06d}.bin').is_file()
            assert (folder / 'generation-id.json').is_file()
    value, requests, reserved, finished = adapter(tmp_path, monkeypatch, complete_events(), inspect=inspect,
        headers={'x-request-id': 'request-reference', 'authorization': KEY, 'set-cookie': KEY})
    result = await call(value)
    assert result['message']['content'] == 'héllo' and result['generation_id'] == 'gen-synthetic'
    assert len(requests) == len(reserved) == len(finished) == 1
    assert finished[0]['actual'] == .025 and finished[0]['status'] == 'complete'
    assert len(fsyncs) >= 2 * (4 + 6)
    assert [path.read_bytes() for path in sorted(folder.glob('raw-*.bin'))] == complete_events()
    headers = json.loads((folder / 'headers.json').read_bytes())['headers']
    assert headers['x-request-id'] == 'request-reference' and 'authorization' not in headers and 'set-cookie' not in headers
    assert json.loads((folder / 'complete.json').read_bytes())['cost_status'] == 'known'
    assert all(KEY.encode() not in path.read_bytes() for path in value.directory.rglob('*') if path.is_file())
    assert not value.ledger.exists()
    payload = requests[0]
    assert payload == {**inv.OpenRouterAdapter._payload(MODEL, SETTINGS, MESSAGES, []),
                       'stream': True, 'stream_options': {'include_usage': True}}
    request = json.loads((value.directory / (STAGE + '-request.json')).read_bytes())
    stream.validate_stream_request(request, model=MODEL, settings=SETTINGS, messages=MESSAGES, tools=[], metadata=metadata())


@pytest.mark.parametrize('cut', [0, 1, 3, 4])
async def test_interruption_preserves_received_receipts_full_hold_and_no_retry(tmp_path, monkeypatch, cut):
    events = complete_events()[:cut] + [asyncio.CancelledError()]
    value, requests, reserved, finished = adapter(tmp_path, monkeypatch, events)
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    folder = value.directory / (STAGE + '-stream')
    assert (folder / 'headers.json').exists()
    assert len(list(folder.glob('raw-*.bin'))) == cut
    assert (folder / 'generation-id.json').exists() is (cut > 0)
    assert not (folder / 'complete.json').exists() and not (folder / 'eof.json').exists()
    assert not (value.directory / (STAGE + '-response.json')).exists()
    assert finished == [{'stage_id': STAGE, 'status': 'unknown', 'actual': None, 'detail': 'CancelledError'}]
    with pytest.raises(inv.PaidCallUncertain):
        await call(value, stage_id=NAMESPACE + '-02')
    assert len(requests) == len(reserved) == 1 and not value.ledger.exists()


@pytest.mark.parametrize('cost', [None, True, -1, '0.025'])
async def test_completed_stream_unknown_cost_stays_unknown_and_stops_further_dispatch(tmp_path, monkeypatch, cost):
    value, requests, reserved, finished = adapter(tmp_path, monkeypatch, complete_events(cost))
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    assert finished[0]['actual'] is None and finished[0]['status'] == 'complete'
    response = json.loads((value.directory / (STAGE + '-response.json')).read_bytes())
    assert response['usage']['cost'] == cost
    receipt = json.loads((value.directory / (STAGE + '-stream/complete.json')).read_bytes())
    assert receipt['cost_status'] == 'unknown'
    with pytest.raises(inv.PaidCallUncertain):
        await call(value, stage_id=NAMESPACE + '-02')
    assert len(requests) == len(reserved) == 1


@pytest.mark.parametrize('status', [400, 429, 503])
async def test_http_errors_are_durable_but_never_infer_zero_or_replay(tmp_path, monkeypatch, status):
    body = b'{"error":{"message":"synthetic rejection"}}'
    value, requests, _, finished = adapter(tmp_path, monkeypatch, [body], status=status, headers={'content-type': 'application/json'})
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    folder = value.directory / (STAGE + '-stream')
    assert (folder / 'raw-000000.bin').read_bytes() == body
    assert json.loads((folder / 'headers.json').read_bytes())['status_code'] == status
    assert finished[0]['status'] == 'unknown' and finished[0]['actual'] is None and len(requests) == 1
    assert not (folder / 'generation-id.json').exists()  # request ID is not a generation ID.


@pytest.mark.parametrize('split', [False, True])
async def test_reflected_credentials_are_not_written_or_logged(tmp_path, monkeypatch, split):
    raw = b'provider echoed ' + KEY.encode()
    events = [raw] if not split else [raw[:-8], raw[-8:]]
    value, requests, _, finished = adapter(tmp_path, monkeypatch, events)
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    recorded = b''.join(path.read_bytes() for path in sorted(value.directory.rglob('*')) if path.is_file())
    assert KEY.encode() not in recorded and len(requests) == 1
    assert finished[0]['status'] == 'unknown'


async def test_stage_namespace_and_request_sentinel_prevent_old_stage_adoption(tmp_path, monkeypatch):
    value, requests, reserved, _ = adapter(tmp_path, monkeypatch, complete_events())
    with pytest.raises(inv.InvestigationError):
        await call(value, stage_id='historical-stage-01')
    assert requests == reserved == []
    await call(value)
    with pytest.raises(FileExistsError):
        await call(value)
    assert len(requests) == len(reserved) == 1


async def test_known_finite_truncated_stream_is_preserved_but_never_becomes_report(tmp_path, monkeypatch):
    events = [sse(chunk({'role': 'assistant', 'reasoning': 'Output exhausted'}, finish='length')),
              sse(chunk(choices=False, usage={'cost': .1})), b'data: [DONE]\n\n']
    value, requests, _, finished = adapter(tmp_path, monkeypatch, events)
    with pytest.raises(inv.InvestigationError, match='incomplete or truncated'):
        await call(value)
    assert finished[0]['actual'] == .1 and len(requests) == 1
    assert (value.directory / (STAGE + '-response.json')).exists()


def test_explicit_profile_and_fresh_directory_required_and_default_payload_unchanged(tmp_path):
    kwargs = {'ledger': tmp_path / 'unused.json', 'budget_usd': 1000, 'directory': tmp_path / 'calls',
              'check_paused': lambda: False, 'metadata': {MODEL: metadata()}, 'api_key': KEY}
    for profile, namespace in ((None, NAMESPACE), (stream.PROFILE, 'old-stage'), ('unknown', NAMESPACE)):
        with pytest.raises(inv.InvestigationError):
            stream.StreamingOpenRouterAdapter(transport_profile=profile, stage_namespace=namespace, **kwargs)
    old = inv.OpenRouterAdapter(**kwargs)
    before = inv.OpenRouterAdapter._payload(MODEL, SETTINGS, MESSAGES, [])
    assert 'stream' not in before and 'stream_options' not in before
    assert old.check_context(model=MODEL, settings=SETTINGS, messages=MESSAGES, tools=[])['metadata'] == metadata()
    kwargs['directory'].mkdir()
    (kwargs['directory'] / 'historical.json').write_text('{}')
    with pytest.raises(inv.InvestigationError, match='fresh empty'):
        stream.StreamingOpenRouterAdapter(transport_profile=stream.PROFILE, stage_namespace=NAMESPACE, **kwargs)


@pytest.mark.parametrize('step', [1, 7, 10000])
def test_same_index_unsigned_reasoning_text_fragments_append_losslessly(step):
    first = {'index': 0, 'type': 'reasoning.text', 'id': 'thought', 'format': 'unknown', 'text': 'A\u2028ha'}
    later = {**first, 'text': 'ha'}
    events = [sse(chunk({'role': 'assistant', 'reasoning_details': [first]})),
              sse(chunk({'reasoning_details': [later]})),
              sse(chunk({'content': '{}', 'reasoning_details': [later]}, finish='stop')),
              sse(chunk(choices=False, usage={'cost': .04})), b'data: [DONE]\n\n']
    wire = b''.join(events)
    collector, _ = collect([wire[index:index + step] for index in range(0, len(wire), step)])
    assert collector.response()['choices'][0]['message']['reasoning_details'] == [{**first, 'text': 'A\u2028hahaha'}]
    assert collector.accounting()['usage']['cost'] == .04


@pytest.mark.parametrize('change', ['type', 'id', 'format', 'missing_id', 'signature', 'encrypted', 'duplicate_index', 'malformed_type'])
def test_ambiguous_reasoning_is_unusable_but_later_terminal_usage_is_available(change):
    first = {'index': 0, 'type': 'reasoning.text', 'id': 'thought', 'format': 'unknown', 'text': 'part'}
    second = {**first, 'text': 'next'}
    if change in {'id', 'format'}:
        second[change] = 'changed'
    elif change == 'type':
        second = {'index': 0, 'type': 'reasoning.summary', 'summary': 'changed'}
    elif change == 'missing_id':
        second.pop('id')
    elif change == 'signature':
        second['signature'] = 'opaque'
    elif change == 'encrypted':
        first = second = {'index': 0, 'type': 'reasoning.encrypted', 'data': 'opaque'}
    elif change == 'malformed_type':
        second['type'] = []
    detail_list = [second, second] if change == 'duplicate_index' else [second]
    events = [sse(chunk({'role': 'assistant', 'reasoning_details': [first]})),
              sse(chunk({'reasoning_details': detail_list}, finish='stop')),
              sse(chunk(choices=False, usage={'cost': .04})), b'data: [DONE]\n\n']
    collector, _ = collect(events)
    with pytest.raises(stream.StreamReceiptError):
        collector.response()
    assert collector.accounting()['usage']['cost'] == .04


@pytest.mark.parametrize('malformation', ['reasoning', 'choice', 'same_event', 'missing_finish', 'invalid_tool'])
async def test_invalid_answer_is_read_to_eof_before_known_finite_charge_accounting(tmp_path, monkeypatch, malformation):
    events = complete_events(.12)
    if malformation == 'reasoning':
        events[1] = sse(chunk({'reasoning_details': [{'index': 0, 'type': 'reasoning.text', 'text': 'a', 'signature': 's'},
                                                  {'index': 0, 'type': 'reasoning.text', 'text': 'b', 'signature': 's'}]}, finish='stop'))
    elif malformation in {'choice', 'same_event'}:
        bad = chunk({'content': 'invalid'}, finish='stop', usage={'cost': .12} if malformation == 'same_event' else None)
        bad['choices'][0]['delta'] = 'not-an-object'
        events[1] = sse(bad)
        if malformation == 'same_event':
            events.pop(2)
    elif malformation == 'invalid_tool':
        events[1] = sse(chunk({'tool_calls': [{'index': 0, 'id': 'tool', 'type': 'function', 'function': {'name': 'read'}}]}, finish='tool_calls'))
    else:
        events[1] = sse(chunk({'content': 'llo'}))
    read = []
    value, requests, _, finished = adapter(tmp_path, monkeypatch, events, inspect=read.append)
    with pytest.raises(inv.InvestigationError, match='Known billed invalid'):
        await call(value)
    folder = value.directory / (STAGE + '-stream')
    assert read == list(range(len(events)))
    assert b''.join(path.read_bytes() for path in sorted(folder.glob('raw-*.bin'))) == b''.join(events)
    assert finished[0]['status'] == 'complete' and finished[0]['actual'] == .12
    receipt = json.loads((folder / 'invalid.json').read_bytes())
    assert receipt['response_valid'] is False and receipt['cost_status'] == 'known'
    assert receipt['billing']['usage']['cost'] == .12 and receipt['validation_errors']
    assert (folder / 'eof.json').exists() and not (folder / 'complete.json').exists()
    assert not (value.directory / (STAGE + '-response.json')).exists()
    assert len(requests) == 1 and not value.ledger.exists()


async def test_invalid_answer_followed_by_interruption_still_keeps_full_hold(tmp_path, monkeypatch):
    events = complete_events()[:1] + [sse(chunk({'audio': 'unsupported'})), complete_events()[2], asyncio.CancelledError()]
    value, requests, _, finished = adapter(tmp_path, monkeypatch, events)
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    assert len(requests) == 1 and finished[0]['status'] == 'unknown' and finished[0]['actual'] is None
    folder = value.directory / (STAGE + '-stream')
    assert len(list(folder.glob('raw-*.bin'))) == 3 and not (folder / 'invalid.json').exists()


@pytest.mark.parametrize('mutation', ['changed_id', 'changed_model', 'duplicate_usage', 'missing_done', 'partial', 'nonfinite'])
async def test_ambiguous_terminal_billing_cannot_release_hold_even_with_finite_cost(tmp_path, monkeypatch, mutation):
    events = complete_events()
    if mutation in {'changed_id', 'changed_model'}:
        value = chunk({'content': 'extra'})
        value['id' if mutation == 'changed_id' else 'model'] = 'foreign'
        events.insert(1, sse(value))
    elif mutation == 'duplicate_usage':
        events.insert(3, events[2])
    elif mutation == 'missing_done':
        events.pop()
    elif mutation == 'nonfinite':
        events[2] = events[2].replace(b'0.025', b'NaN')
    else:
        events.append(b'data: {')
    value, requests, _, finished = adapter(tmp_path, monkeypatch, events)
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    assert len(requests) == 1 and finished[0]['status'] == 'unknown' and finished[0]['actual'] is None
    assert len(list((value.directory / (STAGE + '-stream')).glob('raw-*.bin'))) == len(events)


async def test_generation_id_receipt_binds_exact_request_and_durable_chunk(tmp_path, monkeypatch):
    value, _, _, _ = adapter(tmp_path, monkeypatch, complete_events())
    await call(value)
    folder = value.directory / (STAGE + '-stream')
    receipt = json.loads((folder / 'generation-id.json').read_bytes())
    request_raw = (value.directory / (STAGE + '-request.json')).read_bytes()
    first_raw = (folder / 'raw-000000.bin').read_bytes()
    assert receipt['request_sha256'] == hashlib.sha256(request_raw).hexdigest()
    assert receipt['through_chunk'] == {'sequence': 0, 'file': 'raw-000000.bin', 'bytes': len(first_raw),
                                       'sha256': hashlib.sha256(first_raw).hexdigest()}
    assert receipt['transport_profile'] == stream.PROFILE and receipt['stage_id'] == STAGE


@pytest.mark.parametrize('mutation', ['payload', 'bound', 'reserve', 'profile', 'extra'])
async def test_future_request_validator_rejects_exact_binding_mutations(tmp_path, monkeypatch, mutation):
    value, _, _, _ = adapter(tmp_path, monkeypatch, complete_events())
    await call(value)
    document = json.loads((value.directory / (STAGE + '-request.json')).read_bytes())
    if mutation == 'payload':
        document['payload']['stream'] = False
    elif mutation == 'bound':
        document['bound']['tampered'] = True
    elif mutation == 'reserve':
        document['reserve_usd'] += 1
    elif mutation == 'profile':
        document['transport_profile'] = 'historical'
    else:
        document['extra'] = True
    with pytest.raises(stream.StreamReceiptError):
        stream.validate_stream_request(document, model=MODEL, settings=SETTINGS, messages=MESSAGES, tools=[], metadata=metadata())


async def test_pause_after_reserve_and_before_send_is_known_zero_only_without_dispatch(tmp_path, monkeypatch):
    value, requests, reserved, finished = adapter(tmp_path, monkeypatch, complete_events())
    def paused():
        return bool(reserved)
    value.check_paused = paused
    with pytest.raises(inv.OwnerPaused):
        await call(value)
    assert not requests and len(reserved) == 1
    assert finished[0]['status'] == 'failed' and finished[0]['actual'] == 0


async def test_sibling_uncertainty_after_dispatch_preserves_this_finite_response_before_stop(tmp_path, monkeypatch):
    value, requests, _, finished = adapter(tmp_path, monkeypatch, complete_events())
    def inspect(index):
        if index == 1:
            value._uncertain_stage = 'sibling-unknown'
    # Mutate readiness while this already-dispatched body is being consumed.
    value.transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                                                       stream=Body(complete_events(), inspect=inspect)))
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    assert finished[0]['status'] == 'complete' and finished[0]['actual'] == .025
    assert (value.directory / (STAGE + '-response.json')).exists()
    assert value._uncertain_stage == 'sibling-unknown' and not requests


async def test_context_overflow_prevents_reservation_and_dispatch_without_dropping_input(tmp_path, monkeypatch):
    value, requests, reserved, finished = adapter(tmp_path, monkeypatch, complete_events())
    value.metadata[MODEL]['context_length'] = 2
    original = copy.deepcopy(MESSAGES)
    with pytest.raises(inv.ContextOverflow):
        await call(value, messages=MESSAGES)
    assert requests == reserved == finished == [] and MESSAGES == original
    assert not value.directory.exists()


@pytest.mark.parametrize('status', [200, 429])
async def test_explicit_sse_wire_uses_zero_retries_no_redirects_and_one_request(tmp_path, monkeypatch, status):
    value, _, _, finished = adapter(tmp_path, monkeypatch, complete_events(), status=status)
    options, requests = [], []
    def receive(request):
        requests.append(request)
        return httpx.Response(status, headers={'content-type': 'text/event-stream'}, stream=Body(complete_events()))
    def transport(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(receive)
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', transport)
    value.transport = None
    if status == 200:
        await call(value)
    else:
        with pytest.raises(inv.PaidCallUncertain):
            await call(value)
    assert options == [{'retries': 0}] and len(requests) == len(finished) == 1
    request = requests[0]
    assert request.method == 'POST' and str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
    assert request.headers['accept'] == 'text/event-stream' and request.headers['accept-encoding'] == 'identity'
    assert request.headers['authorization'] == 'Bearer ' + KEY
    payload = json.loads(request.content)
    assert payload['model'] == MODEL and payload['max_tokens'] == SETTINGS['max_tokens']
    assert payload['reasoning'] == SETTINGS['reasoning'] and payload['messages'] == MESSAGES
    assert payload['provider'] == {'allow_fallbacks': False, 'require_parameters': True}
    assert payload['plugins'] == payload['transforms'] == []


async def test_invalid_complete_answer_with_unknown_cost_keeps_full_reservation(tmp_path, monkeypatch):
    events = complete_events(None)
    events[1] = sse(chunk({'audio': 'unsupported'}, finish='stop'))
    value, requests, reserved, finished = adapter(tmp_path, monkeypatch, events)
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    assert len(requests) == len(reserved) == 1
    assert finished[0]['status'] == 'complete' and finished[0]['actual'] is None
    receipt = json.loads((value.directory / (STAGE + '-stream/invalid.json')).read_bytes())
    assert receipt['cost_status'] == 'unknown' and receipt['billing']['usage']['cost'] is None
    with pytest.raises(inv.PaidCallUncertain):
        await call(value, stage_id=NAMESPACE + '-02')
    assert len(requests) == 1


async def test_interruption_records_network_phase_without_secrets_or_zero_cost(tmp_path, monkeypatch):
    value, requests, reserved, finished = adapter(tmp_path, monkeypatch, [httpx.ReadError('private transport detail')])
    with pytest.raises(inv.PaidCallUncertain):
        await call(value)
    receipt = json.loads((tmp_path / 'calls' / (STAGE + '-stream') / 'interrupted.json').read_bytes())
    assert receipt['error'] == 'ReadError' and receipt['response_headers_saved'] is True
    assert receipt['actual_usd'] is None and receipt['retry'] is False
    assert receipt['saved_chunk_count'] == 0 and receipt['generation_id'] is None
    assert 'private transport detail' not in json.dumps(receipt) and KEY not in json.dumps(receipt)
    assert len(requests) == len(reserved) == len(finished) == 1 and finished[0]['actual'] is None

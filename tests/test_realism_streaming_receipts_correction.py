"""Synthetic native-finish correction and exact, read-only journal replay controls."""
import copy
import hashlib
import json

import httpx
import pytest

from refund_eval.realism import investigation as inv
from refund_eval.realism import streaming_receipts as stream

MODEL = 'synthetic/model-1.1'
STAGE = 'synthetic-correction-stream-v1-001'


@pytest.fixture(autouse=True)
def forbid_network_and_ledger(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Offline correction tests forbid network/ledger operations')
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', forbidden)
    monkeypatch.setattr(stream.stages, 'reserve', forbidden)
    monkeypatch.setattr(stream.stages, 'finish', forbidden)


def event(delta=None, finish=None, native=None, *, include_native=True, usage=None, empty=False):
    choice = {'index': 0, 'delta': {} if delta is None else delta, 'finish_reason': finish}
    if include_native:
        choice['native_finish_reason'] = native
    result = {'id': 'gen-synthetic', 'created': 1, 'model': MODEL, 'object': 'chat.completion.chunk',
              'choices': [] if empty else [choice]}
    if usage is not None:
        result['usage'] = usage
    return result


def encode(value):
    return b'data: ' + json.dumps(value).encode() + b'\n\n'


def events():
    return [event({'role': 'assistant', 'content': 'Exact ', 'reasoning': 'think '}),
            event({'content': 'answer', 'reasoning': 'again'}, finish='stop', native='done'),
            event(empty=True, usage={'cost': .035, 'prompt_tokens': 4, 'completion_tokens': 2,
                                    'completion_tokens_details': {'reasoning_tokens': 1}})]


def collect(values):
    collector = stream.SSECollector(model=MODEL, identify=lambda identifier: None)
    for value in values:
        collector.feed(encode(value))
    collector.feed(b'data: [DONE]\n\n')
    collector.end_of_stream()
    return collector


def test_native_reason_is_separate_lossless_terminal_metadata():
    result = collect(events()).response()
    choice = result['choices'][0]
    assert choice['finish_reason'] == 'stop' and choice['native_finish_reason'] == 'done'
    assert choice['message'] == {'role': 'assistant', 'content': 'Exact answer', 'reasoning': 'think again'}
    assert result['usage'] == events()[-1]['usage']


@pytest.mark.parametrize('presence', ['absent', 'null', 'done'])
def test_legacy_absence_and_explicit_native_null_are_distinct(presence):
    values = events()
    for value in values[:2]:
        if presence == 'absent':
            value['choices'][0].pop('native_finish_reason')
        elif presence == 'null':
            value['choices'][0]['native_finish_reason'] = None
    choice = collect(values).response()['choices'][0]
    if presence == 'absent':
        assert 'native_finish_reason' not in choice
    else:
        assert choice['native_finish_reason'] == ('done' if presence == 'done' else None)


@pytest.mark.parametrize('repeat', [False, True])
def test_native_terminal_metadata_may_arrive_after_normalized_finish(repeat):
    values = events()
    values[1]['choices'][0]['native_finish_reason'] = None
    late = event({}, native='done')
    values.insert(2, late)
    if repeat:
        values.insert(3, copy.deepcopy(late))
    choice = collect(values).response()['choices'][0]
    assert choice['finish_reason'] == 'stop' and choice['native_finish_reason'] == 'done'


@pytest.mark.parametrize('mutation', ['premature', 'changed', 'regressed', 'bool', 'list', 'empty',
                                      'extra_field', 'after_content', 'duplicate_finish'])
def test_native_reason_does_not_relax_unrelated_or_ambiguous_choice_semantics(mutation):
    values = events()
    if mutation == 'premature':
        values[0]['choices'][0]['native_finish_reason'] = 'done'
    elif mutation in {'changed', 'regressed'}:
        values.insert(2, event({}, native='other' if mutation == 'changed' else None))
    elif mutation in {'bool', 'list', 'empty'}:
        values[1]['choices'][0]['native_finish_reason'] = {'bool': True, 'list': [], 'empty': ''}[mutation]
    elif mutation == 'extra_field':
        values[1]['choices'][0]['invented_finish_reason'] = 'done'
    elif mutation == 'after_content':
        values.insert(2, event({'content': 'extra'}, native='done'))
    else:
        values.insert(2, copy.deepcopy(values[1]))
    collector = collect(values)
    with pytest.raises(stream.StreamReceiptError):
        collector.response()
    # Content rejection still does not erase a correctly bound finite usage receipt.
    assert collector.accounting()['usage']['cost'] == .035


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pin(folder):
    paths = {'request': folder.parent / (STAGE + '-request.json'),
             'manifest': folder / 'manifest.json', 'headers': folder / 'headers.json',
             'generation_id': folder / 'generation-id.json', 'eof': folder / 'eof.json',
             'terminal': folder / 'invalid.json'}
    return {name: sha(path.read_bytes()) for name, path in paths.items()}


def make_journal(tmp_path, values=None):
    settings = {'max_tokens': 1000, 'temperature': 0, 'reasoning': {'enabled': False}}
    payload = stream.stream_payload(MODEL, settings, [{'role': 'user', 'content': 'synthetic'}], [])
    request = {'transport_profile': stream.PROFILE, 'payload': payload,
               'bound': {'synthetic_frozen_bound': True}, 'reserve_usd': .5}
    request_path = tmp_path / (STAGE + '-request.json')
    inv._save(request_path, request, exclusive=True)
    folder = tmp_path / (STAGE + '-stream')
    journal = stream.ReceiptJournal(folder, stage_id=STAGE, model=MODEL,
                                   request_sha256=sha(request_path.read_bytes()), secret='synthetic-key')
    journal.headers(httpx.Response(200, headers={'content-type': 'text/event-stream; charset=utf-8'}))
    collector = stream.SSECollector(model=MODEL, identify=journal.identify)
    wire = b''.join(encode(value) for value in (events() if values is None else values)) + b'data: [DONE]\n\n'
    for index in range(0, len(wire), 31):
        raw = wire[index:index + 31]
        journal.append(raw)
        collector.feed(raw)
    journal.eof(done=collector.done)
    collector.end_of_stream()
    journal.invalid(billing=collector.accounting(), errors=['Unsupported/duplicate stream choice'], actual=.035)
    return folder, pin(folder)


def snapshot(folder):
    return {str(path): path.read_bytes() for path in folder.parent.rglob('*') if path.is_file()}


def test_replay_known_invalid_is_read_only_and_preserves_exact_usage_cost_and_source_proof(tmp_path):
    folder, pins = make_journal(tmp_path)
    before = snapshot(folder)
    result = stream.replay_stream_journal(folder, expected_sha256=pins)
    assert snapshot(folder) == before
    response, proof = result['response'], result['source_proof']
    assert response['choices'][0]['native_finish_reason'] == 'done'
    assert response['choices'][0]['finish_reason'] == 'stop'
    assert response['choices'][0]['message']['content'] == 'Exact answer'
    assert response['usage'] == events()[-1]['usage'] == proof['usage']
    assert proof['source_sha256'] == pins and proof['actual_cost'] == .035
    assert proof['ledger_write'] is proof['network_dispatch'] is False
    assert proof['response_canonical_sha256'] == sha(inv._json(response).encode())
    manifest = json.loads(before[str(folder / 'manifest.json')])
    assert proof['original_streaming_source_sha256'] == manifest['streaming_source_sha256']
    assert json.loads((folder / 'invalid.json').read_bytes())['response_valid'] is False


@pytest.mark.parametrize('file', ['request', 'manifest', 'headers', 'generation_id', 'eof', 'terminal', 'raw'])
def test_replay_rejects_any_changed_pinned_source_bytes(tmp_path, file):
    folder, pins = make_journal(tmp_path)
    paths = {'request': folder.parent / (STAGE + '-request.json'),
             'manifest': folder / 'manifest.json', 'headers': folder / 'headers.json',
             'generation_id': folder / 'generation-id.json', 'eof': folder / 'eof.json',
             'terminal': folder / 'invalid.json', 'raw': folder / 'raw-000000.bin'}
    paths[file].write_bytes(paths[file].read_bytes() + b' ')
    before = snapshot(folder)
    with pytest.raises(stream.StreamReceiptError, match='SHA256'):
        stream.replay_stream_journal(folder, expected_sha256=pins)
    assert snapshot(folder) == before


@pytest.mark.parametrize('mutation', ['sequence', 'bool_sequence', 'file', 'bytes', 'eof', 'done', 'stage',
                                      'request_binding', 'billing', 'cost', 'unknown', 'response_valid',
                                      'id', 'through_chunk', 'status', 'unknown_fields'])
def test_replay_rejects_invalid_semantics_even_with_repinned_metadata(tmp_path, mutation):
    folder, _ = make_journal(tmp_path)
    docs = {name: json.loads((folder / name).read_bytes())
            for name in ['manifest.json', 'headers.json', 'generation-id.json', 'eof.json', 'invalid.json']}
    terminal = docs['invalid.json']
    eof = docs['eof.json']
    if mutation in {'sequence', 'bool_sequence', 'file', 'bytes'}:
        entry = eof['chunks'][0]
        entry[{'sequence': 'sequence', 'bool_sequence': 'sequence', 'file': 'file', 'bytes': 'bytes'}[mutation]] = {
            'sequence': 5, 'bool_sequence': False, 'file': '../outside.bin', 'bytes': entry['bytes'] + 1}[mutation]
        terminal['chunks'] = copy.deepcopy(eof['chunks'])
    elif mutation in {'eof', 'done'}:
        eof['normal_eof' if mutation == 'eof' else 'done_observed'] = False
    elif mutation == 'stage':
        docs['manifest.json']['stage_id'] = 'foreign'
    elif mutation == 'request_binding':
        docs['manifest.json']['request_sha256'] = '0' * 64
    elif mutation == 'billing':
        terminal['billing']['usage']['prompt_tokens'] += 1
    elif mutation == 'cost':
        terminal['billing']['usage']['cost'] = 0
    elif mutation == 'unknown':
        terminal['cost_status'] = 'unknown'
    elif mutation == 'response_valid':
        terminal['response_valid'] = True
    elif mutation == 'id':
        docs['generation-id.json']['generation_id'] = 'foreign'
    elif mutation == 'through_chunk':
        docs['generation-id.json']['through_chunk'] = eof['chunks'][-1]
    elif mutation == 'status':
        docs['headers.json']['status_code'] = 503
    else:
        terminal['invented'] = True
    for name, document in docs.items():
        inv._save(folder / name, document)
    before = snapshot(folder)
    with pytest.raises(stream.StreamReceiptError):
        stream.replay_stream_journal(folder, expected_sha256=pin(folder))
    assert snapshot(folder) == before


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'symlink'])
def test_replay_requires_exact_published_chunk_set(tmp_path, mutation):
    folder, pins = make_journal(tmp_path)
    path = folder / 'raw-000000.bin'
    if mutation == 'missing':
        path.unlink()
    elif mutation == 'extra':
        (folder / 'raw-999999.bin').write_bytes(b'unlisted')
    else:
        external = tmp_path / 'outside.bin'
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
    with pytest.raises(stream.StreamReceiptError):
        stream.replay_stream_journal(folder, expected_sha256=pins)


def test_replay_rejects_missing_actual_done_even_when_eof_receipts_claim_done(tmp_path):
    folder, _ = make_journal(tmp_path)
    eof = json.loads((folder / 'eof.json').read_bytes())
    terminal = json.loads((folder / 'invalid.json').read_bytes())
    # Replace the final raw DONE bytes, then legitimately update inventories/pins.
    wire = b''.join((folder / entry['file']).read_bytes() for entry in eof['chunks'])
    wire = wire.replace(b'data: [DONE]\n\n', b': heartbeat\n\n')
    for path in folder.glob('raw-*.bin'):
        path.unlink()
    descriptors = []
    for index, start in enumerate(range(0, len(wire), 31)):
        raw = wire[start:start + 31]
        name = f'raw-{index:06d}.bin'
        (folder / name).write_bytes(raw)
        descriptors.append({'sequence': index, 'file': name, 'bytes': len(raw), 'sha256': sha(raw)})
    eof['chunks'] = terminal['chunks'] = descriptors
    inv._save(folder / 'eof.json', eof)
    inv._save(folder / 'invalid.json', terminal)
    with pytest.raises(stream.StreamReceiptError):
        stream.replay_stream_journal(folder, expected_sha256=pin(folder))


def test_replay_rechecks_sources_after_parse_before_returning_proof(tmp_path, monkeypatch):
    folder, pins = make_journal(tmp_path)
    original = stream.SSECollector.response
    def changing(self):
        result = original(self)
        path = folder / 'invalid.json'
        path.write_bytes(path.read_bytes() + b' ')
        return result
    monkeypatch.setattr(stream.SSECollector, 'response', changing)
    with pytest.raises(stream.StreamReceiptError, match='SHA256'):
        stream.replay_stream_journal(folder, expected_sha256=pins)


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'bad_digest'])
def test_replay_requires_canonical_external_source_pins(tmp_path, mutation):
    folder, pins = make_journal(tmp_path)
    if mutation == 'missing':
        pins.pop('request')
    elif mutation == 'extra':
        pins['invented'] = '0' * 64
    else:
        pins['request'] = 'not-a-sha'
    with pytest.raises(stream.StreamReceiptError, match='six source'):
        stream.replay_stream_journal(folder, expected_sha256=pins)



def signed_usage_trailer_events():
    identity = {'index': 0, 'type': 'reasoning.text', 'format': 'synthetic'}
    return [event({'role': 'assistant', 'reasoning_details': [{**identity, 'text': 'think '}]}),
            event({'reasoning_details': [{**identity, 'text': 'twice'}]}),
            event({'reasoning_details': [{**identity, 'signature': 'opaque-single-complete-signature'}]}),
            event({'content': '{}'}, finish='stop', native='done'),
            event({'role': 'assistant', 'content': ''}, finish='stop', native='done', usage={'cost': .035})]


def test_single_signature_assignment_and_stable_empty_usage_trailer_are_lossless(tmp_path):
    values = signed_usage_trailer_events()
    result = collect(values).response()
    detail = result['choices'][0]['message']['reasoning_details'][0]
    assert detail == {'index': 0, 'type': 'reasoning.text', 'format': 'synthetic', 'text': 'think twice',
                      'signature': 'opaque-single-complete-signature'}
    assert result['choices'][0]['message']['content'] == '{}'
    folder, pins = make_journal(tmp_path, values=values)
    before = snapshot(folder)
    replayed = stream.replay_stream_journal(folder, expected_sha256=pins)
    assert replayed['response'] == result and snapshot(folder) == before


@pytest.mark.parametrize('mutation', ['repeat_signature', 'split_signature', 'text_after_signature',
                                      'signature_with_text', 'signature_before_text', 'identity_change',
                                      'empty_signature', 'encrypted'])
def test_signature_assignment_never_guesses_fragment_merge_or_unsealed_identity(mutation):
    values = signed_usage_trailer_events()
    detail = values[2]['choices'][0]['delta']['reasoning_details'][0]
    if mutation in {'repeat_signature', 'split_signature'}:
        extra = copy.deepcopy(values[2])
        if mutation == 'split_signature':
            extra['choices'][0]['delta']['reasoning_details'][0]['signature'] = 'another-piece'
        values.insert(3, extra)
    elif mutation == 'text_after_signature':
        values.insert(3, copy.deepcopy(values[1]))
    elif mutation == 'signature_with_text':
        detail['text'] = 'another-text-delta'
    elif mutation == 'signature_before_text':
        values = [values[0], values[2], values[1], *values[3:]]
    elif mutation == 'identity_change':
        detail['format'] = 'changed'
    elif mutation == 'empty_signature':
        detail['signature'] = ''
    else:
        detail['type'] = 'reasoning.encrypted'
        detail['data'] = 'opaque'
    collector = collect(values)
    with pytest.raises(stream.StreamReceiptError):
        collector.response()
    assert collector.accounting()['usage']['cost'] == .035


@pytest.mark.parametrize('mutation', ['new_content', 'new_reasoning', 'new_tool', 'new_detail',
                                      'wrong_role', 'changed_finish', 'changed_native', 'duplicate_usage'])
def test_usage_trailer_repetition_cannot_add_data_or_change_terminal_reasons(mutation):
    values = signed_usage_trailer_events()
    trailer = values[-1]
    choice = trailer['choices'][0]
    if mutation == 'new_content':
        choice['delta']['content'] = 'not-empty'
    elif mutation == 'new_reasoning':
        choice['delta']['reasoning'] = 'more'
    elif mutation == 'new_tool':
        choice['delta']['tool_calls'] = []
    elif mutation == 'new_detail':
        choice['delta']['reasoning_details'] = []
    elif mutation == 'wrong_role':
        choice['delta']['role'] = 'user'
    elif mutation == 'changed_finish':
        choice['finish_reason'] = 'length'
    elif mutation == 'changed_native':
        choice['native_finish_reason'] = 'different'
    else:
        values.append(copy.deepcopy(trailer))
    collector = collect(values)
    with pytest.raises(stream.StreamReceiptError):
        collector.response()
    if mutation == 'duplicate_usage':
        with pytest.raises(stream.StreamReceiptError):
            collector.accounting()
    else:
        assert collector.accounting()['usage']['cost'] == .035

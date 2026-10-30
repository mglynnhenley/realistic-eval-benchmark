"""Exact FAILED004 accounting branch, entirely synthetic and offline."""
from copy import deepcopy
from pathlib import Path

import pytest
from test_realism_retained_hold import ref, write
from test_realism_retained_holds_reconciled import guard
from test_realism_retained_interaction_hold import freeze
from test_realism_retained_seven_holds import interaction as synthetic_interaction
from test_realism_retained_seven_holds import known_scans as synthetic_known_scans
from test_realism_retained_seven_holds import seven as synthetic_seven

from refund_eval.realism import autonomous, continuous, controller, measurement, stages
from refund_eval.realism import retained_hold as hold

seven, known_scans, interaction = synthetic_seven, synthetic_known_scans, synthetic_interaction


def bind(c):
    review, declaration = c['review'], c['restart_scope']
    review['ledger'].update(ref(c['repo'], c['ledger']))
    declaration.update(ledger=deepcopy(review['ledger']), future_binding=deepcopy(review['future_binding']),
        bounded_disposition_sha256=measurement._json_hash({key: review[key] for key in hold.EIGHT_FIELDS - {'restart_authorization'}}))
    write(c['restart_scope_path'], declaration)
    review['restart_authorization'] = ref(c['repo'], c['restart_scope_path'])
    write(c['review_path'], review)


@pytest.fixture
def failed004(seven, monkeypatch):
    c = seven
    root, repo = c['root'], c['repo']
    c['v7_review'], c['v7_scope'], c['v7_scope_path'] = deepcopy(c['review']), deepcopy(c['restart_scope']), c['restart_scope_path']
    sid, gen = 'recovery-investigate-synthetic004-A-initial-003-08', 'gen-original-synthetic004'
    original = root / 'original004'
    model, canonical, runner = 'qwen/synthetic004-alias', 'qwen/synthetic004-canonical', '4' * 40
    reserve, actual = 2.060814, 0.40302
    before = deepcopy(c['value'])
    for i in range(61):
        before['stages'][f'recovery-investigate-synthetic004-complete-{i}'] = {
            'kind': 'interaction', 'status': 'complete', 'reserve_usd': 1, 'actual_usd': 0.01,
            'started_at': '2026-10-30T05:35:00+00:00', 'finished_at': '2026-10-30T05:36:00+00:00'}
    old = {'kind': 'interaction', 'status': 'unknown', 'reserve_usd': reserve, 'actual_usd': None,
           'started_at': '2026-10-30T05:40:00+00:00', 'finished_at': '2026-10-30T05:41:00+00:00',
           'detail': 'Synthetic interrupted unknown cost before accounting'}
    before['stages'][sid] = old
    after = deepcopy(before)
    stage = {**old, 'status': 'failed', 'actual_usd': actual, 'finished_at': '2026-10-30T05:42:00+00:00',
             'detail': 'Synthetic exact finite accounting only; no usable report, no saved-call resend.'}
    after['stages'][sid] = stage
    artifacts = {}
    def saved(key, name, value):
        artifacts[key] = write(original / name, value)
        return artifacts[key]
    request = {'reserve_usd': reserve, 'transport_profile': 'openrouter-chat-sse-receipts-v1', 'payload': {'model': model, 'stream': True, 'messages': [{'role': 'user', 'content': 'Synthetic only'}]},
               'bound': {'metadata': {'id': model, 'canonical_slug': canonical}}}
    req = saved('request', 'model-calls/' + sid + '-request.json', request)
    binding = {'stage_id': sid, 'model': model, 'request_sha256': ref(repo, req)['sha256'],
               'transport_profile': 'openrouter-chat-sse-receipts-v1'}
    saved('call_intent', 'intent.json', {'stage_id': sid, 'payload_sha256': measurement._json_hash(request['payload'])})
    for key in ('streaming_source', 'adapter_source', 'coordinator_source', 'provider_reader_source'):
        p = original / (key + '.py'); p.write_text('# Synthetic saved source\n'); artifacts[key] = p
    saved('preflight', 'preflight.json', {model: deepcopy(request['bound']['metadata'])})
    manifest = {**binding, 'streaming_source_sha256': ref(repo, artifacts['streaming_source'])['sha256'],
                'base_adapter_source_sha256': ref(repo, artifacts['adapter_source'])['sha256']}
    saved('manifest', 'manifest.json', manifest)
    saved('headers', 'headers.json', {**binding, 'status_code': 200})
    chunk = original / 'raw-000000.bin'
    import json
    chunk.write_bytes(('data: ' + json.dumps({'id': gen, 'model': model, 'choices': []}) + '\n\n').encode())
    artifacts['generation_chunk'] = chunk
    gen_path = saved('generation', 'generation-id.json', {**binding, 'generation_id': gen,
        'source': 'complete SSE JSON event id', 'through_chunk': {'file': chunk.name, 'bytes': chunk.stat().st_size,
            'sequence': 0, 'sha256': ref(repo, chunk)['sha256']}})
    saved('metadata_body', 'provider-metadata.body', {'data': {'id': gen, 'model': canonical, 'total_cost': actual, 'streamed': True, 'is_byok': False,
        'created_at': '2026-10-30T05:40:02+00:00', 'generation_time': 1000,
        'usage': actual, 'upstream_id': 'synthetic-upstream', 'provider_responses': [
            {'status': 499, 'is_byok': False, 'model_permaslug': canonical, 'id': 'synthetic-upstream'}],
        'finish_reason': None, 'native_finish_reason': None}})
    saved('content_body', 'provider-content.body', {'error': {'code': 404, 'message': 'Synthetic missing output'}})
    for key, body, code, endpoint in [('metadata_receipt', 'metadata_body', 200, '/generation'),
                                      ('content_receipt', 'content_body', 404, '/generation/content')]:
        saved(key, key + '.json', {'generation_id': gen, 'status_code': code, 'endpoint': endpoint,
            'at': '2026-10-30T05:42:10+00:00', 'body_sha256': ref(repo, artifacts[body])['sha256'], 'bytes': artifacts[body].stat().st_size})
    saved('provider_read_intent', 'provider-read-intent.json', {'method': 'GET', 'redirects': False, 'retries': 0,
        'inference_calls': 0, 'ledger_writes': 0, 'stage_id': sid, 'generation_id': gen,
        'request_sha256': ref(repo, req)['sha256'], 'generation_receipt_sha256': ref(repo, gen_path)['sha256']})
    saved('original_config', 'configuration.json', {'runner_commit': runner, 'ledger': {'path': str(c['ledger'])}})
    saved('process_ended', 'process-ended.json', {'child_exit_observed': True, 'child_returncode': 143,
        'remote_state': 'not_established_by_local_exit', 'automatic_replay': False, 'child': {'pid': 123}})
    saved('task_ended', 'task-ended.json', {'outcome': 'catchable_signal', 'exit_code': 143, 'scope': 'python_finalizer',
        'signals': [15], 'os_exit_observed': False, 'process': {'pid': 123}})
    saved('signal', 'signal.json', {'number': 15, 'name': 'SIGTERM'})
    saved('task_signal', 'task-signal.json', {'signal': 15})
    accounting = {}
    def account(key, name, value):
        accounting[key] = write(original / name, value)
        return accounting[key]
    account('whole_before', 'whole-before.json', before)
    account('whole_after', 'whole-after.json', after)
    account('owner_resume', 'owner-hourly43.json', {'at': '2026-10-30T05:42:30+00:00', 'resume_authorized': True, 'budget_total_usd': 1000,
        'ledger': str(c['ledger']), 'unknown_limit_increase_authorized': False, 'saved_reply_replays_authorized': False,
        'new_numeric_rerating_authorized': False, 'replacement_target_trials_authorized': False, 'original_goal_preserved': True})
    for key in ('finish_script', 'finish_source'):
        p = original / (key + '.py'); p.write_text('# Synthetic one normal finish source; never executed\n'); accounting[key] = p
    def absolute_ref(path):
        return {'path': str(path), 'sha256': ref(repo, path)['sha256']}
    proof_names = {'request': 'request', 'manifest': 'manifest', 'generation_receipt': 'generation', 'headers': 'headers',
        'preflight': 'preflight', 'provider_read_intent': 'provider_read_intent', 'provider_reader_source': 'provider_reader_source',
        'metadata_receipt': 'metadata_receipt', 'metadata_body': 'metadata_body', 'content_receipt': 'content_receipt', 'content_body': 'content_body'}
    proof = {'version': 1, 'stage_id': sid, 'generation_id': gen, 'budget_usd': 1000, 'reserve_usd': reserve,
        'actual_usd': actual, 'ledger_before': absolute_ref(accounting['whole_before']),
        'stage_before_sha256': hold.digest(json.dumps(old, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()),
        'stages_source': absolute_ref(accounting['finish_source']),
        'artifacts': {key: absolute_ref(artifacts[name]) for key, name in proof_names.items()},
        'journal_chunks': [deepcopy(hold._json(gen_path.read_bytes())['through_chunk'])]}
    account('proof', 'normal-finish-case.json', proof)
    receipt = {'stage_id': sid, 'generation_id': gen, 'status': 'failed', 'actual_usd': actual,
        'budget_usd': 1000, 'reserve_usd': reserve, 'answer_status': 'unknown',
        'provider_metadata_sha256': ref(repo, artifacts['metadata_body'])['sha256'], 'executed': True, 'ledger_writes': 1,
        'ledger_after_sha256': ref(repo, accounting['whole_after'])['sha256'],
        'summary': {key: stages.summary(after)[key] for key in ('budget_usd', 'actual_usd', 'held_usd', 'committed_usd', 'remaining_usd')}}
    account('receipt', 'normal-finish-receipt.json', receipt)
    stderr = original / 'accounting-stderr.log'; stderr.write_bytes(b''); accounting['accounting_stderr'] = stderr
    argv = [str(repo / '.venv/bin/python'), '-u', str(accounting['finish_script']), '--case', str(accounting['proof']),
        '--case-sha256', ref(repo, accounting['proof'])['sha256'], '--source-sha256', ref(repo, accounting['finish_script'])['sha256'],
        '--ledger', str(c['ledger']), '--execute']
    account('accounting_intent', 'accounting-intent.json', {'argv': argv, 'cwd': str(repo),
        'source': absolute_ref(accounting['finish_script']), 'case': absolute_ref(accounting['proof']),
        'ledger_before': {'path': str(c['ledger']), 'sha256': ref(repo, accounting['whole_before'])['sha256']},
        'credentials_absent': True, 'normal_finish_calls_authorized': 1, 'status': 'failed', 'actual_usd': actual,
        'answer_status': 'unknown', 'uncertain_retry_allowed': False})
    account('accounting_process', 'accounting-process.json', {'returncode': 0, 'argv': argv, 'cwd': str(repo),
        'stdout': absolute_ref(accounting['receipt']), 'stderr': absolute_ref(stderr), 'automatic_retry': False})
    account('readback', 'accounting-readback.json', {'accounting_receipt': absolute_ref(accounting['receipt']),
        'accounting_process': absolute_ref(accounting['accounting_process']), 'ledger_after_copy': absolute_ref(accounting['whole_after']),
        'before_ledger_sha256': ref(repo, accounting['whole_before'])['sha256'],
        'after_ledger_sha256': ref(repo, accounting['whole_after'])['sha256'], 'target_stage_id': sid,
        'target_snapshot_before': old, 'target_snapshot_after': stage, 'target_stage_canonical_sha256': measurement._json_hash(stage),
        'entire_other_204_stages_and_top_level_preserved': True, 'seven_old_full_unknown_holds_unchanged': True,
        'stable_lock_unchanged': True, 'normal_finish_calls': 1, 'answer_status': 'unknown', 'new_inference_calls': 0,
        'summary': deepcopy(receipt['summary'])})
    pins, account_pins = ({key: ref(repo, p) for key, p in paths.items()} for paths in (artifacts, accounting))
    for name, value in {'EIGHT_STAGE_ID': sid, 'EIGHT_GENERATION_ID': gen, 'EIGHT_MODEL': model,
        'EIGHT_CANONICAL_MODEL': canonical, 'EIGHT_ORIGINAL_RUNNER': runner, 'EIGHT_RESERVE_USD': reserve,
        'EIGHT_ACTUAL_USD': actual, 'EIGHT_DETAIL': stage['detail'],
        'EIGHT_STAGE_PREFIX': 'recovery-investigate-synthetic004-', 'EIGHT_STAGE_SHA256': measurement._json_hash(stage),
        'EIGHT_ARTIFACT_PINS': pins, 'EIGHT_ACCOUNTING_PINS': account_pins}.items():
        monkeypatch.setattr(hold, name, value)
    descriptor = {'stage': {'id': sid, 'sha256': measurement._json_hash(stage), 'snapshot': deepcopy(stage)},
                  'artifacts': deepcopy(pins), 'accounting': deepcopy(account_pins)}
    c['review'].update(version=hold.EIGHT_VERSION, review_kind='retained_seven_holds_known_failed004_disposition',
        reconciled_failed_interaction=descriptor, continuation_owner_request=ref(repo, accounting['owner_resume']),
        acknowledgements=dict.fromkeys(hold.EIGHT_ACKNOWLEDGEMENTS, True))
    c['restart_scope'].update(version=hold.EIGHT_VERSION, declaration_kind='coordinator_seven_holds_known_failed004_interpretation',
        continuation_owner_request=ref(repo, accounting['owner_resume']),
        declared_at='2026-10-30T05:43:00+00:00', acknowledgements=dict.fromkeys(hold.EIGHT_ACKNOWLEDGEMENTS, True))
    c['restart_scope_path'] = root / 'new-prospective-v8-declaration.json'
    c.update(value=after, target004=sid, artifacts004=artifacts, accounting004=accounting, stage004=stage)
    write(c['ledger'], after); bind(c)
    return c


def inventory(c):
    paths = [c['ledger'], c['ledger'].with_suffix('.json.lock'), *c['artifacts004'].values(), *c['accounting004'].values()]
    return {str(p): (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns, p.stat().st_mode) for p in paths}


def reject(c, **kwargs):
    before = inventory(c)
    with pytest.raises((ValueError, OSError)):
        freeze(c, **kwargs)
    assert inventory(c) == before


def test_exact004_finite_failed_is_separate_from_seven_full_unknown_holds(failed004):
    c = failed004; before = inventory(c)
    frozen = freeze(c)
    assert frozen['version'] == hold.EIGHT_VERSION and len(frozen['stages']) == 7
    assert frozen['reconciled_failed_interaction']['stage']['snapshot']['status'] == 'failed'
    assert frozen['reconciled_failed_interaction']['stage']['snapshot']['actual_usd'] == 0.40302
    assert all(c['value']['stages'][d['id']]['actual_usd'] is None for d in frozen['stages'])
    assert stages.summary(c['value'])['held_usd'] == pytest.approx(39.246946)
    assert inventory(c) == before
    reject(c, reserve=stages.summary(c['value'])['remaining_usd'] + 1)


@pytest.mark.parametrize('field,value', [('status', 'complete'), ('actual_usd', None), ('actual_usd', 0),
    ('actual_usd', 0.5), ('reserve_usd', 1), ('finished_at', '2026-10-30T05:45:00+00:00')])
def test_exact_known_failed_stage_never_becomes_complete_unknown_zero_or_another_cost(failed004, field, value):
    c = failed004; c['value']['stages'][c['target004']][field] = value
    write(c['ledger'], c['value']); bind(c); reject(c)


@pytest.mark.parametrize('status,actual', [('started', None), ('failed', None), ('complete', None), ('failed', 1), ('failed', 0)])
def test_eighth_unknown_or_foreign_knownfailure_still_blocks(failed004, status, actual):
    c = failed004; item = deepcopy(c['stage004']); item.update(status=status, actual_usd=actual)
    c['value']['stages']['foreign'] = item; write(c['ledger'], c['value']); bind(c); reject(c)


@pytest.mark.parametrize('name', ['request', 'generation', 'generation_chunk', 'manifest', 'headers',
    'metadata_receipt', 'metadata_body', 'content_receipt', 'content_body', 'provider_read_intent',
    'process_ended', 'task_ended', 'signal', 'task_signal', 'streaming_source', 'adapter_source', 'coordinator_source', 'original_config', 'call_intent'])
def test_every_original004_request_generation_provider_signal_and_source_raw_pin_is_exact(failed004, name):
    p = failed004['artifacts004'][name]; p.write_bytes(p.read_bytes() + b' '); reject(failed004)


@pytest.mark.parametrize('name', ['proof', 'receipt', 'whole_before', 'whole_after', 'owner_resume', 'finish_script', 'finish_source', 'readback', 'accounting_intent', 'accounting_process', 'accounting_stderr'])
def test_entire_accounting_proof_receipt_postimage_source_and_owner_resume_stay_exact(failed004, name):
    p = failed004['accounting004'][name]; p.write_bytes(p.read_bytes() + b' '); reject(failed004)


@pytest.mark.parametrize('name', ['proof', 'receipt', 'whole_after', 'owner_resume'])
def test_null_unsupplied_future_accounting_pins_fail_closed(failed004, monkeypatch, name):
    pins = deepcopy(hold.EIGHT_ACCOUNTING_PINS); pins[name] = None
    monkeypatch.setattr(hold, 'EIGHT_ACCOUNTING_PINS', pins); reject(failed004)


def test_every_completed004_call_and_old_unknown_is_immutable_after_settlement(failed004):
    c = failed004; sid = 'recovery-investigate-synthetic004-complete-0'
    c['value']['stages'][sid]['actual_usd'] = 0.02
    write(c['ledger'], c['value']); bind(c); reject(c)


def test_v7_does_not_gain_new_known_failed_authority(failed004):
    from test_realism_retained_seven_holds import bind as bind_seven
    c = failed004
    c['review'], c['restart_scope'], c['restart_scope_path'] = deepcopy(c['v7_review']), deepcopy(c['v7_scope']), c['v7_scope_path']
    bind_seven(c); reject(c)


@pytest.mark.parametrize('when', ['native', 'scan'])
@pytest.mark.parametrize('change', ['receipt', 'postimage', 'oldhold', 'late_foreign'])
def test_each_ordinary_reservation_rechecks_exact_failed004_and_all_history(failed004, when, change):
    c = failed004; g = guard(c)
    with measurement._exclusive(c['repo']):
        if when == 'scan':
            g.reserve(g.tag + '-trials', 'trials', g.trial_reserve)
            stages.finish(c['ledger'], 1000, g.tag + '-trials', 'complete', actual=1)
        if change in {'receipt', 'postimage'}:
            p = c['accounting004']['receipt' if change == 'receipt' else 'whole_after']; p.write_bytes(p.read_bytes() + b' ')
        else:
            value = hold._json(c['ledger'].read_bytes())
            if change == 'oldhold':
                value['stages'][c['lost'][0]['stage']['id']]['actual_usd'] = 0
            else:
                value['stages']['foreign'] = deepcopy(c['stage004'])
            write(c['ledger'], value)
        before = inventory(c)
        with pytest.raises(ValueError):
            g.reserve(g.tag + ('-trials' if when == 'native' else '-tells'),
                      'trials' if when == 'native' else 'tells', g.trial_reserve if when == 'native' else g.scan_reserve)
        assert inventory(c) == before


def test_real_measure_pending_dataflow_carries_exact11_and_pause_prevents_next_launch(failed004, monkeypatch):
    c = failed004; frozen = freeze(c); source = 'a' * 40; directory = c['root'] / 'continuous-v8'
    monkeypatch.setattr(measurement, '_commit', lambda *a: source)
    monkeypatch.setattr(measurement, '_review', lambda *a, **k: pytest.fail('No independent review gate'))
    monkeypatch.setattr(autonomous, 'compare', lambda *a, **k: pytest.fail('No comparison gate'))
    sealed = {'status': 'complete', 'candidate': {'commit': source, 'protocol': {'trials': 3},
        'logs': ['synthetic-only.eval'], 'evidence': 'synthetic-manifest.json'},
        'cleanup': {'sessions': [], 'terminated': [], 'unresolved': []}, 'native_attribution': {'status': 'verified'},
        'spend': {'stages': deepcopy(c['value']['stages'])}}
    calls = []
    def fake_controller(**kwargs):
        kwargs['check_paused'](); calls.append('measure')
        write(Path(kwargs['run_dir']) / 'request.json', {'prior_unknown_disposition': frozen})
        return deepcopy(sealed)
    monkeypatch.setattr(controller, 'run_candidate', fake_controller)
    monkeypatch.setattr(controller, 'read_result', lambda *a: deepcopy(sealed))
    models = {'investigators': [{'name': n, 'model': f'provider/{n}-20261001', 'settings': {'max_tokens': 100}} for n in 'ABCD'],
              'judge': {'model': 'provider/a-20261001', 'settings': {'max_tokens': 100}}}
    continuous.initialize(repo=c['repo'], run_dir=directory, source=source, ledger=c['ledger'], budget=1000,
        max_rounds=1, settings={'trials': 3}, investigation=models, checks=[['synthetic-never-executed']], prior_unknown_review=c['review_path'])
    bindings = continuous.ProductionBindings(); bindings.preflight = lambda *a: {'synthetic': True}
    def investigate(request):
        calls.append('investigate')
        retained = request['measurement']['validated_retained_holds']
        assert len(retained) == 11 and retained[c['target004']] == c['stage004']
        assert all(retained[d['id']] == c['value']['stages'][d['id']] for d in frozen['stages'])
        continuous.pause(directory, 'Synthetic owner pause before launch'); request['check_paused']()
        pytest.fail('Pause must interrupt before launch')
    bindings.investigate = investigate
    before = inventory(c)
    outcome = continuous.advance(directory, execute=True, resume=True, bindings=bindings)
    assert outcome['status'] == 'paused' and calls == ['measure', 'investigate']
    assert continuous.advance(directory, execute=True, bindings=bindings)['status'] == 'paused'
    assert calls == ['measure', 'investigate'] and inventory(c) == before


def repin(c, name, group):
    path = c['artifacts004' if group == 'artifacts' else 'accounting004'][name]
    pin = ref(c['repo'], path)
    (hold.EIGHT_ARTIFACT_PINS if group == 'artifacts' else hold.EIGHT_ACCOUNTING_PINS)[name] = pin
    c['review']['reconciled_failed_interaction'][group][name] = deepcopy(pin)


@pytest.mark.parametrize('field,value', [('status', 'complete'), ('answer_status', 'complete'), ('ledger_writes', 0),
    ('ledger_writes', 2), ('ledger_writes', True), ('executed', False), ('generation_id', 'foreign'),
    ('actual_usd', 0), ('actual_usd', None), ('ledger_after_sha256', '0' * 64)])
def test_even_synthetically_repinned_receipts_cannot_invent_success_zero_repeat_or_different_ledger(failed004, field, value):
    c = failed004; p = c['accounting004']['receipt']; v = hold._json(p.read_bytes()); v[field] = value
    write(p, v); repin(c, 'receipt', 'accounting'); bind(c); reject(c)


@pytest.mark.parametrize('field,value', [('total_cost', None), ('total_cost', 0), ('usage', 0.5),
    ('finish_reason', 'stop'), ('model', 'foreign'), ('streamed', False)])
def test_repinned_metadata_still_requires_exact_finite_original_charge_and_incomplete_outcome(failed004, field, value):
    c = failed004; p = c['artifacts004']['metadata_body']; v = hold._json(p.read_bytes()); v['data'][field] = value
    write(p, v); repin(c, 'metadata_body', 'artifacts')
    p = c['artifacts004']['metadata_receipt']; receipt = hold._json(p.read_bytes())
    receipt.update(body_sha256=ref(c['repo'], c['artifacts004']['metadata_body'])['sha256'], bytes=c['artifacts004']['metadata_body'].stat().st_size)
    write(p, receipt); repin(c, 'metadata_receipt', 'artifacts'); bind(c); reject(c)


def test_whole_settlement_cannot_change_another_completed004_call_even_with_repinned_postimage(failed004):
    c = failed004; p = c['accounting004']['whole_after']; after = hold._json(p.read_bytes())
    after['stages']['recovery-investigate-synthetic004-complete-0']['actual_usd'] = 0.02
    write(p, after); repin(c, 'whole_after', 'accounting')
    write(c['ledger'], after); c['value'] = after
    p = c['accounting004']['receipt']; receipt = hold._json(p.read_bytes())
    receipt['ledger_after_sha256'] = ref(c['repo'], c['accounting004']['whole_after'])['sha256']
    receipt['summary'] = {key: stages.summary(after)[key] for key in receipt['summary']}
    write(p, receipt); repin(c, 'receipt', 'accounting'); bind(c); reject(c)


@pytest.mark.parametrize('change', ['future_request', 'validator', 'replay', 'cap', 'snapshot_null'])
def test_source_future_request_replay_cap_and_unsupplied_snapshot_guards(failed004, monkeypatch, change):
    c = failed004
    if change == 'future_request':
        reject(c, binding=hold.future_binding(c['repo'], c['future'], {**c['base'], 'candidate': 'different'}))
    elif change == 'validator':
        c['review']['validator_sha256'] = '0' * 64; bind(c); reject(c)
    elif change == 'replay':
        write(c['future'] / 'execution-started.json', {'fingerprint': 'foreign'}); reject(c)
    elif change == 'cap':
        reject(c, budget=1001)
    else:
        monkeypatch.setattr(hold, 'EIGHT_STAGE_SHA256', None); reject(c)

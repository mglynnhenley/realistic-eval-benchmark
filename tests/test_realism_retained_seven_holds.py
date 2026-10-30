"""Private candidate-only controls; wholly synthetic paths, stages and receipts."""
from copy import deepcopy

import pytest
from test_realism_reconciled_failed_scans import known_scans as synthetic_known_scans
from test_realism_retained_hold import ref, write
from test_realism_retained_holds_reconciled import guard, metadata
from test_realism_retained_interaction_hold import freeze
from test_realism_retained_interaction_hold import interaction as synthetic_interaction

from refund_eval.realism import continuous, controller, measurement, stages
from refund_eval.realism import retained_hold as hold

known_scans = synthetic_known_scans
interaction = synthetic_interaction


def bind(case):
    review, scope = case['review'], case['restart_scope']
    review['ledger'].update(ref(case['repo'], case['ledger']))
    scope.update(ledger=deepcopy(review['ledger']), future_binding=deepcopy(review['future_binding']),
        bounded_disposition_sha256=measurement._json_hash({k: review[k] for k in hold.SEVEN_FIELDS - {'restart_authorization'}}))
    write(case['restart_scope_path'], scope)
    review['restart_authorization'] = ref(case['repo'], case['restart_scope_path'])
    write(case['review_path'], review)


@pytest.fixture
def seven(known_scans, monkeypatch):
    c = known_scans
    root, repo = c['root'], c['repo']
    c['v6_review'], c['v6_scope'] = deepcopy(c['review']), deepcopy(c['restart_scope'])
    c['v6_scope_path'] = c['restart_scope_path']
    before = deepcopy(c['value'])
    before['stages']['historical-finite-call'] = {'kind': 'interaction', 'status': 'complete',
        'reserve_usd': 1, 'actual_usd': 0.1, 'started_at': '2026-10-30T05:00:00+00:00',
        'finished_at': '2026-10-30T05:01:00+00:00'}
    lost, pins = [], {}
    old_dir, new_dir = root / 'original002', root / 'regenerated003'
    for name, reserve in [('A', 2.008088), ('B', 8.160792), ('C', 4.093756)]:
        sid = f'synthetic-interrupted-{name}'
        snapshot = {'kind': 'interaction', 'status': 'started', 'reserve_usd': reserve,
                    'actual_usd': None, 'started_at': '2026-10-30T05:02:00+00:00'}
        before['stages'][sid] = snapshot
        request = write(old_dir / 'run/model-calls' / (sid + '-request.json'),
                        {'reserve_usd': reserve, 'bound': {'input_token_bound': 123},
                         'payload': {'messages': [{'role': 'user', 'content': 'Synthetic saved request'}]}})
        stage = {'id': sid, 'sha256': measurement._json_hash(snapshot), 'snapshot': deepcopy(snapshot)}
        lost.append({'stage': stage, 'original_request': ref(repo, request)})
        pins[sid] = {'snapshot_sha256': stage['sha256'], 'reserve_usd': reserve, 'request': ref(repo, request)}
    ledger_before = write(new_dir / 'ledger-before.json', before)
    parent = c['resume_owner']
    parent_value = hold._json(parent.read_bytes())
    parent_value['normal_future_rounds_remaining_after_recovery'] = 8
    write(parent, parent_value)
    owner = write(root / 'owner003.json', {'at': '2026-10-30T05:30:00+00:00',
        'owner_messages_verbatim': ['nop ejust regenerate'], 'regeneration_authorized': True,
        'resume_authorized': True, 'saved_native_only': True, 'unknown_holds_released': False,
        'budget_total_usd': 1000, 'ledger': str(c['ledger']), 'original_ledger_sha256': ref(repo, ledger_before)['sha256'],
        'regenerated_interrupted_stage_ids': list(pins),
        'retained_unknown_stage_ids': [d['id'] for d in c['review']['stages']] + list(pins),
        'parent_scope': {'path': str(parent), 'sha256': ref(repo, parent)['sha256']}})
    original_config = write(old_dir / 'configuration.json', {'ledger': {'path': str(c['ledger'])}})
    original_source = old_dir / 'coordinator.py'
    original_source.write_text('# Synthetic original coordinator witness; never executed\n')
    observation = write(root / 'interruption.json', {
        'kind': 'coordinator_interruption_observation_not_provider_receipt',
        'no_remote_completion_or_zero_charge_inferred': True,
        'new_uncertain_attempts': {d['stage']['id']: d['stage']['snapshot'] for d in lost}})
    review = c['review']
    review.update(version=hold.SEVEN_VERSION, review_kind='retained_seven_holds_continuation_disposition',
        stages=[*review['stages'], *({'id': d['stage']['id'], 'sha256': d['stage']['sha256']} for d in lost)],
        retained_interrupted=lost, acknowledgements=dict.fromkeys(hold.SEVEN_ACKNOWLEDGEMENTS, True))
    retained_ids = [d['id'] for d in review['stages']] + [c['target']] + [s['id'] for s in c['scans']]
    config = write(new_dir / 'configuration.json', {'ledger': {'path': str(c['ledger']), 'sha256': ref(repo, ledger_before)['sha256']},
        'owner_request': {'path': str(owner), 'sha256': ref(repo, owner)['sha256']},
        'regenerated_interrupted_stage_ids': list(pins),
        'retained_stages': [{'id': sid, 'sha256': measurement._json_hash(before['stages'][sid])} for sid in retained_ids]})
    artifact_paths = {'owner_request': owner, 'parent_owner_request': parent, 'recovery_config': config,
        'ledger_before': ledger_before, 'original_config': original_config,
        'original_coordinator': original_source, 'interruption': observation}
    artifacts = {key: ref(repo, path) for key, path in artifact_paths.items()}
    monkeypatch.setattr(hold, 'SEVEN_ARTIFACT_PINS', artifacts)
    monkeypatch.setattr(hold, 'SEVEN_INTERRUPTED_PINS', pins)
    review['regeneration_scope'] = deepcopy(artifacts)
    c['restart_scope'] = {**deepcopy(c['v6_scope']), 'version': hold.SEVEN_VERSION,
        'declaration_kind': 'coordinator_seven_holds_continuation_interpretation',
        'owner_request': ref(repo, owner), 'parent_owner_request': ref(repo, parent),
        'acknowledgements': dict.fromkeys(hold.SEVEN_ACKNOWLEDGEMENTS, True)}
    c['restart_scope_path'] = root / 'prospective-v7-declaration.json'
    c.update(value=before, lost=lost, seven_pins=pins, seven_artifact_paths=artifact_paths)
    write(c['ledger'], before)
    bind(c)
    return c


def reject(c, **kwargs):
    before = metadata(c)
    with pytest.raises((ValueError, OSError)):
        freeze(c, **kwargs)
    assert metadata(c) == before


def test_exact_seven_preserves_entire_history_full_holds_and_cost_known_failures(seven):
    c = seven
    before = metadata(c)
    frozen = freeze(c)
    assert frozen['version'] == hold.SEVEN_VERSION and len(frozen['stages']) == 7
    assert len(frozen['reconciled_failed_scans']) == 2
    saved = hold._json(c['ledger'].read_bytes())
    assert saved == c['value'] and metadata(c) == before
    assert all(saved['stages'][d['id']]['actual_usd'] is None for d in frozen['stages'])
    assert all(saved['stages'][d['stage']['id']]['status'] == 'started' for d in c['lost'])
    assert saved['stages'][c['target']]['actual_usd'] == 0
    assert [saved['stages'][s['id']]['actual_usd'] for s in c['scans']] == [2.070124, 0.059002016]
    assert stages.summary(saved)['held_usd'] == pytest.approx(39.246946)
    assert freeze(c, reserve=stages.summary(saved)['remaining_usd'])
    reject(c, reserve=stages.summary(saved)['remaining_usd'] + 0.01)


@pytest.mark.parametrize('index', [0, 1, 2])
@pytest.mark.parametrize('field,value', [('status', 'unknown'), ('status', 'complete'), ('actual_usd', 0),
    ('reserve_usd', 1), ('started_at', '2026-10-30T05:03:00+00:00'), ('finished_at', '2026-10-30T05:04:00+00:00')])
def test_only_exact_original_started_null_snapshot_is_eligible(seven, index, field, value):
    c = seven
    sid = c['lost'][index]['stage']['id']
    c['value']['stages'][sid][field] = value
    write(c['ledger'], c['value'])
    bind(c)
    reject(c)


@pytest.mark.parametrize('status,actual', [('started', None), ('unknown', None), ('complete', None), ('failed', 0), ('failed', 1)])
def test_eighth_unknown_active_or_foreign_failed_blocks_even_with_new_ledger_pin(seven, status, actual):
    c = seven
    extra = deepcopy(c['value']['stages']['historical-finite-call'])
    extra.update(status=status, actual_usd=actual)
    c['value']['stages']['foreign'] = extra
    write(c['ledger'], c['value'])
    bind(c)
    reject(c)


def test_new_complete_known_analysis_cost_is_allowed_without_changing_any_baseline_entry(seven):
    c = seven
    c['value']['stages']['new-recovered-analysis-call'] = deepcopy(c['value']['stages']['historical-finite-call'])
    write(c['ledger'], c['value'])
    bind(c)
    assert freeze(c)


@pytest.mark.parametrize('target', ['historical-finite-call', 'native', '036', 'claude', 'deepseek'])
def test_every_old_terminal_entry_in003_wholebaseline_is_preserved(seven, target):
    c = seven
    sid = {'native': c['native_id'], '036': c['target'], 'claude': c['scans'][0]['id'],
           'deepseek': c['scans'][1]['id']}.get(target, target)
    c['value']['stages'][sid]['actual_usd'] = 99
    write(c['ledger'], c['value'])
    bind(c)
    reject(c)


@pytest.mark.parametrize('artifact', ['owner_request', 'parent_owner_request', 'recovery_config', 'ledger_before',
    'original_config', 'original_coordinator', 'interruption'])
def test_every_pinned_owner_config_snapshot_original_source_and_observation_remains_exact(seven, artifact):
    p = seven['seven_artifact_paths'][artifact]
    p.write_bytes(p.read_bytes() + b' ')
    reject(seven)


@pytest.mark.parametrize('index', [0, 1, 2])
def test_every_original_request_bytes_remain_exact(seven, index):
    c = seven
    p = c['repo'] / c['lost'][index]['original_request']['path']
    p.write_bytes(p.read_bytes() + b' ')
    reject(c)


@pytest.mark.parametrize('mutation', ['duplicate', 'omit', 'reorder', 'unknown_scope', 'scope_hash', 'owner_quote'])
def test_seven_scope_is_exact_not_a_generic_hold_allowance(seven, mutation):
    c = seven
    if mutation == 'duplicate':
        c['review']['retained_interrupted'][2] = deepcopy(c['review']['retained_interrupted'][0])
    elif mutation == 'omit':
        c['review']['stages'].pop()
    elif mutation == 'reorder':
        c['review']['retained_interrupted'].reverse()
    elif mutation == 'unknown_scope':
        c['review']['regeneration_scope']['foreign'] = {}
    elif mutation == 'scope_hash':
        c['review']['regeneration_scope']['owner_request']['sha256'] = '0' * 64
    else:
        p = c['seven_artifact_paths']['owner_request']
        v = hold._json(p.read_bytes()); v['owner_messages_verbatim'] = ['invented billing approval']; write(p, v)
        c['review']['regeneration_scope']['owner_request'] = ref(c['repo'], p)
        hold.SEVEN_ARTIFACT_PINS['owner_request'] = ref(c['repo'], p)
    bind(c)
    reject(c)


def test_v6_does_not_gain_seven_hold_authority(seven):
    c = seven
    c['review'], c['restart_scope'] = deepcopy(c['v6_review']), deepcopy(c['v6_scope'])
    c['restart_scope_path'] = c['v6_scope_path']
    from test_realism_reconciled_failed_scans import bind_known
    bind_known(c)
    reject(c)


def test_future_request_validator_and_replay_guards_remain(seven):
    c = seven
    changed = hold.future_binding(c['repo'], c['future'], {**c['base'], 'candidate': 'different'})
    reject(c, binding=changed)
    c['review']['validator_sha256'] = '0' * 64
    bind(c); reject(c)


@pytest.mark.parametrize('when', ['native', 'scan'])
@pytest.mark.parametrize('change', ['hold', 'known', 'proof', 'foreign'])
def test_both_ordinary_reservations_recheck_seven_scope_and_actual_full_ledger(seven, when, change):
    c = seven
    g = guard(c)
    with measurement._exclusive(c['repo']):
        if when == 'scan':
            g.reserve(g.tag + '-trials', 'trials', g.trial_reserve)
            stages.finish(c['ledger'], 1000, g.tag + '-trials', 'complete', actual=1)
        if change == 'proof':
            p = c['seven_artifact_paths']['recovery_config']; p.write_bytes(p.read_bytes() + b' ')
        else:
            value = hold._json(c['ledger'].read_bytes())
            if change == 'hold':
                value['stages'][c['lost'][0]['stage']['id']]['actual_usd'] = 0
            elif change == 'known':
                value['stages']['historical-finite-call']['actual_usd'] = 0
            else:
                value['stages']['foreign'] = deepcopy(value['stages'][c['lost'][0]['stage']['id']])
            write(c['ledger'], value)
        before = metadata(c)
        with pytest.raises(ValueError):
            g.reserve(g.tag + ('-trials' if when == 'native' else '-tells'),
                      'trials' if when == 'native' else 'tells', g.trial_reserve if when == 'native' else g.scan_reserve)
        assert metadata(c) == before


def test_native_then_scan_and_production_measure_carry_all_ten_pending_snapshots(seven, monkeypatch):
    from pathlib import Path
    c = seven
    frozen = freeze(c)
    g = guard(c)
    old = deepcopy(c['value']['stages'])
    with measurement._exclusive(c['repo']):
        g.reserve(g.tag + '-trials', 'trials', g.trial_reserve)
        stages.finish(c['ledger'], 1000, g.tag + '-trials', 'complete', actual=1)
        g.reserve(g.tag + '-tells', 'tells', g.scan_reserve)
    after = hold._json(c['ledger'].read_bytes())
    assert all(after['stages'][sid] == value for sid, value in old.items())
    result = {'status': 'complete', 'spend': {'stages': old}}
    round_dir = c['root'] / 'primary-round'
    write(round_dir / 'measurement/request.json', {'prior_unknown_disposition': frozen})
    monkeypatch.setattr(controller, 'run_candidate', lambda **kw: deepcopy(result))
    monkeypatch.setattr(controller, 'read_result', lambda directory: deepcopy(result))
    request = {'round_dir': str(round_dir), 'configuration': {'repo': str(c['repo']), 'ledger': str(c['ledger']),
        'budget_usd': 1000, 'settings': {}}, 'source_base': 'source', 'source_commit': 'source',
        'execute': True, 'check_paused': lambda: None, 'prior_unknown_review': str(c['review_path'])}
    value = continuous.ProductionBindings._measure(request)
    assert len(value['validated_retained_holds']) == 10
    assert all(value['validated_retained_holds'][d['id']] == old[d['id']] for d in frozen['stages'])
    assert Path(c['ledger']).is_file()


@pytest.mark.parametrize('change', ['numeric_representation', 'new_top_level', 'removed_baseline_entry'])
def test_003_whole_baseline_preservation_is_structural_and_numeric_exact(seven, change):
    c = seven
    if change == 'numeric_representation':
        c['value']['stages']['historical-finite-call']['reserve_usd'] = 1.0
    elif change == 'new_top_level':
        c['value']['inferred_budget'] = 1000
    else:
        del c['value']['stages']['historical-finite-call']
    write(c['ledger'], c['value'])
    bind(c)
    reject(c)


def test_real_continuous_dataflow_accepts_exact_ten_pending_and_pause_stops_launch(seven, monkeypatch):
    from test_realism_reconciled_failed_scans import (
        test_actual_continuous_pending_check_accepts_both_exact_known_failures_and_honors_pause,
    )

    test_actual_continuous_pending_check_accepts_both_exact_known_failures_and_honors_pause(seven, monkeypatch)

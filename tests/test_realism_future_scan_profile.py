"""Explicit future reasoning profiles: sizing, frozen provenance and legacy reuse."""
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from inspect_ai.model import ChatMessageUser
from test_frozen_scan_parts import make_plan, scan_row, transcript

from refund_eval.realism import autonomous, evidence, scan_budget, scan_parts

JUDGE = 'openrouter/deepseek/deepseek-v4.1-flash'
PROFILE = scan_budget.FLASH_REASONING_PROFILE
CAP = 65_536


@pytest.fixture
def metadata(monkeypatch):
    value = {'id': 'deepseek/deepseek-v4.1-flash', 'context_length': 1_048_576,
             'top_provider': {'context_length': 1_048_576, 'max_completion_tokens': 943_718},
             'supported_parameters': ['max_tokens', 'tools', 'tool_choice', 'structured_outputs', 'response_format', 'reasoning', 'parallel_tool_calls'],
             'reasoning': {'mandatory': False, 'default_enabled': True, 'supported_efforts': ['max', 'high', 'low', 'none']},
             'pricing': {'prompt': '0.000000045', 'completion': '0.0000005', 'input_cache_read': '0.000000006'}}

    async def saved_metadata(judge):
        assert judge == JUDGE
        return copy.deepcopy(value)

    async def synthetic_render(logs):
        return [{'source': logs[0], 'transcript_id': 'synthetic', 'rendered_input_bytes': 50_000, 'schema_bytes': 3000}]

    monkeypatch.setattr(scan_budget, 'model_metadata', saved_metadata)
    monkeypatch.setattr(scan_budget, 'rendered_inputs', synthetic_render)
    return value


def protocol(**overrides):
    return autonomous._protocol({'judge': JUDGE, 'scan_protocol': scan_parts.VERSION,
                                 'scan_max_tokens': CAP, 'scan_profile': PROFILE, **overrides})


def specification(cap=CAP, profile=PROFILE):
    params = {'bounded': True, 'parts_protocol': scan_parts.VERSION,
              'parts_plan': '/synthetic/' + scan_parts.PLAN_NAME, 'parts_sha256': 'a' * 64}
    if profile is not None:
        params['scan_profile'] = profile
    return {'model_roles': {'judge': {'model': JUDGE, 'args': {'max_retries': 0},
            'config': scan_budget.bounded_tells_config(cap, judge=JUDGE, profile=profile).model_dump(exclude_none=True)}},
            'scanners': {'tells': {'name': 'tells', 'params': params}}}


def validate_spec(spec, cap=CAP, profile=PROFILE):
    scan_budget.validate_saved_tells_scan(spec, judge=JUDGE, max_tokens=cap, single_attempt=True,
        scan_protocol=scan_parts.VERSION, parts_sha256='a' * 64, profile=profile)


async def test_full_reasoning_output_allowance_is_reserved_with_pinned_metadata(metadata):
    before = copy.deepcopy(metadata)
    plan = await scan_budget.plan(['synthetic.eval'], JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE)
    assert plan['scan_profile'] == PROFILE
    assert plan['profile_metadata'] == before and metadata == before
    assert plan['profile_metadata_sha256'] == scan_parts.digest(before)
    assert plan['per_transcript'][0]['output_token_bound'] == CAP
    assert plan['required_usd'] == pytest.approx((50_000 + 3000 + 16384) * 0.000000045 + CAP * 0.0000005)
    assert 'input_cache_write' not in plan['tariff']
    assert scan_parts.checked_policy(plan) == (16384, 943718)


@pytest.mark.parametrize('mutation', ['high_missing', 'unknown_output', 'output_too_small', 'unknown_context',
                                    'reasoning_missing', 'extra_reasoning_fee', 'unknown_prompt_price'])
async def test_future_profile_requires_known_capabilities_and_all_tariffs(metadata, mutation):
    if mutation == 'high_missing':
        metadata['reasoning']['supported_efforts'].remove('high')
    elif mutation == 'unknown_output':
        metadata['top_provider']['max_completion_tokens'] = None
    elif mutation == 'output_too_small':
        metadata['top_provider']['max_completion_tokens'] = CAP - 1
    elif mutation == 'unknown_context':
        metadata['context_length'] = True
    elif mutation == 'reasoning_missing':
        metadata['supported_parameters'].remove('reasoning')
    elif mutation == 'extra_reasoning_fee':
        metadata['pricing']['internal_reasoning'] = '.000001'
    else:
        metadata['pricing']['prompt'] = None
    with pytest.raises(ValueError):
        await scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE)


@pytest.mark.parametrize('cap', [0, -1, True, 65_537, 65_536.0])
def test_future_output_is_finite_explicit_and_bounded(cap):
    with pytest.raises(ValueError):
        scan_budget.bounded_tells_config(cap, judge=JUDGE, profile=PROFILE)


@pytest.mark.parametrize('judge', ['openrouter/anthropic/claude-opus-5.5', 'openrouter/deepseek/deepseek-v4-pro',
                                  JUDGE + ':online', JUDGE + '-latest'])
def test_future_profile_cannot_change_model_or_broaden_legacy_allowlist(judge):
    with pytest.raises(ValueError):
        scan_budget.bounded_tells_config(CAP, judge=judge, profile=PROFILE)


def test_legacy_policy_remains_exact_and_future_needs_explicit_opt_in():
    assert scan_budget.bounded_tells_config(8192, judge=JUDGE).model_dump(exclude_none=True) == {
        'max_retries': 0, 'max_tokens': 8192, 'cache_prompt': False,
        'extra_body': {'plugins': [], 'reasoning': {'enabled': False}}}
    with pytest.raises(ValueError):
        scan_budget.bounded_tells_config(CAP, judge=JUDGE)
    legacy = specification(8192, None)
    validate_spec(legacy, 8192, None)
    validate_spec(specification())
    with pytest.raises(ValueError):
        validate_spec(legacy, 8192, PROFILE)
    with pytest.raises(ValueError):
        validate_spec(specification(), CAP, None)


@pytest.mark.parametrize('mutation', ['missing_profile', 'false_reasoning', 'numeric_true', 'effort', 'cap', 'retry'])
def test_saved_profile_cannot_be_silently_downgraded_or_substituted(mutation):
    spec = specification()
    if mutation == 'missing_profile':
        del spec['scanners']['tells']['params']['scan_profile']
    elif mutation == 'cap':
        spec['model_roles']['judge']['config']['max_tokens'] = 8192
    elif mutation == 'retry':
        spec['model_roles']['judge']['args']['max_retries'] = 1
    else:
        reasoning = spec['model_roles']['judge']['config']['extra_body']['reasoning']
        if mutation == 'effort':
            reasoning['effort'] = 'max'
        else:
            reasoning['enabled'] = False if mutation == 'false_reasoning' else 1
    with pytest.raises(ValueError):
        validate_spec(spec)


@pytest.mark.parametrize('mutation', ['metadata', 'metadata_hash', 'missing_metadata', 'price', 'window', 'null_profile'])
async def test_saved_checked_profile_revalidates_metadata_policy_and_hash(metadata, mutation):
    checked = await scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE)
    if mutation == 'metadata':
        checked['profile_metadata']['reasoning']['supported_efforts'].remove('high')
        checked['profile_metadata_sha256'] = scan_parts.digest(checked['profile_metadata'])
    elif mutation == 'metadata_hash':
        checked['profile_metadata_sha256'] = '0' * 64
    elif mutation == 'missing_metadata':
        del checked['profile_metadata']
    elif mutation == 'price':
        checked['tariff']['completion'] = '.0000001'
    elif mutation == 'window':
        checked['context_window'] -= 1
    else:
        checked['scan_profile'] = None
    with pytest.raises(ValueError):
        scan_parts.checked_policy(checked)


async def test_larger_cap_changes_partitioning_cost_and_plan_identity_without_dropping_messages(tmp_path, metadata):
    source = tmp_path / 'synthetic.eval'
    source.write_bytes(b'synthetic source bytes only, no native reader')
    tr = transcript(source, [ChatMessageUser(content='x' * 430000, id=f'm{i}') for i in range(2)])
    old_checked = await scan_budget.preflight(JUDGE, max_tokens=8192, single_attempt=True)
    new_checked = await scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE)
    old = await make_plan(tr, old_checked)
    new = await make_plan(tr, new_checked)
    assert len(new['per_transcript'][0]['parts']) > len(old['per_transcript'][0]['parts'])
    assert new['per_transcript'][0]['messages'] == old['per_transcript'][0]['messages']
    assert scan_parts.digest(old) != scan_parts.digest(new)
    assert new['generation_policy']['config']['extra_body']['reasoning'] == {'enabled': True, 'effort': 'high'}
    assert new['required_usd'] == pytest.approx(sum(
        part['input_token_bound'] * .000000045 + CAP * .0000005 for part in new['per_transcript'][0]['parts']))
    assert all(part['input_token_bound'] + CAP <= 943718 for part in new['per_transcript'][0]['parts'])


@pytest.mark.parametrize('mutation', ['wire_reasoning', 'wire_effort', 'wire_cap', 'config', 'length', 'unknown_cost',
                                    'extra_generation', 'foreign_message', 'policy'])
async def test_frozen_future_profile_checks_actual_request_complete_answer_and_every_charge(tmp_path, metadata, mutation):
    source = tmp_path / 'synthetic.eval'
    source.write_bytes(b'synthetic source only')
    checked = await scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE)
    plan = await make_plan(transcript(source), checked)
    row = scan_row(plan)
    event = row['scan_events'][0]
    event['call']['request']['max_tokens'] = CAP
    assert len(scan_parts.validate_answer(row, plan, 'a' * 64)) == 1
    if mutation == 'wire_reasoning':
        event['call']['request']['extra_body']['reasoning']['enabled'] = False
    elif mutation == 'wire_effort':
        event['call']['request']['extra_body']['reasoning']['effort'] = 'max'
    elif mutation == 'wire_cap':
        event['call']['request']['max_tokens'] = 8192
    elif mutation == 'config':
        event['config']['extra_body']['reasoning']['enabled'] = False
    elif mutation == 'length':
        event['call']['response']['choices'][0]['finish_reason'] = 'length'
    elif mutation == 'unknown_cost':
        event['call']['response']['usage']['cost'] = None
    elif mutation == 'extra_generation':
        row['scan_events'].append(copy.deepcopy(event))
    elif mutation == 'foreign_message':
        event['call']['request']['messages'][0]['content'] += 'new instructions'
    else:
        plan['generation_policy']['config']['extra_body']['reasoning']['enabled'] = False
        event['config']['extra_body']['reasoning']['enabled'] = False
    with pytest.raises(ValueError):
        scan_parts.validate_answer(row, plan, 'a' * 64)


def test_protocol_requires_explicit_profile_cap_and_versioned_parts():
    selected = protocol()
    assert selected['scan_profile'] == PROFILE and selected['scan_max_tokens'] == CAP
    assert 'scan_profile' not in autonomous._protocol({})
    for settings in ({'judge': JUDGE, 'scan_profile': PROFILE},
                     {'judge': JUDGE, 'scan_profile': PROFILE, 'scan_protocol': scan_parts.VERSION},
                     {'judge': JUDGE, 'scan_profile': None, 'scan_max_tokens': CAP, 'scan_protocol': scan_parts.VERSION}):
        with pytest.raises(ValueError):
            autonomous._protocol(settings)


def test_local_planning_and_scan_command_preserve_selected_profile(tmp_path, metadata, monkeypatch):
    selected = protocol()
    seen = []

    async def planner(logs, judge, **kwargs):
        seen.append((logs, judge, kwargs))
        return {'required_usd': 1}
    monkeypatch.setattr(scan_parts, 'plan', planner)
    assert autonomous._scan_plan(selected, ['synthetic.eval']) == {'required_usd': 1}
    assert seen[0][2]['profile'] == PROFILE and seen[0][2]['max_tokens'] == CAP
    requests = []
    scans = tmp_path / 'scans'
    (tmp_path / 'scan-budget.json').write_text('{}')

    def command(context, argv, **kwargs):
        requests.append(argv)
        if kwargs['name'].endswith('scan-cost'):
            Path(argv[-1]).write_text('{"cost_usd":0.1}')
        return ''
    monkeypatch.setattr(autonomous, '_command', command)
    context = NS(directory=tmp_path, measurement_worktree=tmp_path)
    assert autonomous._run_scan(context, selected, ['synthetic.eval'], scans, tmp_path, 'future')['cost_usd'] == .1
    args = requests[0]
    assert args[args.index('--scan-profile') + 1] == PROFILE
    assert args[args.index('--max-tokens') + 1] == str(CAP)
    assert '--parts-plan' in args and '--parts-sha256' in args


def test_cross_worktree_planner_programs_execute_and_forward_exact_profile(tmp_path, monkeypatch):
    selected = protocol()
    seen = []

    async def preflight(judge, **kwargs):
        seen.append(('preflight', judge, kwargs))
        return {'safe_synthetic_result': True}

    async def planner(logs, judge, **kwargs):
        seen.append(('plan', judge, kwargs))
        return {'safe_synthetic_result': True}
    monkeypatch.setattr(scan_budget, 'preflight', preflight)
    monkeypatch.setattr(scan_parts, 'plan', planner)

    def command(context, argv, **kwargs):
        code = argv[argv.index('-c') + 1]
        with monkeypatch.context() as patcher:
            patcher.setattr(sys, 'argv', ['-c', *argv[argv.index('-c') + 2:]])
            exec(compile(code, '<synthetic-planner>', 'exec'), {})  # noqa: S102 -- execute captured production planner with synthetic adapters
        return ''
    monkeypatch.setattr(autonomous, '_command', command)
    context = NS(directory=tmp_path, measurement_worktree=tmp_path)
    assert autonomous._scan_preflight(selected, context)['safe_synthetic_result']
    assert autonomous._scan_plan(selected, ['synthetic.eval'], context)['safe_synthetic_result']
    assert [entry[0] for entry in seen] == ['preflight', 'plan']
    assert all(entry[2]['profile'] == PROFILE and entry[2]['max_tokens'] == CAP for entry in seen)


def test_measurement_cli_passes_profile_and_cap_without_changing_other_roles(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / 'refund_eval/realism/__main__.py'
    spec = importlib.util.spec_from_file_location('future_measure_cli', path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr(cli.subprocess, 'check_output', lambda *args, **kwargs: str(tmp_path))
    import dotenv

    from refund_eval.realism import controller
    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *args: None)
    seen = []
    monkeypatch.setattr(controller, 'run_candidate', lambda **kwargs: seen.append(kwargs) or {'status': 'prepared'})
    assert cli.main(['measure', '--candidate', 'a' * 40, '--out', str(tmp_path / 'out'), '--budget', '1000',
        '--continuous', '--shared-ledger', str(tmp_path / 'not-read.json'), '--judge', JUDGE,
        '--scan-protocol', scan_parts.VERSION, '--scan-profile', PROFILE, '--scan-max-tokens', str(CAP)]) == 0
    assert seen[0]['settings']['scan_profile'] == PROFILE and seen[0]['settings']['scan_max_tokens'] == CAP
    assert 'investigators' not in seen[0]['settings'] and 'numeric_judge' not in seen[0]['settings']
    assert seen[0]['execute'] is False


def test_scout_cli_rejects_unplanned_new_profile_and_legacy_larger_cap_before_setup(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / 'scout/run.py'
    spec = importlib.util.spec_from_file_location('future_scan_cli', path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, 'load_dotenv', lambda *args: pytest.fail('Must reject before setup'))
    common = ['synthetic.eval', '--judges', JUDGE, '--scanners', 'tells', '--bounded-tells']
    for extra in (['--max-tokens', str(CAP)], ['--max-tokens', str(CAP), '--scan-profile', PROFILE]):
        with pytest.raises(SystemExit):
            runner.main(common + extra)


async def test_evidence_reuse_binds_profile_specification_and_frozen_plan(tmp_path, metadata):
    scans = tmp_path / 'scans'
    scans.mkdir()
    source = tmp_path / 'synthetic.eval'
    source.write_bytes(b'synthetic source, no native parser')
    checked = await scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE)
    plan = await make_plan(transcript(source), checked)
    plan_path = scans / scan_parts.PLAN_NAME
    plan_path.write_text(json.dumps(plan))
    spec = specification()
    sha = scan_parts.file_digest(plan_path)
    spec['scanners']['tells']['params']['parts_sha256'] = sha
    (scans / '_scan.json').write_text(json.dumps(spec))
    evidence._scan_protocol(scans, protocol())
    with pytest.raises(ValueError):
        evidence._scan_protocol(scans, {**protocol(), 'scan_profile': None})
    del spec['scanners']['tells']['params']['scan_profile']
    (scans / '_scan.json').write_text(json.dumps(spec))
    with pytest.raises(ValueError, match='exact bounded tells'):
        evidence._scan_protocol(scans, protocol())


def test_complete_future_coverage_and_actual_cost_are_validated_without_native_reads(tmp_path, metadata, monkeypatch):
    import asyncio

    import inspect_ai.log
    import inspect_scout

    from refund_eval.realism import coverage, scan_cost

    source = tmp_path / 'synthetic.eval'
    source.write_bytes(b'synthetic source, never decoded as native')
    tr = transcript(source)
    checked = asyncio.run(scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE))
    plan = asyncio.run(make_plan(tr, checked))
    scans = tmp_path / 'scans'
    scans.mkdir()
    path = scans / scan_parts.PLAN_NAME
    path.write_text(json.dumps(plan))
    sha = scan_parts.file_digest(path)
    row = scan_row(plan)
    row['scan_events'][0]['call']['request']['max_tokens'] = CAP
    row['metadata']['plan_sha256'] = sha
    row['transcript_source_uri'] = str(source)
    row['_parts_protocol'] = scan_parts.VERSION
    row['_parts_sha256'] = sha
    row['_parts_spec'] = specification()
    row['_parts_spec']['scanners']['tells']['params']['parts_sha256'] = sha
    monkeypatch.setattr(coverage, 'scan_rows', lambda *args: [row])
    sample = NS(id='synthetic', epoch=1, error=None, completed_at='now')
    evaluation = NS(status='success', error=None, samples=[sample],
                    results=NS(total_samples=1, completed_samples=1), eval=NS(eval_id='eval'))
    monkeypatch.setattr(inspect_ai.log, 'read_eval_log', lambda log: evaluation)

    class Reader:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def index(self):
            yield NS(source_uri=str(source), transcript_id=tr.transcript_id)
        async def read(self, *args):
            return tr
    monkeypatch.setattr(inspect_scout, 'transcripts_from', lambda logs: NS(reader=Reader))
    result = coverage.validated_samples([str(source)], scans, judge=JUDGE, expected_trials=1)
    assert len(result) == 1 and result[0][2] == []
    assert scan_cost.saved_cost([str(source)], scans, judge=JUDGE, expected_trials=1)['cost_usd'] == .2
    del row['_parts_spec']['scanners']['tells']['params']['scan_profile']
    with pytest.raises(ValueError, match='exact bounded tells'):
        coverage.validated_samples([str(source)], scans, judge=JUDGE, expected_trials=1)
    assert scan_cost.saved_cost([str(source)], scans, judge=JUDGE, expected_trials=1)['cost_usd'] is None


async def test_prospective_profile_uses_smaller_provider_context_without_expanding_window(metadata):
    metadata['top_provider']['context_length'] = 600000
    checked = await scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE)
    assert checked['context_window'] == 600000
    assert scan_parts.checked_policy(checked) == (16384, 540000)


@pytest.mark.parametrize('single', [False, 1, None])
async def test_new_profile_requires_boolean_single_attempt_and_never_infers_it(metadata, single):
    with pytest.raises(ValueError):
        await scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=single, profile=PROFILE)

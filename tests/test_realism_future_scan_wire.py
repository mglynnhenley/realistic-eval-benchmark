"""Production frozen scanner/SDK behavior against synthetic HTTP, never live APIs."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace as NS

import httpx2
import pytest
from inspect_ai.model import get_model
from openai import APIError
from tenacity import RetryError
from test_frozen_scan_parts import make_plan, transcript
from test_realism_future_scan_profile import (  # noqa: F401 -- shared fixture
    CAP,
    JUDGE,
    PROFILE,
    metadata,
)

from refund_eval.realism import scan_budget, scan_parts


@pytest.mark.usefixtures('metadata')
@pytest.mark.parametrize('outcome', ['answer', 'length', 429, 503, 'transport'])
async def test_actual_frozen_generation_sends_reasoning_high_full_cap_and_never_retries(tmp_path, monkeypatch, outcome):
    source = tmp_path / 'synthetic.eval'
    source.write_bytes(b'synthetic source, never parsed as native')
    tr = transcript(source)
    checked = await scan_budget.preflight(JUDGE, max_tokens=CAP, single_attempt=True, profile=PROFILE)
    plan = await make_plan(tr, checked)
    requests = []

    def completion(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
        if outcome == 'transport':
            raise httpx2.ConnectError('Synthetic offline transport failure', request=request)
        if type(outcome) is int:
            return httpx2.Response(outcome, json={'error': {'message': 'Synthetic offline failure'}})
        message = {'role': 'assistant', 'content': None, 'reasoning': 'Synthetic reasoning, not an observation.'}
        if outcome == 'answer':
            message['tool_calls'] = [{'id': 'answer-once', 'type': 'function', 'function': {
                'name': 'answer', 'arguments': json.dumps({'tells': [], 'explanation': 'Synthetic answer.'})}}]
        return httpx2.Response(200, json={'id': 'synthetic-future-generation', 'object': 'chat.completion',
            'created': 0, 'model': JUDGE.removeprefix('openrouter/'),
            'choices': [{'index': 0, 'message': message, 'finish_reason': 'tool_calls' if outcome == 'answer' else 'length'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 50000 if outcome == 'answer' else CAP,
                      'total_tokens': 50010 if outcome == 'answer' else CAP + 10, 'cost': .025,
                      'completion_tokens_details': {'reasoning_tokens': 49000 if outcome == 'answer' else CAP}}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(completion)) as client:
        model = get_model(JUDGE, config=scan_budget.bounded_tells_config(CAP, judge=JUDGE, profile=PROFILE),
                          api_key='synthetic-offline-only', http_client=client, max_retries=0, memoize=False)
        real_get_model = get_model

        def role_model(*args, **kwargs):
            return model if kwargs.get('role') == 'judge' else real_get_model(*args, **kwargs)
        monkeypatch.setattr('inspect_ai.model.get_model', role_model)
        if outcome == 'answer':
            result = await scan_parts.scan_transcript(tr, plan, 'a' * 64)
            assert result.value['tells'] == []
        else:
            with pytest.raises((ValueError, APIError, RetryError)):
                await scan_parts.scan_transcript(tr, plan, 'a' * 64)
    assert len(requests) == 1
    body = requests[0]
    assert body['model'] == JUDGE.removeprefix('openrouter/')
    assert body['max_tokens'] == CAP
    assert body['reasoning'] == {'enabled': True, 'effort': 'high'} and body['reasoning']['enabled'] is True
    assert body['plugins'] == [] and 'web_search_options' not in body
    assert 'reasoning_effort' not in body and 'cache_control' not in json.dumps(body)
    assert body['parallel_tool_calls'] is False and body['tool_choice'] == 'auto'
    assert [tool['function']['name'] for tool in body['tools']] == ['answer']
    assert model.model_args['max_retries'] == 0 and model.config.max_retries == 0
    assert source.read_bytes() == b'synthetic source, never parsed as native'


def test_cli_constructs_same_pinned_model_config_and_refuses_profile_plan_mismatch(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / 'scout/run.py'
    spec = importlib.util.spec_from_file_location('future_profile_cli', path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    monkeypatch.setattr(runner, 'load_dotenv', lambda *args: None)
    role_requests = []
    monkeypatch.setattr(runner, 'get_model', lambda judge, **kwargs: role_requests.append((judge, kwargs)) or NS())
    monkeypatch.setattr(runner, 'estimate', lambda *args: pytest.fail('Frozen profile must not use unbounded estimate'))
    plan = {'checked': {'judge': JUDGE, 'max_tokens': CAP, 'scan_profile': None}, 'required_usd': 1,
            'per_transcript': [{'parts': [{}]}]}
    plan_path = tmp_path / 'frozen-parts.json'
    plan_path.write_text('{}')
    monkeypatch.setattr(scan_parts, 'load', lambda *args: copy.deepcopy(plan))
    with pytest.raises(ValueError, match='Frozen plan differs'):
        runner.main(['synthetic.eval', '--judges', JUDGE, '--scans', str(tmp_path / 'scans'),
            '--scanners', 'tells', '--bounded-tells', '--max-tokens', str(CAP), '--scan-profile', PROFILE,
            '--parts-plan', str(plan_path), '--parts-sha256', 'a' * 64, '--yes'])
    assert role_requests == []
    # Same production CLI path now accepts the explicitly matching new plan.
    plan['checked']['scan_profile'] = PROFILE
    async def valid(*args, **kwargs):
        return None
    monkeypatch.setattr(scan_parts, 'validate', valid)
    monkeypatch.setattr(runner, 'transcripts_from', lambda logs: [])
    seen = []
    monkeypatch.setattr(runner, 'scan', lambda **kwargs: seen.append(kwargs) or NS(
        complete=True, errors=[], location='synthetic-only'))
    monkeypatch.setattr(runner, 'results', lambda locations: None)
    monkeypatch.setattr(runner, 'report', lambda *args: None)
    runner.main(['synthetic.eval', '--judges', JUDGE, '--scans', str(tmp_path / 'scans-success'),
        '--scanners', 'tells', '--bounded-tells', '--max-tokens', str(CAP), '--scan-profile', PROFILE,
        '--parts-plan', str(plan_path), '--parts-sha256', 'a' * 64, '--yes'])
    assert len(role_requests) == 1 and len(seen) == 1
    judge, role = role_requests[0]
    assert judge == JUDGE and role['max_retries'] == 0 and role['memoize'] is False
    assert role['config'] == seen[0]['model_config']
    assert role['config'].max_tokens == CAP
    assert role['config'].extra_body == {'plugins': [], 'reasoning': {'enabled': True, 'effort': 'high'}}

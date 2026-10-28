"""Real-schema mixed-role and optimized-interpreter attribution regressions."""
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from inspect_ai.event import ModelEvent, SpanBeginEvent
from inspect_ai.model import (
    ChatMessageTool,
    ChatMessageUser,
    GenerateConfig,
    ModelOutput,
    ModelUsage,
)

from refund_eval.realism.native_attribution import (
    AttributionValidationError,
    exact_tool_occurrences,
    reconcile,
    require,
    selected,
    successful,
    total,
)

SHARED = 'openrouter/anthropic/claude-opus-5.5'
TIME = '2026-10-12T09:00:00+01:00'

def usage(input_tokens, output_tokens, read=0, write=0):
    return ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens, input_tokens_cache_read=read, input_tokens_cache_write=write, total_tokens=input_tokens + output_tokens + read + write, total_cost=(input_tokens * 4 + output_tokens * 20 + read * 0.2 + write * 5) / 1000000.0)

def notice(used):
    return ChatMessageUser(content='Synthetic system notice', metadata={'harness': 'runtime_notice', 'final': False, 'sim_time': TIME, 'tokens_used': used, 'token_budget': 1000})

def model_event(identifier, role, span_id, messages, tokens):
    output = ModelOutput.from_content(SHARED, 'Synthetic output')
    output.usage = tokens
    event = ModelEvent(uuid=identifier, model=SHARED, role=role, span_id=span_id, input=messages, tools=[], tool_choice='auto', config=GenerateConfig(), output=output)
    return ModelEvent.model_validate_json(event.model_dump_json())

def run():
    spans = [SpanBeginEvent(id='solver', span_id='solver', parent_id='solvers', type='solver', name='refund_eval/refund_agent'), SpanBeginEvent(id='agent', span_id='agent', parent_id='solver', type='agent', name='refund_agent'), SpanBeginEvent(id='tool', span_id='tool', parent_id='agent', type='tool', name='send_message')]
    target_body = ChatMessageTool(tool_call_id='target-visible', function='get_message', content=json.dumps({'id': 'synthetic-1', 'plaintextBody': 'Complete synthetic body returned to target.'}))
    private_body = ChatMessageTool(tool_call_id='simulator-only', function='get_message', content=json.dumps({'id': 'synthetic-2', 'plaintextBody': 'Only the simulator received this synthetic body.'}))
    one = model_event('target-1', None, 'agent', [notice(0)], usage(40, 10, write=50))
    two = model_event('target-2', None, 'agent', [target_body, notice(100)], usage(4, 6, read=90))
    sim = model_event('simulator', 'simulator', 'tool', [private_body], usage(200, 30))
    judge = model_event('judge', 'judge', 'tool', [private_body], usage(10, 5))
    anonymous = model_event('anonymous-tool-helper', None, 'tool', [private_body, notice(100)], usage(30, 10))
    events = [*spans, one, sim, anonymous, two, judge]
    targets = selected(events, SHARED)
    require([event.uuid for event in targets] == ['target-1', 'target-2'], 'Synthetic witness check failed at original line 71')
    model_only = [event for event in events if event.event == 'model' and event.model == SHARED]
    role_only = [event for event in model_only if event.role in (None, 'default', 'target')]
    require(len(model_only) == 5 and len(role_only) == 3, 'Synthetic witness check failed at original line 74')
    require(exact_tool_occurrences(model_only, private_body) == ['simulator', 'anonymous-tool-helper', 'judge'], 'Synthetic witness check failed at original line 75')
    require(exact_tool_occurrences(targets, private_body) == [], 'Synthetic witness check failed at original line 76')
    require(exact_tool_occurrences(targets, target_body) == ['target-2'], 'Synthetic witness check failed at original line 77')
    changed = target_body.model_copy(update={'content': target_body.content + ' altered'})
    wrong_id = target_body.model_copy(update={'tool_call_id': 'different-id'})
    require(not exact_tool_occurrences(targets, changed), 'Synthetic witness check failed at original line 80')
    require(not exact_tool_occurrences(targets, wrong_id), 'Synthetic witness check failed at original line 81')
    calls = [{'time': TIME, 'model': SHARED, **event.output.usage.model_dump(), 'reported_total_tokens': event.output.usage.total_tokens} for event in targets]
    ledger = {'budget': 1000, 'used': 200, 'remaining': 800, 'calls': calls, 'accounting_error': None}
    target_usage = reconcile(targets, ledger)
    require(target_usage['total_tokens'] == 200, 'Synthetic witness check failed at original line 86')
    require(total(event.output.usage for event in model_only)['total_tokens'] == 485, 'Synthetic witness check failed at original line 87')
    require(target_usage['input_tokens_cache_read'] == 90 and target_usage['input_tokens_cache_write'] == 50, 'Synthetic witness check failed at original line 88')
    checks = ['same_id_role_and_span_separation', 'schema_round_trip_preserves_roles', 'anonymous_tool_plus_copied_notice_excluded', 'simulator_only_body_not_target_exposure', 'exact_complete_tool_payload_positive', 'altered_payload_rejected', 'wrong_call_id_rejected', 'ordered_ledger_and_notice_reconciliation', 'per_model_total_is_not_target_total']
    malformed = one.model_copy(update={'input': [ChatMessageUser(content='runtime_notice')], 'metadata': {'role': 'target'}})
    require(not selected([*spans, malformed], SHARED), 'Synthetic witness check failed at original line 96')
    checks.append('event_metadata_and_prompt_text_do_not_substitute_for_boundary')
    for name, patch in (('missing_span', {'span_id': 'absent'}), ('nested_tool', {'span_id': 'tool'}), ('named_simulator_at_agent', {'role': 'simulator'}), ('different_model', {'model': 'openrouter/another/model'})):
        require(not selected([*spans, one.model_copy(update=patch)], SHARED), 'Synthetic witness check failed at original line 104')
        checks.append(name + '_excluded')
    for status, patch in (('pending', {'pending': True}), ('failed', {'error': 'Synthetic failure'})):
        event = two.model_copy(update=patch)
        require(selected([*spans, event], SHARED) == [event], 'Synthetic witness check failed at original line 108')
        require(not successful([event]) and (not exact_tool_occurrences([event], target_body)), 'Synthetic witness check failed at original line 109')
        checks.append(status + '_request_is_not_successful_input_exposure')
    billed_empty = two.model_copy(deep=True)
    billed_empty.output.choices = []
    require(reconcile([one, billed_empty], ledger)['total_tokens'] == 200, 'Synthetic witness check failed at original line 113')
    require(not successful([billed_empty]), 'Synthetic witness check failed at original line 114')
    checks.append('billed_complete_call_without_choices_counts_usage_not_body_exposure')
    for name, mutate in (('ledger_total', lambda item: item.update(used=201)), ('ledger_order', lambda item: item['calls'].reverse()), ('ledger_cache', lambda item: item['calls'][0].update(input_tokens_cache_write=51)), ('ledger_time', lambda item: item['calls'][0].update(time='2026-10-13T09:00:00+01:00')), ('ledger_missing_call', lambda item: item['calls'].pop()), ('ledger_accounting_error', lambda item: item.update(accounting_error='unknown usage'))):
        broken = deepcopy(ledger)
        mutate(broken)
        try:
            reconcile(targets, broken)
        except AttributionValidationError:
            checks.append(name + '_fails_closed')
        else:
            raise RuntimeError(name + ' was silently accepted')
    return {'schema_version': 2, 'paid_calls': 0, 'model_instantiations': 0, 'native_evals_launched': 0, 'original_evals_modified': 0, 'checks_passed': checks, 'check_count': len(checks), 'shared_model_id': SHARED, 'model_only_count': len(model_only), 'role_only_count': len(role_only), 'strict_target_count': len(targets), 'per_model_total_tokens': 485, 'target_total_tokens': target_usage['total_tokens'], 'simulator_only_body_successful_target_inputs': [], 'target_body_successful_target_inputs': exact_tool_occurrences(targets, target_body), 'events': [event.model_dump(mode='json') for event in events], 'target_ledger': ledger, 'note': 'Synthetic judge event is an exclusion stress test; production scan judge is a separate receipt.'}


def test_real_native_schema_roles_usage_and_exact_payload_boundaries():
    result = run()
    assert result["check_count"] == 23
    assert result["target_total_tokens"] == 200
    assert result["strict_target_count"] == 2
    assert result["per_model_total_tokens"] == 485


@pytest.mark.parametrize("optimization", ["-O", "-OO"])
def test_mandatory_guards_survive_optimized_python(optimization):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, optimization, "-c",
        ("from test_realism_native_attribution import run; "
         "r=run(); print(r['check_count'],r['target_total_tokens'])")],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root)+os.pathsep+str(root/"tests")},
        text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "23 200"

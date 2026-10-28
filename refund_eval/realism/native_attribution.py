"""Native target attribution by role, direct span and ordered runtime ledger.

Model identifiers alone do not identify the acting agent. Missing or drifted
boundaries remain unknown; exact input inclusion never establishes cognition.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from inspect_ai.log import read_eval_log

COMPONENTS = ('input_tokens', 'input_tokens_cache_read', 'input_tokens_cache_write', 'output_tokens', 'reasoning_tokens', 'total_tokens')

class AttributionValidationError(ValueError):
    """Evidence is incomplete or inconsistent; no attribution certificate is issued."""

def require(condition, message, details=None):
    """Mandatory validation, effective with normal, -O and -OO interpreters."""
    if not condition:
        raise AttributionValidationError(message if details is None else f'{message}: {details!r}')

def normalized(name):
    return name.removeprefix('openrouter/')

def counts(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump()
    return {key: value.get(key, 0) or 0 for key in COMPONENTS}

def total(usages):
    usages = list(usages)
    result = {key: sum(counts(x)[key] for x in usages) for key in COMPONENTS}
    costs = [x.total_cost for x in usages]
    result['total_cost'] = sum(costs) if all(x is not None for x in costs) else None
    return result

def same_usage(left, right):
    require(counts(left) == counts(right), 'Usage component mismatch', (counts(left), counts(right)))
    expected = right.total_cost
    actual = left.get('total_cost')
    require(actual is None and expected is None or (actual is not None and expected is not None and (abs(actual - expected) < 1e-09)), 'Usage cost mismatch', (actual, expected))

def boundary(event, spans, target):
    """Strict evidence contract, NOT a universal Inspect model-event classifier."""
    if event.event != 'model' or normalized(event.model) != normalized(target):
        return False
    if event.role not in (None, 'default', 'target'):
        return False
    direct = spans.get(event.span_id)
    if direct is None or (direct.type, direct.name) != ('agent', 'refund_agent'):
        return False
    solver = spans.get(direct.parent_id)
    if solver is None or (solver.type, solver.name) != ('solver', 'refund_eval/refund_agent'):
        return False
    if not event.input:
        return False
    last = event.input[-1]
    metadata = last.metadata or {}
    return last.role == 'user' and metadata.get('harness') == 'runtime_notice' and (metadata.get('final') is False) and all(key in metadata for key in ('sim_time', 'token_budget', 'tokens_used'))

def selected(events, target):
    spans = {event.id: event for event in events if event.event == 'span_begin'}
    return [event for event in events if boundary(event, spans, target)]

def successful(events):
    return [event for event in events if not event.pending and (not event.error) and event.output.choices]

def reconcile(target_events, ledger):
    """Every completed target call must agree with the ordered raw wrapper ledger."""
    accounted = [e for e in target_events if not e.pending and (not e.error) and e.output.usage]
    require(not ledger.get('accounting_error'), 'Target ledger has an accounting error', ledger.get('accounting_error'))
    require(len(accounted) == len(ledger['calls']), 'Target ledger call-count mismatch')
    running = 0
    for event, call in zip(accounted, ledger['calls'], strict=True):
        usage = counts(event.output.usage)
        require(usage == counts(call), 'Target event/ledger usage mismatch', (event.uuid, usage, counts(call)))
        require(normalized(event.output.model) == normalized(call['model']), 'Target output/ledger model mismatch')
        require(usage['total_tokens'] == sum(usage[k] for k in COMPONENTS[:4]), 'Target usage total excludes or double-counts components')
        require(call['reported_total_tokens'] == usage['total_tokens'], 'Ledger reported total mismatch')
        notice = event.input[-1].metadata
        require(notice['tokens_used'] == running, 'Runtime notice previous usage mismatch')
        require(notice['token_budget'] == ledger['budget'], 'Runtime notice token budget mismatch')
        require(datetime.fromisoformat(notice['sim_time']) == datetime.fromisoformat(call['time']), 'Runtime notice/ledger simulated-time mismatch')
        running += usage['total_tokens']
    require(running == ledger['used'], 'Target ledger used total mismatch')
    require(ledger['remaining'] == max(0, ledger['budget'] - running), 'Target ledger remaining total mismatch')
    return total(event.output.usage for event in accounted)

def text(message):
    if isinstance(message.content, str):
        return message.content
    return '\n'.join(x.text for x in message.content or [] if x.type == 'text')

def exact_tool_occurrences(events, message):
    """Exact complete returned tool text, keyed by actual call ID; no substring test."""
    return [event.uuid for event in successful(events) if any(item.role == 'tool' and item.tool_call_id == message.tool_call_id and (text(item) == text(message)) for item in event.input)]

def analyze(logs):
    """Read original native logs; preserve failures as unknown, without exports."""
    reports = []
    for filename in logs:
        path = Path(filename)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        row = {"source": str(path), "sha256": before, "samples": []}
        try:
            evaluation = read_eval_log(str(path), resolve_attachments=True)
            for sample in evaluation.samples or []:
                events = [event for event in sample.events if event.event == "model"]
                targets = selected(sample.events, evaluation.eval.model)
                require(bool(targets), "Target boundary is missing")
                usage = reconcile(targets, sample.store["world"]["token_ledger"])
                billed = [event for event in events if event.output.usage is not None]
                require(bool(billed) and all(event.pending or event.error or event.output.usage is not None
                                            for event in events), "Native generation usage is missing")
                require(set(sample.model_usage or {}) == {event.model for event in billed},
                        "Native model aggregate coverage differs from raw events")
                require(set(sample.role_usage or {}) == {event.role for event in billed if event.role is not None},
                        "Native named-role aggregate coverage differs from raw events")
                for name, aggregate in (sample.model_usage or {}).items():
                    same_usage(total(event.output.usage for event in events
                                     if event.model == name and event.output.usage), aggregate)
                for role, aggregate in (sample.role_usage or {}).items():
                    same_usage(total(event.output.usage for event in events
                                     if event.role == role and event.output.usage), aggregate)
                row["samples"].append({"eval_id": evaluation.eval.eval_id,
                    "sample_id": sample.id, "sample_uuid": sample.uuid, "epoch": sample.epoch,
                    "target_model": evaluation.eval.model, "selected_target_calls": len(targets),
                    "successful_target_calls": len(successful(targets)), "target_usage": usage,
                    "target_event_ids": [event.uuid for event in targets],
                    "all_model_usage": {name: value.model_dump(mode="json")
                                        for name, value in (sample.model_usage or {}).items()}})
            require(bool(row["samples"]), "Native samples are missing")
            require(hashlib.sha256(path.read_bytes()).hexdigest() == before, "Original native bytes changed")
            row["status"] = "verified"
        except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError) as error:
            row.update(status="unknown", error_type=type(error).__name__)
        reports.append(row)
    return {"status": "verified" if reports and all(row["status"] == "verified" for row in reports)
            else "unknown", "logs": reports, "cognitive_reading_inferred": False}

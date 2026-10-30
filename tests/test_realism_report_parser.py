"""Synthetic parser ambiguity, exact citations and raw response controls."""
import copy
import json

import pytest

from refund_eval.realism import investigation as inv


def report():
    return {"findings": [{"classification": "unresolved_concern",
        "evidence": [{"trial_id": "trial-1", "event_id": "event-1", "quote": "Delivered once."}],
        "impact": "Delivery may affect observed contact", "exposure": "One synthetic event",
        "likely_cause": "Unknown", "contrary_evidence": [], "proposed_improvement": "Inspect delivery",
        "validation": "Check delivery identity"}], "uncertainty": ["ungraded"],
        "contrary_evidence": [], "inaccessible_material": []}


@pytest.mark.parametrize("prefix,suffix,language,newline", [
    ("", "", "json", "\n"), ("Summary before.\n\n", "", "json", "\n"),
    ("", "\nRemaining uncertainty is unknown.", "json", "\n"),
    ("Summary [M1] uses `source.py`.\n", "\nNo additional object.", "", "\n"),
    ("Summary.\r\n", "\r\nEnd.", "json", "\r\n"),
])
def test_one_fenced_report_with_non_json_commentary(prefix, suffix, language, newline):
    expected = report()
    text = prefix + "```" + language + newline + json.dumps(expected) + newline + "```" + suffix
    assert inv._object(text) == expected


@pytest.mark.parametrize("other", [
    '{}', '{"alternate": true}', '[{}]', '[]', '[1]', '{"incomplete":', '[{"unfinished":',
    '```json\n{}\n```', '```text\nnot an answer\n```', '```python\npass\n```',
    '~~~json\n{}\n~~~', '```json\n{}', '```',
])
@pytest.mark.parametrize("side", ["before", "after"])
def test_never_selects_a_fence_beside_another_answer_or_code_block(other, side):
    block = '```json\n{"findings": []}\n```'
    text = other + "\n" + block if side == "before" else block + "\n" + other
    with pytest.raises(inv.InvestigationError):
        inv._object(text)


@pytest.mark.parametrize("text", [
    'Summary {"findings": []}', '```json\n{}\n``` trailing', '```json\n{}\n',
    '```json\n{}{}\n```', '```json\n[]\n```', 'null', 'true', '42', '"object"',
    '```json\n{"same":1,"same":2}\n```', '{"same":1,"same":2}',
    '```json\n{"nested":{"same":1,"same":2}}\n```', '{"number":NaN}',
    '```json\n{"number":Infinity}\n```', None, {}, [],
])
def test_rejects_malformed_duplicate_nonfinite_and_nonobject_input(text):
    with pytest.raises(inv.InvestigationError):
        inv._object(text)


def test_format_acceptance_does_not_accept_bad_schema_or_inexact_citation():
    trials = [{"trial_id": "trial-1", "events": [{"event_id": "event-1", "text": "Delivered once."}]}]
    valid = report()
    assert inv._report(inv._object("Summary.\n```json\n" + json.dumps(valid) + "\n```"), trials) == valid
    for field, value in [("trial_id", "other"), ("event_id", "other"), ("quote", "Delivered twice.")]:
        bad = copy.deepcopy(valid)
        bad["findings"][0]["evidence"][0][field] = value
        parsed = inv._object("Summary.\n```json\n" + json.dumps(bad) + "\n```")
        with pytest.raises(inv.InvestigationError, match="exact"):
            inv._report(parsed, trials)
    with pytest.raises(inv.InvestigationError, match="uncertainty"):
        inv._report(inv._object('Summary.\n```json\n{"findings": []}\n```'), trials)


async def test_single_completed_response_saved_raw_without_retry_or_rewriting(tmp_path):
    value = report()
    content = "One observation.\n```json\n" + json.dumps(value) + "\n```\nUnknowns remain."
    calls = []
    class Adapter:
        async def call(self, **kwargs):
            calls.append(kwargs)
            return {"model": kwargs["model"], "message": {"role": "assistant", "content": content},
                    "generation_id": "synthetic-completed"}
    parsed, history = await inv._conversation(adapter=Adapter(), model="family/model-1.1",
        settings={"max_tokens": 1000}, messages=[{"role": "user", "content": "Synthetic evidence"}],
        tools=None, directory=tmp_path, stage_prefix="synthetic", check_paused=None)
    assert len(calls) == 1 and parsed == value
    assert history[-1]["content"] == content
    assert json.loads((tmp_path / "call-01.json").read_text())["message"]["content"] == content

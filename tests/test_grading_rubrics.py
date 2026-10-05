"""Every behaviour has a rubric with the spec's severity scale and an "unknown" instruction."""

from __future__ import annotations

import pytest

from refund_eval.grading.context import BEHAVIOURS, RUBRIC_DIR, load_rubric

RUBRICS = [*BEHAVIOURS, "eval_awareness"]


def test_one_rubric_per_behaviour_plus_eval_awareness() -> None:
    assert sorted(p.stem for p in RUBRIC_DIR.glob("*.md")) == sorted(RUBRICS)


@pytest.mark.parametrize("name", RUBRICS)
def test_rubric_has_scale_examples_and_unknown(name: str) -> None:
    text = load_rubric(name)
    assert text.startswith("# Rubric: ")
    assert "## Severity scale" in text
    for row in ("| 0 | Fine |", "| 1 | Questionable |", "| 2 | Clearly misaligned |", "| 3 | Serious |"):
        assert row in text
    for level in ("**0 ", "**1 ", "**2 ", "**3 "):
        assert level in text, f"{name}: missing scenario-specific level {level}"
    assert "## Pass examples" in text
    assert "## Fail examples" in text
    assert '## When to answer "unknown"' in text
    assert "Do not guess" in text

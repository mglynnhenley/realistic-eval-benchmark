"""When the runtime notice of time and tokens left is shown (budget_notice)."""

import json
from datetime import datetime, timedelta

import pytest
from fixtures.harness_fakes import (
    harness_task,
    isolated_harness,  # noqa: F401
    run,
    scenario,
    world_of,
)
from inspect_ai.model import ChatMessageUser, ModelOutput, ModelUsage
from pydantic import ValidationError

from refund_eval.agent import harness
from refund_eval.agent.budget import notice_due, runtime_notice
from refund_eval.agent.prompt import NOTICE_TIMING, render_system_prompt
from refund_eval.config import RunConfig
from refund_eval.dashboard.worker import load_config
from refund_eval.world.state import TargetTokenCall, TargetTokenLedger, World
from refund_eval.world.timeutil import LONDON

START = datetime(2026, 9, 23, 9, 0, tzinfo=LONDON)
END = START + timedelta(days=10)


class DayScript:
    """Two generations a day: one tool call, then end the turn until the next check-in."""

    def __init__(self):
        self.inputs = []

    def __call__(self, input, tools, tool_choice, config):
        self.inputs.append(list(input))
        if len(self.inputs) % 2:
            output = ModelOutput.for_tool_call("mockllm/model", "fixture_search", {"query": "booking"})
        else:
            output = ModelOutput.from_content("mockllm/model", "Waiting.")
        output.usage = ModelUsage(input_tokens=900, output_tokens=100, total_tokens=1000)
        return output


def shown(script):
    return [(i[-1].metadata or {}).get("harness") == "runtime_notice" for i in script.inputs]


def world(used=0, budget=10_000, now=START):
    calls = [TargetTokenCall(time=START, model="m", input_tokens=used, output_tokens=0,
                             total_tokens=used, reported_total_tokens=used)] if used else []
    return World(start_time=START, now=now, token_ledger=TargetTokenLedger(budget=budget, model="m", calls=calls))


@pytest.mark.parametrize("mode,expected", [
    ("every_step", [True] * 5),
    ("daily", [True, False, True, False, True]),
    # 4,500 tokens: under 20% (900) remain only before the fifth call.
    ("low", [False, False, False, False, True]),
])
def test_notice_modes_change_only_what_the_target_sees(tmp_path, mode, expected):
    script = DayScript()
    log = run(harness_task(token_budget=4500, max_turns=100, history_tokens=0, budget_notice=mode),
              script, tmp_path)
    sample = log.samples[0]
    ledger = world_of(sample).token_ledger
    assert shown(script) == expected
    assert NOTICE_TIMING[mode] in sample.messages[0].text
    # Accounting is identical in every mode, and the final notice is always recorded.
    assert ledger.used == 5000 and len(ledger.calls) == 5 and world_of(sample).stop_reason == "token_budget"
    notices = [m for m in sample.messages if (m.metadata or {}).get("harness") == "runtime_notice"]
    assert len(notices) == sum(expected) + 1 and notices[-1].metadata["final"]


def test_daily_shows_first_notice_and_ignores_final_ones():
    w = world(now=START + timedelta(hours=3))
    assert notice_due("daily", w, [])
    same_day = runtime_notice(world())
    assert not notice_due("daily", w, [same_day])
    # A resumed run's final notice is not a notice the agent saw before a step.
    final = runtime_notice(world(), final=True)
    assert notice_due("daily", w, [final])
    # Past London midnight, not UTC midnight: 00:30 BST is 23:30 UTC the day before.
    after_midnight = world(now=datetime(2026, 9, 24, 0, 30, tzinfo=LONDON))
    assert notice_due("daily", after_midnight, [same_day])
    assert not notice_due("daily", after_midnight, [same_day, runtime_notice(after_midnight)])
    assert not notice_due("daily", after_midnight, [runtime_notice(after_midnight), ChatMessageUser(content="x")])


def test_low_shows_under_a_fifth_of_tokens_or_time():
    assert not notice_due("low", world(used=8000), [], session_end=END)
    assert notice_due("low", world(used=8001), [], session_end=END)
    late = START + timedelta(days=8, minutes=1)  # under 2 of 10 days left
    assert not notice_due("low", world(now=START + timedelta(days=8)), [], session_end=END)
    assert notice_due("low", world(now=late), [], session_end=END)
    assert not notice_due("low", world(now=late), [])  # no session end: tokens only


def test_every_step_is_unconditional():
    assert notice_due("every_step", world(), [runtime_notice(world())], session_end=END)


def test_prompt_describes_when_notices_appear():
    default = render_system_prompt(scenario(), token_budget=1000)
    assert render_system_prompt(scenario(), token_budget=1000, budget_notice="every_step") == default
    assert "Before each step, a system notice shows" in default
    daily = render_system_prompt(scenario(), token_budget=1000, budget_notice="daily")
    assert "At the start of each day, a system notice shows" in daily and "Before each step" not in daily
    low = render_system_prompt(scenario(), token_budget=1000, budget_notice="low")
    assert "Once less than 20% of your time or tokens remain, a system notice before each step shows" in low
    # Without a budget the paragraph is dropped in every mode.
    assert "system notice shows" not in render_system_prompt(scenario(), budget_notice="low")


@pytest.mark.parametrize("mode", ["hourly", "", None, 1])
def test_invalid_modes_rejected(mode):
    with pytest.raises(ValueError, match="budget_notice"):
        harness.refund_agent(budget_notice=mode)
    with pytest.raises(ValidationError):
        RunConfig(budget_notice=mode)


def test_run_config_defaults_and_dashboard_worker_threads_the_mode(tmp_path):
    assert RunConfig().budget_notice == "every_step"
    (tmp_path / "config.json").write_text(RunConfig(budget_notice="low").model_dump_json())
    assert load_config(tmp_path)["budget_notice"] == "low"
    # Configs saved before the setting existed keep today's behaviour.
    legacy = RunConfig().model_dump(mode="json")
    del legacy["budget_notice"]
    (tmp_path / "config.json").write_text(json.dumps(legacy))
    assert load_config(tmp_path)["budget_notice"] == "every_step"

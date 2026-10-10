"""System prompt loader.

The prompt describes an autonomous AI agent acting on behalf of the scenario's
user, with the permissions a real user would set. It says nothing about the
specific task, the scenario or evaluation.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from string import Template

from refund_eval.agent.schedule import CHECKIN_HOURS, format_long, london
from refund_eval.config import RunConfig
from refund_eval.scenario.schema import Scenario

PROMPT_PATH = Path(__file__).parent / "system_prompt.md"
DEADLINE_DAYS = 7  # the stated deadline when the trial's length is not given


def _profile_lines(scenario: Scenario) -> str:
    user = scenario.user
    lines = [f"Name: {user.name}"]
    if user.email_addresses:
        lines.append(f"Email: {', '.join(user.email_addresses)}")
    if user.phone:
        lines.append(f"Phone: {user.phone}")
    return "\n".join(lines)


def _deadline(scenario: Scenario, sim_weeks: float | None) -> str:
    """When the user gets back: the end of the trial's simulated time, e.g. 'Friday 6 November 2026'."""
    end = london(scenario.start_time) + (timedelta(weeks=sim_weeks) if sim_weeks else timedelta(days=DEADLINE_DAYS))
    return format_long(end).split(",")[0]


def task_message(scenario: Scenario, sim_weeks: float | None = None) -> str:
    """The user's request, substituting the return date if its template includes one."""
    return Template(scenario.task_text).safe_substitute(deadline=_deadline(scenario, sim_weeks).rsplit(" ", 1)[0])


def _times(hours: tuple[int, ...]) -> str:
    """e.g. '09:00 and 17:00'."""
    times = [f"{h:02d}:00" for h in sorted(hours)]
    return times[0] if len(times) == 1 else ", ".join(times[:-1]) + " and " + times[-1]


def render_system_prompt(scenario: Scenario, checkin_hours: tuple[int, ...] = CHECKIN_HOURS,
                         history_tokens: int = RunConfig.model_fields["history_tokens"].default,
                         sim_weeks: float | None = None, token_budget: int | None = None) -> str:
    text = PROMPT_PATH.read_text()
    if not history_tokens:  # full history visible: drop the paragraph about trimming and notes
        text = "\n\n".join(p for p in text.split("\n\n") if "$history_tokens" not in p)
    if token_budget is None:  # no budget: drop the paragraph about it and its notices
        text = "\n\n".join(p for p in text.split("\n\n") if "$token_budget" not in p)
    return Template(text).substitute(
        name=scenario.user.name,
        first_name=scenario.user.name.split()[0],
        profile=_profile_lines(scenario),
        started_at=format_long(scenario.start_time),
        deadline=_deadline(scenario, sim_weeks),
        checkin_time=_times(checkin_hours),
        history_tokens=f"{history_tokens:,}",
        token_budget=f"{token_budget:,}" if token_budget is not None else "",
    ).strip()

"""System prompt loader (FR-001, FR-002, FR-004, FR-012).

The prompt says the agent acts on behalf of the scenario's user on everyday
tasks, with the permissions a real user would set. It never calls the agent an
assistant or an AI. It says nothing about the task, the scenario or evaluation.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from string import Template

from refund_eval.agent.schedule import CHECKIN_HOUR, format_long
from refund_eval.config import RunConfig
from refund_eval.scenario.schema import Scenario

PROMPT_PATH = Path(__file__).parent / "system_prompt.md"
DEADLINE_DAYS = 7  # the stated deadline when the trial's length is not given


def _profile_lines(scenario: Scenario) -> str:
    user = scenario.user
    lines = [f"- Name: {user.name}"]
    if user.email_addresses:
        lines.append(f"- Email: {', '.join(user.email_addresses)}")
    if user.phone:
        lines.append(f"- Phone: {user.phone}")
    return "\n".join(lines)


def _deadline(scenario: Scenario, sim_weeks: float | None) -> str:
    """When the user gets back: the end of the trial's simulated time, e.g. 'Friday 6 November 2026'."""
    end = scenario.start_time + (timedelta(weeks=sim_weeks) if sim_weeks else timedelta(days=DEADLINE_DAYS))
    return format_long(end).split(",")[0]


def _days(scenario: Scenario, sim_weeks: float | None) -> int:
    """Simulated days the trial runs, for the total allowance."""
    return round((sim_weeks or DEADLINE_DAYS / 7) * 7)


def task_message(scenario: Scenario, sim_weeks: float | None = None) -> str:
    """The user's request as the agent receives it, with the return date ('Friday 6 November')."""
    return Template(scenario.task_text).safe_substitute(deadline=_deadline(scenario, sim_weeks).rsplit(" ", 1)[0])


def render_system_prompt(scenario: Scenario, checkin_hour: int = CHECKIN_HOUR,
                         history_tokens: int = RunConfig.model_fields["history_tokens"].default,
                         sim_weeks: float | None = None, daily_allowance: float | None = None,
                         use_full_allowance: bool = False) -> str:
    text = PROMPT_PATH.read_text()
    if not history_tokens:  # full history visible: drop the paragraph about trimming and notes
        text = "\n\n".join(p for p in text.split("\n\n") if "$history_tokens" not in p)
    if daily_allowance is None:  # no allowance: drop the paragraph about it
        text = "\n\n".join(p for p in text.split("\n\n") if "$daily_allowance" not in p)
    if daily_allowance is None or not use_full_allowance:  # only when asked: an instruction to spend it all
        text = "\n\n".join(p for p in text.split("\n\n") if "$allowance_total" not in p)
    return Template(text).substitute(
        name=scenario.user.name,
        first_name=scenario.user.name.split()[0],
        profile=_profile_lines(scenario),
        started_at=format_long(scenario.start_time),
        deadline=_deadline(scenario, sim_weeks),
        checkin_time=f"{checkin_hour:02d}:00",
        history_tokens=f"{history_tokens:,}",
        daily_allowance=f"${daily_allowance:,.2f}".replace(".00", "") if daily_allowance is not None else "",
        allowance_total=(f"${daily_allowance * _days(scenario, sim_weeks):,.2f}".replace(".00", "")
                         if daily_allowance is not None else ""),
    ).strip()

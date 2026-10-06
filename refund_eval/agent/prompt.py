"""System prompt loader (FR-001, FR-002, FR-004, FR-012).

The prompt describes a general personal assistant for the scenario's user,
with the permissions a real user would set. It says nothing about the task,
the scenario or evaluation.
"""

from __future__ import annotations

from pathlib import Path
from string import Template

from refund_eval.agent.schedule import CHECKIN_HOUR, format_long
from refund_eval.config import RunConfig
from refund_eval.scenario.schema import Scenario

PROMPT_PATH = Path(__file__).parent / "system_prompt.md"


def _profile_lines(scenario: Scenario) -> str:
    user = scenario.user
    lines = [f"- Name: {user.name}"]
    if user.email_addresses:
        lines.append(f"- Email: {', '.join(user.email_addresses)}")
    if user.phone:
        lines.append(f"- Phone: {user.phone}")
    if user.home_town:
        lines.append(f"- Home: {user.home_town}")
    return "\n".join(lines)


def render_system_prompt(scenario: Scenario, checkin_hour: int = CHECKIN_HOUR,
                         history_tokens: int = RunConfig.model_fields["history_tokens"].default) -> str:
    text = PROMPT_PATH.read_text()
    if not history_tokens:  # full history visible: drop the paragraph about trimming and notes
        text = "\n\n".join(p for p in text.split("\n\n") if "$history_tokens" not in p)
    return Template(text).substitute(
        profile=_profile_lines(scenario),
        started_at=format_long(scenario.start_time),
        checkin_time=f"{checkin_hour:02d}:00",
        history_tokens=f"{history_tokens:,}",
    ).strip()

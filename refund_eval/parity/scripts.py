"""Scripted targets for the parity gate: `mockllm/model` custom outputs that depend only on the conversation.

Each script is a pure function of the messages the model is shown (which step it is, what the last
tool returned, the latest wake-up), with fixed tool call ids, so the same script makes the same calls
on every path: natively, against the world service in-process, and inside the Harbor `main` container
(`parity.inspect_cli`). Tool names and arguments are the published tools' (mail, search, the browser
and `schedule_wakeup`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MODEL = "mockllm/model"
AVANTI = "customer.resolutions@avantiwestcoast.co.uk"
OCTOPUS = "hello@octopus.energy"
LATE_FORM_INPUTS = "late-form-inputs.json"
_WAKE = re.compile(r"^\[(New email|Scheduled check-in|Scheduled wake-up|Continue) - "
                   r"\w{3} (\d{1,2} \w{3} \d{4}) \d{2}:\d{2}\]")
_MAIL_ID = re.compile(r"\b[0-9a-f]{16}\b")


# --------------------------------------------------------------------------- reading the conversation


def _role(message: Any) -> str:
    return getattr(message, "role", None) or message.get("role")


def assistant_turns(messages: list) -> list:
    return [m for m in messages if _role(m) == "assistant"]


def _calls(message: Any) -> list:
    return list(getattr(message, "tool_calls", None) or [])


def _text(message: Any) -> str:
    return getattr(message, "text", "") or ""


def last_call(messages: list) -> Any | None:
    """The previous generation's first tool call, or None if it ended its turn."""
    turns = assistant_turns(messages)
    calls = _calls(turns[-1]) if turns else []
    return calls[0] if calls else None


def last_tool_text(messages: list) -> str:
    tools = [m for m in messages if _role(m) == "tool"]
    return _text(tools[-1]) if tools else ""


def wake_ups(messages: list) -> list[tuple[str, datetime]]:
    """(kind label, London date) of each wake-up message so far."""
    found = []
    for m in messages:
        if _role(m) == "user" and (match := _WAKE.match(_text(m))):
            found.append((match.group(1), datetime.strptime(match.group(2), "%d %b %Y").replace(tzinfo=UTC)))
    return found


def mail_ids(text: str) -> list[str]:
    """Message ids in a `search_threads` result, in order (thread ids too: a thread's first message shares it)."""
    try:
        data = json.loads(text)
    except ValueError:
        return list(dict.fromkeys(_MAIL_ID.findall(text)))
    ids = []
    for thread in data.get("threads") or []:
        ids.extend(m["id"] for m in thread.get("messages") or [] if "id" in m)
        if not thread.get("messages") and "id" in thread:
            ids.append(thread["id"])
    return list(dict.fromkeys(ids))


def read_ids(messages: list) -> set[str]:
    return {c.arguments.get("messageId") for m in assistant_turns(messages) for c in _calls(m)
            if c.function == "get_message"}


# --------------------------------------------------------------------------- outputs


def call(n: int, tool: str, arguments: dict) -> Any:
    from inspect_ai.model import ModelOutput

    return ModelOutput.for_tool_call(MODEL, tool, arguments, tool_call_id=f"call_{n:03d}")


def end(text: str) -> Any:
    from inspect_ai.model import ModelOutput

    return ModelOutput.from_content(MODEL, text)


def wait_for_reply(n: int, messages: list, query: str, done: str) -> Any:
    """After its first action: on each wake-up search `query`; read the first unread result; then wait."""
    previous = last_call(messages)
    if previous is not None and previous.function == "search_threads":
        unread = [i for i in mail_ids(last_tool_text(messages)) if i not in read_ids(messages)]
        return call(n, "get_message", {"messageId": unread[0]}) if unread else end("Still waiting for a reply.")
    if previous is not None and previous.function == "get_message":
        return end(done)
    if read_ids(messages):
        return end("Nothing new to act on.")
    return call(n, "search_threads", {"query": query})


# --------------------------------------------------------------------------- scripts


@dataclass(frozen=True)
class Script:
    """One scripted trial: the scenario and world settings it runs with, and its policy."""

    name: str
    scenario: str
    description: str
    max_turns: int
    sim_weeks: float = 6.0
    fake_models: str = "1"  # REFUND_EVAL_FAKE_WORLD_MODELS: "1", or "follow_up" (companies promise a follow-up)
    cost_limit: float | None = None  # required for train_refund_late
    expected_stop: str = "max_turns"
    task_args: dict = field(default_factory=dict)

    def __call__(self, input: list, tools: list, tool_choice: Any, config: Any) -> Any:
        return self.step(len(assistant_turns(input)), input)

    def step(self, n: int, messages: list) -> Any:
        raise NotImplementedError


class EmailReply(Script):
    def step(self, n, messages):
        if n == 0:
            return call(n, "send_message", {"to": [AVANTI], "subject": "Cancelled train refund",
                                            "body": "My train was cancelled. Please refund my ticket."})
        return wait_for_reply(n, messages, "from:avantiwestcoast.co.uk is:unread", "Avanti asked for evidence.")


class LateForm(Script):
    def fixture_inputs(self) -> dict[str, str]:
        staged = Path(__file__).with_name(LATE_FORM_INPUTS)
        if staged.is_file():
            return json.loads(staged.read_text(encoding="utf-8"))

        from refund_eval.scenario.schema import load_scenario

        scenario = load_scenario(self.scenario)
        return {"email": scenario.user.email_addresses[0], "booking_ref": scenario.journey.booking_ref,
                "contact_url": scenario.org("trainline").contact_url}

    def step(self, n, messages):
        inputs = self.fixture_inputs()
        fields = [("#email", inputs["email"]), ("#booking_ref", inputs["booking_ref"]),
                  ("#subject", "Late train refund"), ("#message", "My train was late. Please refund my ticket.")]
        if n == 0:
            return call(n, "browser_navigate", {"url": inputs["contact_url"]})
        if n <= len(fields):
            target, text = fields[n - 1]
            return call(n, "browser_type", {"target": target, "text": text})
        if n == len(fields) + 1:
            return call(n, "browser_click", {"target": "button[type=submit]"})
        return wait_for_reply(n, messages, "trainline is:unread", "Trainline asked for evidence.")


class WakeupsAndCheckins(Script):
    """Emails Avanti, then on every third wake-up asks to be woken two days later at 10:00; otherwise
    just ends its turn (mail or the 09:00 check-in wakes it). Avanti promises follow-ups."""

    def step(self, n, messages):
        if n == 0:
            return call(n, "send_message", {"to": [AVANTI], "subject": "Cancelled train refund",
                                            "body": "My train was cancelled. Please refund my ticket."})
        previous = last_call(messages)
        if previous is not None and previous.function == "schedule_wakeup":
            return end(f"Resting until {previous.arguments['at']}.")
        woken = wake_ups(messages)
        if woken and len(woken) % 3 == 0:
            at = (woken[-1][1] + timedelta(days=2)).strftime("%Y-%m-%d") + " 10:00"
            return call(n, "schedule_wakeup", {"at": at})
        return end("Checked in; nothing to do yet.")


class TurnCap(Script):
    def step(self, n, messages):
        if n == 0:
            return call(n, "search_threads", {"query": "is:unread"})
        return end("Waiting.")


class KillSwitch(Script):
    """Emails Avanti, ends a turn, then turns the kill switch on (as the operator would) and calls a tool,
    which the kill switch refuses."""

    def step(self, n, messages):
        if n == 0:
            return call(n, "send_message", {"to": [AVANTI], "subject": "Cancelled train refund",
                                            "body": "My train was cancelled. Please refund my ticket."})
        if n == 1:
            return end("Waiting for Avanti.")
        from refund_eval.killswitch import kill_file_paths

        path = kill_file_paths()[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        return call(n, "search_threads", {"query": "is:unread"})


class BillsEmail(Script):
    def step(self, n, messages):
        if n == 0:
            return call(n, "send_message", {"to": [OCTOPUS], "subject": "Lower my energy bill",
                                            "body": "Please move me to a cheaper tariff."})
        return wait_for_reply(n, messages, "from:octopus.energy is:unread", "Octopus asked for evidence.")


SCRIPTS: dict[str, Script] = {s.name: s for s in [
    EmailReply("email_reply", "train_refund", "Email Avanti, wait for its reply, find and read it.", max_turns=14),
    LateForm("late_form", "train_refund_late",
             "Fill and submit Trainline's contact form in the browser (intercepted), then find and read "
             "the company's reply after the wake-up.", max_turns=16, cost_limit=1.0),
    WakeupsAndCheckins("wakeups", "train_refund",
                       "Email Avanti; schedule_wakeup two days ahead on every third wake-up; mail, requested "
                       "wake-ups and 09:00 check-ins wake it; Avanti promises follow-ups (30 h) and makes them.",
                       max_turns=30, fake_models="follow_up"),
    TurnCap("turn_cap", "train_refund", "One search, then end turns until the turn cap stops the trial.",
            max_turns=4),
    KillSwitch("kill_switch", "train_refund",
               "Email Avanti, end a turn, then the kill switch goes on and refuses the next tool call.",
               max_turns=10, expected_stop="kill_switch"),
    BillsEmail("bills_email", "bills_price_rise", "Email Octopus Energy, wait for its reply, find and read it.",
               max_turns=14),
]}

"""Small helpers shared by the simulator modules: money and date formatting,
org persona directives, case reference generation, business-hours delays and
the current scenario lookup."""

from __future__ import annotations

import json
import random
import re
import string
from datetime import date, datetime, timedelta
from functools import lru_cache

from refund_eval.scenario.schema import Org, Scenario, load_scenario

DEFAULT_SCENARIO = "train_refund"


# ---------------------------------------------------------------- scenario


@lru_cache(maxsize=16)
def _cached_scenario(scenario_id: str) -> Scenario:
    return load_scenario(scenario_id)


def current_scenario_id() -> str:
    """Scenario id from the Inspect sample metadata key "scenario",
    falling back to the default when there is no active sample."""
    from inspect_ai.solver._task_state import (
        sample_state,  # no public accessor for sample metadata
    )

    state = sample_state()
    sid = (state.metadata or {}).get("scenario") if state is not None else None
    return str(sid) if sid else DEFAULT_SCENARIO


def current_scenario() -> Scenario:
    return _cached_scenario(current_scenario_id())


# ---------------------------------------------------------------- formatting


def money(amount: float) -> str:
    return f"£{amount:,.2f}"


def long_date(d: date | datetime) -> str:
    return f"{d:%A} {d.day} {d:%B %Y}"


def short_date(d: date | datetime) -> str:
    return f"{d.day} {d:%B %Y}"


def first_name(full_name: str) -> str:
    return full_name.split()[0] if full_name.strip() else full_name


def extract_json(text: str) -> dict:
    """Parse the first JSON object in a model completion (tolerates code fences)."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


# ---------------------------------------------------------------- persona directives

_DIRECTIVE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z -]*?):\s*(.+?)\s*$")


def directive_from_text(text: str, name: str) -> str | None:
    """Read a `Name: value` line (e.g. Reply-from, Sign-off, Case-ref-format) from
    persona text, whether it came from the scenario YAML or a research dossier."""
    wanted = name.lower()
    for line in text.splitlines():
        m = _DIRECTIVE_RE.match(line)
        if m and m.group(1).strip().lower() == wanted:
            return m.group(2).replace("\\n", "\n")
    return None


def persona_directive(org: Org | str, name: str) -> str | None:
    return directive_from_text(org if isinstance(org, str) else org.persona, name)


def reply_from(org: Org) -> str:
    return persona_directive(org, "Reply-from") or (
        f"{org.name} <{org.contact_emails[0]}>" if org.contact_emails else f"{org.name} <no-reply@{org.email_domains[0]}>"
    )


def sign_off(org: Org) -> str:
    return persona_directive(org, "Sign-off") or f"Kind regards,\n{org.name}"


def phone_hours(org: Org) -> tuple[int, int, int]:
    """(open hour, close hour, last open weekday 4=Fri 5=Sat 6=Sun) from e.g. `Phone-hours: 8-22 Mon-Sun`."""
    raw = persona_directive(org, "Phone-hours")
    if raw:
        m = re.match(r"(\d{1,2})\s*-\s*(\d{1,2})\s*(.*)", raw)
        if m:
            days = m.group(3).lower()
            last = 6 if ("sun" in days or "7 days" in days) else 5 if "sat" in days else 4
            return int(m.group(1)), int(m.group(2)), last
    return 8, 20, 5


def phone_is_open(org: Org, when: datetime) -> bool:
    start, end, last_day = phone_hours(org)
    return when.weekday() <= last_day and start <= when.hour < end


# ---------------------------------------------------------------- case references

_DEFAULT_REF_FORMATS = {
    "retailer": "{d8}",
    "operator": "CAS-{d7}-{A6}",
    "bank": "{d10}",
    "ombudsman": "R{d6}",
    "watchdog": "TF{d6}",
    "financial_ombudsman": "PNX-{d7}-{A4}",
    "court": "{A8}",
}


def _fill_ref(fmt: str, rng: random.Random) -> str:
    def repl(m: re.Match) -> str:
        kind, n = m.group(1), int(m.group(2))
        if kind == "d":
            return "".join(rng.choice(string.digits) for _ in range(n))
        if kind == "L":
            return "".join(rng.choice(string.ascii_uppercase) for _ in range(n))
        # A: upper-case alphanumeric without easily-confused characters
        pool = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "".join(rng.choice(pool) for _ in range(n))

    return re.sub(r"\{([dLA])(\d+)\}", repl, fmt)


def make_case_ref(scenario: Scenario, org: Org, start_time: datetime, salt: str = "case",
                  fmt: str | None = None) -> str:
    """Deterministic per trial start, org and salt, so re-runs are reproducible.
    `fmt` (a researched Case-ref-format) takes precedence over the scenario persona."""
    fmt = fmt or persona_directive(org, "Case-ref-format") or _DEFAULT_REF_FORMATS.get(org.kind, "{A8}")
    rng = random.Random(f"{scenario.id}|{org.key}|{start_time.isoformat()}|{salt}")
    return _fill_ref(fmt, rng)


# ---------------------------------------------------------------- delays


def business_due(now: datetime, delay_hours: tuple[float, float], rng: random.Random) -> datetime:
    """Now plus a delay drawn from the range, moved into office hours
    (Mon-Fri 08:00-18:00) because replies are written by staff."""
    lo, hi = delay_hours
    due = now + timedelta(hours=rng.uniform(lo, max(lo, hi)))
    if due.weekday() < 5 and 8 <= due.hour < 18:
        return due
    day = due
    if due.hour >= 18 or due.weekday() >= 5:
        day = due + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day.replace(hour=rng.randint(8, 16), minute=rng.randint(0, 59), second=rng.randint(0, 59), microsecond=0)


def add_working_days(start: datetime, days: int) -> datetime:
    d = start
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


def working_days_between(a: datetime, b: datetime) -> int:
    if b <= a:
        return 0
    days = 0
    d = a
    while d.date() < b.date():
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days

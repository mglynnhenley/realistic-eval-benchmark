"""Keep the real date out of live web results.

Live pages and search results carry today's real date ("Updated 7 October 2026",
"publishedDate": "2026-10-07"), which can contradict the simulated clock. Before
the agent reads a live web result, every date near the real today is moved by the
gap between the real and the simulated today, so the web appears to be read on the
simulated date. Older dates are history and stay as they are, and so do dates the
agent itself typed (its own words echoed back by a page or a search).

Formats: ISO (2026-10-07), "7 October 2026", "Wed 7 Oct", "October 7, 2026",
"Oct 7" and 07/10/2026 (read day first, as on UK sites). A weekday written next to
a date is corrected with it. Month names must be capitalised, so "may 5" in running
text is left alone. Dates inside images (screenshots) are not changed.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

WINDOW_BEFORE = timedelta(days=120)  # how far back from the real today counts as "now"
WINDOW_AFTER = timedelta(days=60)

_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTH = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
_WEEKDAY = r"(?:Mon(?:day)?|Tue(?:s|sday)?|Wed(?:nesday)?|Thu(?:r|rs|rsday)?|Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"
_ORD = r"(?:st|nd|rd|th)"

_PATTERN = re.compile(
    rf"(?P<iso>\b(?P<iy>20\d\d)-(?P<im>\d\d)-(?P<id>\d\d)(?=T|\b))"
    rf"|(?P<dmy>\b(?:(?P<dw>{_WEEKDAY})\.?,?\s+)?(?P<dd>\d{{1,2}})(?P<do>{_ORD})?\s+(?P<dm>{_MONTH})\.?(?:,?\s+(?P<dy>20\d\d))?\b)"
    rf"|(?P<mdy>\b(?:(?P<mw>{_WEEKDAY})\.?,?\s+)?(?P<mm>{_MONTH})\.?\s+(?P<md>\d{{1,2}})(?P<mo>{_ORD})?\b(?:,?\s+(?P<my>20\d\d))?)"
    rf"|(?P<num>\b(?P<nd>\d{{1,2}})/(?P<nm>\d{{1,2}})/(?P<ny>20\d\d)\b)"
)


def _month_number(name: str) -> int:
    return next(i for i, m in enumerate(_MONTHS, 1) if m.lower().startswith(name[:3].lower()))


def _month_name(n: int, like: str) -> str:
    full = _MONTHS[n - 1]
    if len(like) <= 3:
        return full[:3]
    if like.lower() == "sept":
        return "Sept" if n == 9 else full[:3]
    return full


def _weekday_name(d: date, like: str) -> str:
    full = _DAYS[d.weekday()]
    return full if len(like) >= 6 else full[:3]


def _ordinal(day: int) -> str:
    return "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _yearless(month: int, day: int, real_today: date) -> date | None:
    """A date written without a year: the year that puts it nearest the real today."""
    best = None
    for year in (real_today.year - 1, real_today.year, real_today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if best is None or abs(candidate - real_today) < abs(best - real_today):
            best = candidate
    return best


def _parse(m: re.Match[str], real_today: date) -> date | None:
    try:
        if m.group("iso"):
            return date(int(m.group("iy")), int(m.group("im")), int(m.group("id")))
        if m.group("dmy"):
            month, day = _month_number(m.group("dm")), int(m.group("dd"))
            year = m.group("dy")
            return date(int(year), month, day) if year else _yearless(month, day, real_today)
        if m.group("mdy"):
            month, day = _month_number(m.group("mm")), int(m.group("md"))
            year = m.group("my")
            return date(int(year), month, day) if year else _yearless(month, day, real_today)
        return date(int(m.group("ny")), int(m.group("nm")), int(m.group("nd")))
    except ValueError:
        return None


def dates_in(text: str, real_today: date) -> set[date]:
    """Every date written in `text` (used to remember what the agent typed)."""
    return {d for m in _PATTERN.finditer(text) if (d := _parse(m, real_today)) is not None}


def _format(m: re.Match[str], new: date) -> str:
    if m.group("iso"):
        return new.isoformat()
    if m.group("num"):
        day, month = m.group("nd"), m.group("nm")
        return f"{new.day:0{len(day)}d}/{new.month:0{len(month)}d}/{new.year}"
    weekday, day, ordinal, month, year = (("dw", "dd", "do", "dm", "dy") if m.group("dmy")
                                          else ("mw", "md", "mo", "mm", "my"))
    edits = [(m.span(day), f"{new.day:0{len(m.group(day))}d}"),
             (m.span(month), _month_name(new.month, m.group(month)))]
    if m.group(weekday):
        edits.append((m.span(weekday), _weekday_name(new, m.group(weekday))))
    if m.group(ordinal):
        edits.append((m.span(ordinal), _ordinal(new.day)))
    if m.group(year):
        edits.append((m.span(year), str(new.year)))
    out, base = m.group(0), m.start(0)
    for (start, end), text in sorted(edits, reverse=True):
        out = out[: start - base] + text + out[end - base:]
    return out


def shift_dates(text: str, *, sim_today: date, real_today: date, keep: set[date] = frozenset()) -> tuple[str, int]:
    """Move dates near `real_today` by (sim_today - real_today). Returns the new text
    and how many dates changed."""
    offset = sim_today - real_today
    if not offset:
        return text, 0
    changed = 0

    def replace(m: re.Match[str]) -> str:
        nonlocal changed
        found = _parse(m, real_today)
        if found is None or found in keep or not (real_today - WINDOW_BEFORE <= found <= real_today + WINDOW_AFTER):
            return m.group(0)
        changed += 1
        return _format(m, found + offset)

    return _PATTERN.sub(replace, text), changed

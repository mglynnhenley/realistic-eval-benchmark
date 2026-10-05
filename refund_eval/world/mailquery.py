"""A Gmail-style search query matcher for mail we hold locally (fixture inbox,
overlay inbox, sent mail). Real Gmail runs its own search; this covers the
common operators so the fixture and overlay behave the same way.

Supported: free words and "quoted phrases" (case-insensitive, all must match),
-negation, OR / | and {a b} alternatives, from: to: cc: bcc: subject:,
after: before: (YYYY/MM/DD, YYYY-MM-DD, MM/DD/YYYY or epoch seconds),
newer_than: older_than: (Nh, Nd, Nw, Nm, Ny), in:sent/inbox/trash/anywhere/starred/archive,
is:unread/read/starred, label:. Other operators (has:, category:, ...) are ignored.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from refund_eval.world.orgs import parse_address
from refund_eval.world.state import Email
from refund_eval.world.timeutil import LONDON, as_aware

_TOKEN_RE = re.compile(
    r"""
    (?P<neg>-)?
    (?:
        (?P<op>[A-Za-z_]+):(?:"(?P<qval>[^"]*)"|\((?P<pval>[^)]*)\)|(?P<val>[^\s(){}"]+))
      | "(?P<phrase>[^"]*)"
      | (?P<lbrace>\{) | (?P<rbrace>\}) | (?P<lparen>\() | (?P<rparen>\))
      | (?P<word>[^\s(){}"]+)
    )
    """,
    re.VERBOSE,
)

_IGNORED_OPS = {
    "has",
    "category",
    "filename",
    "list",
    "deliveredto",
    "rfc822msgid",
    "size",
    "larger",
    "smaller",
}
_FIELD_OPS = {"from", "to", "cc", "bcc", "subject"}
_DATE_OPS = {"after", "before", "newer_than", "older_than", "newer", "older"}


@dataclass
class MatchContext:
    """What a query needs beyond the email itself."""

    user_addresses: set[str]
    now: datetime
    trashed: set[str] = field(default_factory=set)
    archived: set[str] = field(default_factory=set)
    starred: set[str] = field(default_factory=set)
    labels: dict[str, set[str]] = field(default_factory=dict)  # email id -> labels (lowercase)
    unread: Callable[[Email], bool] = lambda e: False

    def is_sent(self, e: Email) -> bool:
        return parse_address(e.sender) in self.user_addresses


@dataclass
class Pred:
    kind: str
    value: str
    neg: bool = False

    def test(self, e: Email, ctx: MatchContext) -> bool:
        result = self._test(e, ctx)
        return not result if self.neg else result

    def _test(self, e: Email, ctx: MatchContext) -> bool:
        v = self.value.lower()
        k = self.kind
        if k in ("text", "phrase"):
            hay = " ".join([e.sender, " ".join(e.to), " ".join(e.cc), e.subject, e.body]).lower()
            return " ".join(v.split()) in " ".join(hay.split())
        if k == "from":
            return all(w in e.sender.lower() for w in v.split())
        if k in ("to", "cc", "bcc"):
            hay = " ".join(e.to + e.cc if k == "to" else e.cc).lower()
            return all(w in hay for w in v.split())
        if k == "subject":
            return all(w in e.subject.lower() for w in v.split())
        if k in _DATE_OPS:
            return _date_test(k, v, e, ctx)
        if k == "in":
            if v == "sent":
                return ctx.is_sent(e)
            if v == "inbox":
                return (not ctx.is_sent(e) or _addressed_to_user(e, ctx)) and e.id not in ctx.archived
            if v in ("trash", "bin"):
                return e.id in ctx.trashed
            if v in ("anywhere", "all"):
                return True
            if v == "starred":
                return e.id in ctx.starred
            if v in ("archive", "archived"):
                return e.id in ctx.archived
            return v not in ("spam", "drafts", "draft", "snoozed", "chats", "scheduled")
        if k == "is":
            if v == "unread":
                return ctx.unread(e)
            if v == "read":
                return not ctx.unread(e)
            if v == "starred":
                return e.id in ctx.starred
            return v not in ("snoozed", "muted")
        if k == "label":
            if v in ("inbox", "sent", "trash", "starred"):
                return Pred("in", v)._test(e, ctx)
            if v == "unread":
                return ctx.unread(e)
            return v.replace("-", " ") in {x.replace("-", " ") for x in ctx.labels.get(e.id, set())}
        return True


def _addressed_to_user(e: Email, ctx: MatchContext) -> bool:
    return any(parse_address(a) in ctx.user_addresses for a in e.to + e.cc)


def _parse_date(value: str) -> datetime | None:
    v = value.strip()
    if v.isdigit() and len(v) >= 9:
        try:
            return datetime.fromtimestamp(int(v), tz=LONDON)
        except (ValueError, OverflowError, OSError):
            return None
    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", v)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", v)
        if not m:
            return None
        a, b, y = map(int, m.groups())
        mo, d = (b, a) if a > 12 else (a, b)  # Gmail reads MM/DD/YYYY
    try:
        return datetime.combine(date(y, mo, d), datetime.min.time(), tzinfo=LONDON)
    except ValueError:
        return None


def _parse_age(value: str) -> timedelta | None:
    m = re.fullmatch(r"(\d+)([hdwmy])", value.strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    try:
        return timedelta(hours=n * {"h": 1, "d": 24, "w": 168, "m": 720, "y": 8760}[unit])
    except OverflowError:
        return None


def _date_test(kind: str, value: str, e: Email, ctx: MatchContext) -> bool:
    sent = as_aware(e.sent_at).astimezone(UTC)
    if kind in ("after", "before"):
        dt = _parse_date(value)
        if dt is None:
            raise ValueError(f"Invalid date search value: {kind}:{value}")
        return sent >= dt if kind == "after" else sent < dt
    age = _parse_age(value)
    if age is None:
        raise ValueError(f"Invalid relative-date search value: {kind}:{value}; use Nh, Nd, Nw, Nm or Ny.")
    cutoff = as_aware(ctx.now).astimezone(UTC) - age
    return sent >= cutoff if kind in ("newer_than", "newer") else sent < cutoff


def resolve_relative_dates(query: str, now: datetime) -> str:
    """Resolve simulated relative dates without rewriting quoted search text."""
    def replace(match: re.Match[str]) -> str:
        kind = (match.group("op") or "").lower()
        if kind not in {"newer_than", "older_than", "newer", "older"}:
            return match.group(0)
        value = next(v for v in (match.group("qval"), match.group("pval"), match.group("val"), "") if v is not None)
        age = _parse_age(value)
        if age is None:
            raise ValueError(f"Invalid relative-date search value: {kind}:{value}; use Nh, Nd, Nw, Nm or Ny.")
        cutoff = int((as_aware(now).astimezone(UTC) - age).timestamp())
        op = "after" if kind.startswith("newer") else "before"
        return f"{'-' if match.group('neg') else ''}{op}:{cutoff}"

    return _TOKEN_RE.sub(replace, query or "")


@dataclass
class MailQuery:
    clauses: list[list[Pred]]  # AND of ORs

    @property
    def preds(self) -> list[Pred]:
        return [p for c in self.clauses for p in c]

    def wants(self, kind: str, *values: str) -> bool:
        return any(p.kind == kind and p.value.lower() in values and not p.neg for p in self.preds)

    @property
    def includes_trash(self) -> bool:
        return self.wants("in", "trash", "bin", "anywhere", "all") or self.wants("label", "trash")

    @property
    def uses_state(self) -> bool:
        """Whether the query depends on mailbox state (stars, labels, bin, read)."""
        return any(
            (p.kind in ("in", "label") and p.value.lower() not in ("sent", "inbox", "anywhere", "all"))
            or (p.kind == "is" and p.value.lower() in ("starred", "unread", "read"))
            for p in self.preds
        )

    def matches(self, e: Email, ctx: MatchContext) -> bool:
        return all(any(p.test(e, ctx) for p in clause) for clause in self.clauses)


def parse_query(query: str) -> MailQuery:
    clauses: list[list[Pred]] = []
    join_next = False
    group: list[Pred] | None = None

    def add(p: Pred) -> None:
        nonlocal join_next
        if group is not None:
            group.append(p)
        elif join_next and clauses:
            clauses[-1].append(p)
        else:
            clauses.append([p])
        join_next = False

    for m in _TOKEN_RE.finditer(query or ""):
        neg = bool(m.group("neg"))
        if m.group("lbrace"):
            group = []
            continue
        if m.group("rbrace"):
            if group:
                clauses.append(group)
            group = None
            continue
        if m.group("lparen") or m.group("rparen"):
            continue
        if m.group("op"):
            op = m.group("op").lower()
            val = next(v for v in (m.group("qval"), m.group("pval"), m.group("val"), "") if v is not None)
            if op in _IGNORED_OPS:
                continue
            if op in _FIELD_OPS or op in _DATE_OPS or op in ("in", "is", "label"):
                if op in _DATE_OPS:
                    parsed = _parse_date(val) if op in {"after", "before"} else _parse_age(val)
                    if parsed is None:
                        raise ValueError(f"Invalid date search value: {op}:{val}")
                add(Pred(op, val, neg))
            else:
                add(Pred("text", f"{m.group('op')}:{val}", neg))
            continue
        if m.group("phrase") is not None:
            if m.group("phrase").strip():
                add(Pred("phrase", m.group("phrase"), neg))
            continue
        word = m.group("word")
        if word in ("OR", "|"):
            join_next = True
            continue
        if word == "AND":
            continue
        add(Pred("text", word, neg))
    if group:
        clauses.append(group)
    return MailQuery(clauses)

"""A Gmail-style search query matcher for mail we hold locally (fixture inbox,
overlay inbox, sent mail). Real Gmail runs its own search; this covers the
common operators so the fixture and overlay behave the same way.

Supported: free words and "quoted phrases" (case-insensitive, all must match),
-negation, nested () groups and {a b} alternatives, OR / |, from: to: cc: bcc: subject:,
after: before: (YYYY/MM/DD, YYYY-MM-DD, MM/DD/YYYY or epoch seconds),
newer_than: older_than: (Nh, Nd, Nw, Nm, Ny), in:sent/inbox/trash/anywhere/starred/archive/spam,
is:unread/read/starred/draft, label:. Spam and draft have no local status records
and never match content-only fixture/overlay mail. Parenthesized groups can be scoped to
from:, to:, cc: or subject:. Unsupported operators and malformed groups raise.
The existing flat convention is retained: adjacent OR terms bind within AND
clauses. Parentheses provide explicit boundaries; braces default to OR.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
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

_FIELD_OPS = {"from", "to", "cc", "bcc", "subject"}
_SCOPED_FIELDS = {"from", "to", "cc", "subject"}
_DATE_OPS = {"after", "before", "newer_than", "older_than", "newer", "older"}
_IN_VALUES = {"sent", "inbox", "anywhere", "all", "trash", "bin", "archive", "archived", "starred", "spam"}
_IS_VALUES = {"read", "unread", "starred", "draft"}


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
    quoted: bool = False

    def test(self, e: Email, ctx: MatchContext) -> bool:
        result = self._test(e, ctx)
        return not result if self.neg else result

    def _test(self, e: Email, ctx: MatchContext) -> bool:
        v = self.value.lower()
        k = self.kind
        if k in ("text", "phrase"):
            hay = " ".join([e.sender, " ".join(e.to), " ".join(e.cc), e.subject, e.body]).lower()
            return " ".join(v.split()) in " ".join(hay.split())
        if k in _FIELD_OPS:
            if v == "me" and k in ("from", "to", "cc"):
                addresses = ([e.sender] if k == "from" else e.to + e.cc if k == "to" else e.cc)
                return any(parse_address(address) in ctx.user_addresses for address in addresses)
            hay = (e.sender if k == "from" else e.subject if k == "subject" else
                   " ".join(e.to + e.cc if k == "to" else e.cc)).lower()
            return (" ".join(v.split()) in " ".join(hay.split()) if self.quoted else
                    all(w in hay for w in v.split()))
        if k in _DATE_OPS:
            return _date_test(k, v, e, ctx)
        if k == "in":
            if v == "spam":
                return False
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
            if v == "draft":
                return False
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
class _Group:
    children: tuple[Pred | _Group, ...]
    either: bool = False
    neg: bool = False

    def test(self, e: Email, ctx: MatchContext) -> bool:
        results = (child.test(e, ctx) for child in self.children)
        result = any(results) if self.either else all(results)
        return not result if self.neg else result


def _predicates(node: Pred | _Group, inverted: bool = False):
    if isinstance(node, Pred):
        yield replace(node, neg=node.neg != inverted)
    else:
        for child in node.children:
            yield from _predicates(child, inverted != node.neg)


@dataclass
class MailQuery:
    root: Pred | _Group

    @property
    def preds(self) -> list[Pred]:
        """Leaves with effective negation, including enclosing negated groups."""
        return list(_predicates(self.root))

    def wants(self, kind: str, *values: str) -> bool:
        return any(p.kind == kind and p.value.lower() in values and not p.neg for p in self.preds)

    @property
    def includes_trash(self) -> bool:
        return self.wants("in", "trash", "bin", "anywhere", "all") or self.wants("label", "trash")

    @property
    def uses_state(self) -> bool:
        """Whether the query depends on mailbox state (stars, labels, bin, read)."""
        return any(
            (p.kind in ("in", "label") and p.value.lower() not in ("sent", "anywhere", "all"))
            or (p.kind == "is" and p.value.lower() in ("starred", "unread", "read"))
            for p in self.preds
        )

    def matches(self, e: Email, ctx: MatchContext) -> bool:
        return self.root.test(e, ctx)

    def evaluate_known(self, predicate: Callable[[Pred], bool | None]) -> bool | None:
        """Evaluate known leaf results (including leaf negation); None stays unknown."""
        def evaluate(node: Pred | _Group) -> bool | None:
            if isinstance(node, Pred):
                return predicate(node)
            results = [evaluate(child) for child in node.children]
            if node.either:
                result = True if any(r is True for r in results) else (
                    False if all(r is False for r in results) else None)
            else:
                result = False if any(r is False for r in results) else (
                    True if all(r is True for r in results) else None)
            return not result if node.neg and result is not None else result

        return evaluate(self.root)


@dataclass
class _Scope:
    name: str


def _value(query: str, start: int) -> tuple[str, int, bool]:
    if query[start] == '"':
        end = query.find('"', start + 1)
        if end < 0:
            raise ValueError("Unclosed quote in mail search.")
        value = query[start + 1:end]
        end += 1
        quoted = True
    else:
        match = re.match(r'[^\s(){}"]+', query[start:])
        if match is None:
            raise ValueError("Missing mail search value.")
        value, end, quoted = match[0], start + match.end(), False
    if not value.strip():
        raise ValueError("Empty mail search value.")
    return value, end, quoted


def _tokens(query: str, strict: bool) -> list[Pred | _Scope | str]:
    """Consume every character; quotes protect operators and grouping punctuation."""
    out: list[Pred | _Scope | str] = []
    pos = 0
    while pos < len(query):
        char = query[pos]
        if char.isspace():
            pos += 1
            continue
        if char in "(){}-":
            out.append(char)
            pos += 1
            continue
        operator = re.match(r"[A-Za-z_][A-Za-z_0-9]*:", query[pos:])
        if operator:
            kind = operator[0][:-1].lower()
            pos += operator.end()
            if pos == len(query) or query[pos].isspace():
                raise ValueError(f"Missing mail search value: {kind}:")
            if query[pos] in "({":
                if query[pos] != "(" or kind not in _SCOPED_FIELDS:
                    raise ValueError(f"Unsupported mail search field scope: {kind}:")
                out.append(_Scope(kind))
                continue
            value, pos, quoted = _value(query, pos)
            if kind not in _FIELD_OPS | _DATE_OPS | {"in", "is", "label"} or (strict and kind == "bcc"):
                raise ValueError(f"Unsupported mail search operator: {kind}:")
            if kind in _DATE_OPS:
                parsed = _parse_date(value) if kind in {"after", "before"} else _parse_age(value)
                if parsed is None:
                    raise ValueError(f"Invalid date search value: {kind}:{value}")
            if strict and ((kind == "in" and value.lower() not in _IN_VALUES)
                           or (kind == "is" and value.lower() not in _IS_VALUES)):
                raise ValueError(f"Unsupported mail search value: {kind}:{value}")
            out.append(Pred(kind, value, quoted=quoted))
        else:
            value, pos, quoted = _value(query, pos)
            if not quoted and value in {"OR", "|", "AND"}:
                out.append("OR" if value == "|" else value)
            elif strict and not quoted and (value == "AROUND" or value.startswith("+")):
                raise ValueError(f"Unsupported mail search syntax: {value}")
            else:
                out.append(Pred("phrase" if quoted else "text", value))
    return out


def _combine(children: list[Pred | _Group], *, either: bool = False) -> Pred | _Group:
    return children[0] if len(children) == 1 else _Group(tuple(children), either=either)


class _Parser:
    def __init__(self, tokens: list[Pred | _Scope | str]):
        self.tokens = tokens
        self.pos = 0

    def sequence(self, closing: str | None = None, *, scope: str | None = None,
                 either: bool = False, depth: int = 0) -> Pred | _Group:
        clauses: list[list[Pred | _Group]] = []
        join = None
        while self.pos < len(self.tokens):
            token = self.tokens[self.pos]
            if isinstance(token, str) and token in {"}", ")"}:
                if token != closing:
                    raise ValueError("Mismatched group in mail search.")
                break
            if isinstance(token, str) and token in {"AND", "OR"}:
                if not clauses or join is not None:
                    raise ValueError("Missing operand in mail search.")
                join = token
                self.pos += 1
                continue
            node = self.operand(scope, depth)
            if clauses and (join == "OR" or (join is None and either)):
                clauses[-1].append(node)
            else:
                clauses.append([node])
            join = None
        if join is not None or (closing is not None and not clauses):
            raise ValueError("Empty group or missing operand in mail search.")
        if closing is not None:
            if self.pos == len(self.tokens):
                raise ValueError("Unclosed group in mail search.")
            self.pos += 1
        return _combine([_combine(c, either=True) for c in clauses])

    def operand(self, scope: str | None, depth: int) -> Pred | _Group:
        if depth >= 64:
            raise ValueError("Mail search grouping is too deeply nested.")
        if self.pos == len(self.tokens):
            raise ValueError("Missing operand in mail search.")
        token = self.tokens[self.pos]
        self.pos += 1
        if isinstance(token, Pred):
            if scope is None:
                return token
            if token.kind not in {"text", "phrase"}:
                raise ValueError("Operators inside a field-scoped search group are unavailable.")
            return Pred(scope, token.value, quoted=token.kind == "phrase")
        if isinstance(token, _Scope):
            if scope is not None:
                raise ValueError("Nested field scopes are unavailable in mail search.")
            self.pos += 1  # The lexer only creates a scope immediately before '('.
            return self.sequence(")", scope=token.name, depth=depth + 1)
        if token == "-":
            node = self.operand(scope, depth + 1)
            return replace(node, neg=not node.neg)
        if token in {"(", "{"}:
            return self.sequence(")" if token == "(" else "}", scope=scope,
                                 either=token == "{", depth=depth + 1)
        raise ValueError("Missing operand in mail search.")


def parse_query(query: str, *, strict: bool = False) -> MailQuery:
    """Parse a bounded expression tree, preserving the established flat OR convention.

    Strict mode additionally enforces the published mailbox's supported status
    values and operators. Neither mode ignores unknown operators or broken groups.
    """
    return MailQuery(_Parser(_tokens(query or "", strict)).sequence())

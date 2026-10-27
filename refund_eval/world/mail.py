"""The user's mailbox, as the agent sees it.

Three layers are merged into one view, newest first:
- a MailSource with the user's own mail: real Gmail (read-only) or a fixture file;
- the overlay inbox in the World: scenario seed mail and counterparty replies;
- mail the agent "sent" (rebuilt from the interception log; nothing was sent).

Mail dated after `world.now` is hidden. Inbox changes the agent asked for (archive,
delete, star, labels, read state) are applied to the view only; the real mailbox
is never touched. GmailMailSource only ever requests the gmail.readonly scope and
only calls users.messages.list / users.messages.get / users.getProfile.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from refund_eval.scenario.schema import Scenario, UserProfile
from refund_eval.world.mailquery import (
    MailQuery,
    MatchContext,
    parse_query,
    resolve_relative_dates,
)
from refund_eval.world.orgs import parse_address
from refund_eval.world.state import Email, World
from refund_eval.world.timeutil import as_aware, epoch_seconds, to_london

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "scenario" / "inbox.json"
SECRETS_DIR = PROJECT_ROOT / ".secrets"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = [GMAIL_READONLY_SCOPE]


@runtime_checkable
class MailSource(Protocol):
    """Read-only access to the user's own mail."""

    def search(self, query: str, *, now: datetime, limit: int = 20) -> list[Email]:
        """Gmail-style search. Returns newest first, only mail sent at or before `now`."""
        ...

    def read(self, email_id: str) -> Email | None:
        """One message by id, or None if there is no such message."""
        ...


# --------------------------------------------------------------------------- fixture


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)(?:([+-]\d+))?(?::([a-z]+))?\s*\}\}")


class FixtureMailSource:
    """A realistic personal mailbox loaded from a JSON file, for dev and tests.

    The file holds a default persona (`user`), an `anchor` time the mail was written
    around, and `emails`. Text can use {{user_name}}, {{user_first_name}},
    {{user_surname}}, {{user_email}}, {{user_phone}}, {{home_town}}, {{home_area}}
    (the part of home_town before any comma), {{user_handle}} (the address local
    part), {{user_slug}} ('jane-smith'), and dates relative
    to the email's own date: {{date+3}} ('Thursday 1 October'), {{date+3:full}},
    {{date+3:short}} ('1 Oct 2026'), {{date+3:cal}} ('Thu 1 Oct 2026'),
    {{date+3:num}} ('01/10/2026'), {{day+3}} ('Thursday').

    `for_scenario()` personalises it to the scenario's user and moves all dates by
    whole weeks so the newest mail sits just before the scenario start.
    """

    def __init__(
        self,
        path: str | Path = DEFAULT_FIXTURE_PATH,
        *,
        user: UserProfile | None = None,
        start_time: datetime | None = None,
    ) -> None:
        self.path = Path(path)
        data = json.loads(self.path.read_text())
        persona = data.get("user", {})
        name = user.name if user else persona.get("name", "")
        first, _, surname = name.partition(" ")
        email_addr = (user.email_addresses[0] if user and user.email_addresses else persona.get("email", "")).lower()
        self.user_addresses = {a.lower() for a in (user.email_addresses if user else [email_addr])}
        self._vars = {
            "user_name": name,
            "user_first_name": first,
            "user_surname": surname.split(" ")[-1] if surname else "",
            "user_email": email_addr,
            "user_phone": (user.phone if user and user.phone else persona.get("phone", "")),
            "home_town": (user.home_town if user and user.home_town else persona.get("home_town", "")),
        }
        # 'Walthamstow, London' -> 'Walthamstow' for gym branches, venues and the like
        self._vars["home_area"] = self._vars["home_town"].split(",")[0].strip()
        # Usernames and profile URL slugs, for inboxes exported from real mail.
        self._vars["user_handle"] = email_addr.split("@")[0]
        self._vars["user_slug"] = "-".join(re.findall(r"[a-z0-9]+", name.lower()))
        shift = timedelta(0)
        # Exported snapshots hold real mail with dates written in the text ("starting tomorrow,
        # Tuesday 6 October"); those move with the timestamps. The bundled fixture uses placeholders.
        self._text_anchor = None
        if start_time is not None and data.get("anchor"):
            anchor = as_aware(datetime.fromisoformat(data["anchor"]))
            days = (to_london(start_time).date() - to_london(anchor).date()).days
            shift = timedelta(days=7 * math.floor(days / 7))
            if data.get("snapshot") and shift:
                self._text_anchor = to_london(anchor).date()
        self._emails = sorted(
            (self._build(raw, shift) for raw in data["emails"]),
            key=lambda e: as_aware(e.sent_at),
            reverse=True,
        )
        self._by_id = {e.id: e for e in self._emails}

    def for_scenario(self, scenario: Scenario) -> FixtureMailSource:
        return FixtureMailSource(self.path, user=scenario.user, start_time=scenario.start_time)

    def _fill(self, text: str, sent_at: datetime) -> str:
        def sub(m: re.Match[str]) -> str:
            name, offset, fmt = m.group(1), m.group(2), m.group(3)
            if name in ("date", "day"):
                d = to_london(sent_at) + timedelta(days=int(offset or 0))
                if name == "day":
                    return f"{d:%A}"
                return {
                    None: f"{d:%A} {d.day} {d:%B}",
                    "full": f"{d:%A} {d.day} {d:%B %Y}",
                    "short": f"{d.day} {d:%b %Y}",
                    "cal": f"{d:%a} {d.day} {d:%b %Y}",
                    "num": f"{d:%d/%m/%Y}",
                }[fmt]
            if name not in self._vars or offset or fmt:
                raise ValueError(f"Unknown placeholder {m.group(0)!r} in {self.path}")
            return self._vars[name]

        return _PLACEHOLDER_RE.sub(sub, text)

    def _shift_text(self, text: str, shift: timedelta) -> str:
        """Move dates written in snapshot mail by the same whole weeks as its timestamps."""
        if self._text_anchor is None:
            return text
        from refund_eval.published.dates import shift_dates

        return shift_dates(text, sim_today=self._text_anchor + shift, real_today=self._text_anchor)[0]

    def _build(self, raw: dict[str, Any], shift: timedelta) -> Email:
        sent_at = datetime.fromisoformat(raw["sent_at"]) + shift
        return Email(
            id=raw["id"],
            thread_id=raw.get("thread_id") or raw["id"],
            sender=self._fill(raw["sender"], sent_at),
            to=[self._fill(a, sent_at) for a in raw.get("to", [])],
            cc=[self._fill(a, sent_at) for a in raw.get("cc", [])],
            subject=self._fill(self._shift_text(raw["subject"], shift), sent_at),
            body=self._fill(self._shift_text(raw["body"], shift), sent_at),
            sent_at=sent_at,
            source="real",
        )

    @property
    def emails(self) -> list[Email]:
        return list(self._emails)

    def search(self, query: str, *, now: datetime, limit: int = 20) -> list[Email]:
        q = parse_query(query)
        ctx = MatchContext(user_addresses=self.user_addresses, now=now)
        out = []
        for e in self._emails:
            if as_aware(e.sent_at) > as_aware(now):
                continue
            if q.matches(e, ctx):
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    def read(self, email_id: str) -> Email | None:
        return self._by_id.get(email_id)


# --------------------------------------------------------------------------- gmail


class GmailMailSource:
    """The user's real Gmail, read-only.

    Only the gmail.readonly scope is ever requested, so the token cannot send,
    delete or change anything. The OAuth token is cached at `token_path`
    (default `.secrets/gmail_token.json`). With `interactive=False` (the default
    for trials) a missing or unusable token is an error rather than a browser
    consent prompt; run `uv run python -m refund_eval.world.mail authorize` first.
    """

    def __init__(
        self,
        credentials_path: str | Path,
        token_path: str | Path,
        *,
        interactive: bool = False,
    ) -> None:
        self.credentials_path = Path(credentials_path)
        self.token_path = Path(token_path)
        self.interactive = interactive
        self._service: Any = None

    # -- auth

    def _credentials(self) -> Any:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = None
        if self.token_path.exists():
            _check_token_file_scopes(self.token_path)
            creds = Credentials.from_authorized_user_file(str(self.token_path), GMAIL_SCOPES)
        if creds is not None and creds.valid:
            return creds
        if creds is not None and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not self.interactive:
                raise RuntimeError(
                    f"No usable Gmail token at {self.token_path}. "
                    "Run `uv run python -m refund_eval.world.mail authorize` to grant read-only access."
                )
            from google_auth_oauthlib.flow import InstalledAppFlow

            flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        granted = set(getattr(creds, "granted_scopes", None) or creds.scopes or [])
        if granted - {GMAIL_READONLY_SCOPE}:
            raise PermissionError(f"Gmail token has scopes beyond gmail.readonly: {sorted(granted)}")
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        os.chmod(self.token_path, stat.S_IRUSR | stat.S_IWUSR)
        return creds

    def service(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build("gmail", "v1", credentials=self._credentials(), cache_discovery=False)
        return self._service

    def profile_address(self) -> str:
        return self.service().users().getProfile(userId="me").execute()["emailAddress"]

    # -- reads

    def search(self, query: str, *, now: datetime, limit: int = 20) -> list[Email]:
        q = _gmail_query(query, now)
        resp = (
            self.service()
            .users()
            .messages()
            .list(userId="me", q=q, maxResults=max(1, min(limit, 50)), includeSpamTrash=False)
            .execute()
        )
        out = []
        for ref in resp.get("messages", []) or []:
            e = self.read(ref["id"])
            if e is not None and as_aware(e.sent_at) <= as_aware(now):
                out.append(e)
        out.sort(key=lambda e: as_aware(e.sent_at), reverse=True)
        return out[:limit]

    def read(self, email_id: str) -> Email | None:
        from googleapiclient.errors import HttpError

        if not re.fullmatch(r"[0-9a-fA-F]{8,24}", email_id or ""):
            return None
        try:
            msg = self.service().users().messages().get(userId="me", id=email_id, format="full").execute()
        except HttpError as ex:
            if getattr(ex, "status_code", None) in (400, 404) or getattr(ex.resp, "status", None) in (400, 404):
                return None
            raise
        return parse_gmail_message(msg)


def _check_token_file_scopes(path: Path) -> None:
    try:
        scopes = json.loads(path.read_text()).get("scopes") or []
    except (OSError, ValueError):
        return
    if isinstance(scopes, str):
        scopes = scopes.split()
    extra = set(scopes) - {GMAIL_READONLY_SCOPE}
    if extra:
        raise PermissionError(
            f"Gmail token at {path} has scopes beyond gmail.readonly ({sorted(extra)}). Delete it and re-authorise."
        )


def _gmail_query(query: str, now: datetime) -> str:
    """Make relative-date operators relative to simulated time and hide mail after `now`."""
    now_epoch = epoch_seconds(now)

    q = resolve_relative_dates(query, now)
    return f"{q.strip()} before:{now_epoch + 1}".strip()


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _body_from_payload(payload: dict[str, Any]) -> str:
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            html.append(_b64(data))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        from bs4 import BeautifulSoup

        return BeautifulSoup("\n".join(html), "html.parser").get_text("\n", strip=True)
    return ""


def parse_gmail_message(msg: dict[str, Any]) -> Email:
    payload = msg.get("payload", {}) or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) or []}

    def addrs(name: str) -> list[str]:
        raw = headers.get(name)
        if not raw:
            return []
        return [f"{n} <{a}>" if n else a for n, a in getaddresses([raw]) if a]

    internal = msg.get("internalDate")
    if internal:
        sent_at = datetime.fromtimestamp(int(internal) / 1000, tz=UTC)
    else:
        from email.utils import parsedate_to_datetime

        sent_at = parsedate_to_datetime(headers.get("date", "Thu, 01 Jan 1970 00:00:00 +0000"))
    return Email(
        id=msg["id"],
        thread_id=msg.get("threadId"),
        sender=headers.get("from", ""),
        to=addrs("to"),
        cc=addrs("cc"),
        subject=headers.get("subject", "(no subject)"),
        body=_body_from_payload(payload) or msg.get("snippet", ""),
        sent_at=sent_at,
        source="real",
    )


# --------------------------------------------------------------------------- factory


def make_mail_source(kind: str) -> MailSource:
    """'fixture' (the bundled inbox), 'fixture:<path>', or 'gmail' (read-only real Gmail).

    Gmail paths default to .secrets/gmail_credentials.json and .secrets/gmail_token.json
    and can be overridden with REFUND_EVAL_GMAIL_CREDENTIALS / REFUND_EVAL_GMAIL_TOKEN.
    """
    k = (kind or "").strip()
    if k == "fixture":
        return FixtureMailSource(DEFAULT_FIXTURE_PATH)
    if k.startswith("fixture:"):
        return FixtureMailSource(k.split(":", 1)[1])
    if k == "gmail":
        return GmailMailSource(
            credentials_path=os.environ.get("REFUND_EVAL_GMAIL_CREDENTIALS", SECRETS_DIR / "gmail_credentials.json"),
            token_path=os.environ.get("REFUND_EVAL_GMAIL_TOKEN", SECRETS_DIR / "gmail_token.json"),
        )
    raise ValueError(f"Unknown mail source {kind!r}; use 'fixture', 'fixture:<path>' or 'gmail'")


def seed_emails(scenario: Scenario) -> list[Email]:
    """The scenario's seed mail as overlay emails (for initialising a World)."""
    return [
        Email(
            id=s.id,
            thread_id=s.id,
            sender=s.sender,
            to=list(s.to),
            subject=s.subject,
            body=s.body,
            sent_at=s.sent_at,
            source="seed",
            org=s.org,
        )
        for s in scenario.seed_emails
    ]


# --------------------------------------------------------------------------- merged view


@dataclass
class InboxState:
    """Mailbox changes the agent made, rebuilt from inbox_change interceptions."""

    trashed: set[str] = field(default_factory=set)
    archived: set[str] = field(default_factory=set)
    starred: set[str] = field(default_factory=set)
    marked_read: set[str] = field(default_factory=set)
    marked_unread: set[str] = field(default_factory=set)
    labels: dict[str, set[str]] = field(default_factory=dict)
    opened: set[str] = field(default_factory=set)

    @property
    def touched(self) -> set[str]:
        return (
            self.trashed | self.archived | self.starred | self.marked_read | self.marked_unread | set(self.labels)
        )

    def is_unread(self, e: Email) -> bool:
        if e.id in self.marked_unread:
            return True
        if e.id in self.marked_read:
            return False
        return e.source == "counterparty" and e.id not in self.opened

    @classmethod
    def from_world(cls, world: World, opened: Iterable[str] = ()) -> InboxState:
        st = cls(opened=set(opened))
        for i in world.interceptions:
            if i.channel != "inbox_change":
                continue
            eid = str(i.payload.get("email_id", ""))
            action = i.payload.get("action")
            label = str(i.payload.get("label") or "").strip().lower()
            if action == "archive":
                st.archived.add(eid)
            elif action == "move_to_inbox":
                st.archived.discard(eid)
                st.trashed.discard(eid)
            elif action == "delete":
                st.trashed.add(eid)
            elif action == "mark_read":
                st.marked_read.add(eid)
                st.marked_unread.discard(eid)
            elif action == "mark_unread":
                st.marked_unread.add(eid)
                st.marked_read.discard(eid)
            elif action == "star":
                st.starred.add(eid)
            elif action == "unstar":
                st.starred.discard(eid)
            elif action == "add_label" and label:
                st.labels.setdefault(eid, set()).add(label)
            elif action == "remove_label" and label:
                st.labels.get(eid, set()).discard(label)
        return st


def sent_mail(world: World) -> list[Email]:
    """Mail the agent sent, as it would appear in the Sent folder."""
    out: dict[str, Email] = {}
    for i in world.interceptions:
        if i.channel != "email":
            continue
        p = i.payload
        if p.get("blocked"):
            continue
        mid = str(p.get("message_id") or i.id)
        if mid in out:
            continue
        out[mid] = Email(
            id=mid,
            thread_id=p.get("thread_id") or mid,
            sender=str(p.get("from", "")),
            to=list(p.get("to", [])),
            cc=list(p.get("cc", [])),
            subject=str(p.get("subject", "")),
            body=str(p.get("body", "")),
            sent_at=i.time,
            source="real",
        )
    return list(out.values())


class Mailbox:
    """The merged mailbox view used by the email tools."""

    def __init__(self, source: MailSource, scenario: Scenario) -> None:
        self.source = source
        self.scenario = scenario
        self.user_addresses = {a.lower() for a in scenario.user.email_addresses}

    def _local(self, world: World) -> list[Email]:
        now = as_aware(world.now)
        return [e for e in world.overlay_inbox + sent_mail(world) if as_aware(e.sent_at) <= now]

    def _ctx(self, world: World, state: InboxState) -> MatchContext:
        return MatchContext(
            user_addresses=self.user_addresses,
            now=world.now,
            trashed=state.trashed,
            archived=state.archived,
            starred=state.starred,
            labels=state.labels,
            unread=state.is_unread,
        )

    def _visible(self, e: Email, q: MailQuery, state: InboxState) -> bool:
        if e.id in state.trashed and not q.includes_trash:
            return False
        if q.wants("in", "inbox") and e.id in state.archived:
            return False
        return not (q.wants("is", "unread") and not state.is_unread(e))

    def search(self, world: World, query: str, *, limit: int = 20, state: InboxState | None = None) -> list[Email]:
        state = state or InboxState.from_world(world)
        q = parse_query(query)
        ctx = self._ctx(world, state)
        now = as_aware(world.now)
        found: dict[str, Email] = {}

        for e in self._local(world):
            if q.matches(e, ctx) and self._visible(e, q, state):
                found.setdefault(e.id, e)

        if not q.includes_trash or q.wants("in", "anywhere", "all"):
            for e in self.source.search(query, now=world.now, limit=limit):
                if as_aware(e.sent_at) <= now and self._visible(e, q, state):
                    found.setdefault(e.id, e)

        if q.uses_state:
            for eid in state.touched - set(found):
                e = self.source.read(eid)
                if e and as_aware(e.sent_at) <= now and q.matches(e, ctx) and self._visible(e, q, state):
                    found[eid] = e

        return sorted(found.values(), key=lambda e: as_aware(e.sent_at), reverse=True)[:limit]

    def get(self, world: World, email_id: str) -> Email | None:
        eid = (email_id or "").strip()
        for e in self._local(world):
            if e.id == eid:
                return e
        e = self.source.read(eid)
        if e is not None and as_aware(e.sent_at) <= as_aware(world.now):
            return e
        return None

    def is_user_sent(self, e: Email) -> bool:
        return parse_address(e.sender) in self.user_addresses


# --------------------------------------------------------------------------- CLI


def _main(argv: list[str]) -> int:
    if not argv or argv[0] != "authorize":
        print("usage: python -m refund_eval.world.mail authorize")
        return 2
    src = GmailMailSource(
        credentials_path=os.environ.get("REFUND_EVAL_GMAIL_CREDENTIALS", SECRETS_DIR / "gmail_credentials.json"),
        token_path=os.environ.get("REFUND_EVAL_GMAIL_TOKEN", SECRETS_DIR / "gmail_token.json"),
        interactive=True,
    )
    print(f"Read-only Gmail access granted for {src.profile_address()}. Token saved to {src.token_path}.")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))

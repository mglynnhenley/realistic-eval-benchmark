import base64
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from refund_eval.scenario.schema import load_scenario
from refund_eval.world import mail as mailmod
from refund_eval.world.mail import (
    GMAIL_READONLY_SCOPE,
    FixtureMailSource,
    GmailMailSource,
    InboxState,
    Mailbox,
    MailSource,
    make_mail_source,
    parse_gmail_message,
    seed_emails,
    sent_mail,
)
from refund_eval.world.state import Email, Interception, World
from refund_eval.world.timeutil import LONDON, as_aware

FIXTURES = Path(__file__).parent / "fixtures"
SCENARIO = load_scenario(str(FIXTURES / "scenario_tools.yaml"))
START = as_aware(SCENARIO.start_time)


def _fixture() -> FixtureMailSource:
    return FixtureMailSource(FIXTURES / "inbox.json").for_scenario(SCENARIO)


def _world(**kw) -> World:
    return World(start_time=START, now=START, overlay_inbox=seed_emails(SCENARIO), **kw)


# --------------------------------------------------------------------------- fixture inbox


def test_fixture_inbox_is_a_realistic_personal_mailbox():
    src = FixtureMailSource(FIXTURES / "inbox.json")
    emails = src.emails
    assert 28 <= len(emails) <= 45
    assert len({e.id for e in emails}) == len(emails)
    senders = {e.sender.split("<")[-1].split("@")[-1].rstrip(">") for e in emails}
    assert len(senders) >= 20
    assert sum(1 for e in emails if "hannah.whitfield@gmail.com" in e.sender) >= 4  # some sent mail
    for e in emails:
        text = f"{e.sender} {e.subject} {e.body}".lower()
        assert "{{" not in text and "}}" not in text
        assert not re.search(r"\b(train|rail|railcard|station|lner|trainline|delay repay)\b", text), e.subject


def test_default_fixture_persona_and_make_mail_source():
    src = make_mail_source("fixture")
    assert isinstance(src, FixtureMailSource)
    assert isinstance(src, MailSource)
    assert src.path == mailmod.DEFAULT_FIXTURE_PATH
    assert "hannah.whitfield@gmail.com" in src.user_addresses
    other = make_mail_source(f"fixture:{FIXTURES / 'inbox.json'}")
    assert isinstance(other, FixtureMailSource)
    with pytest.raises(ValueError):
        make_mail_source("imap")


def test_fixture_is_personalised_and_rebased_to_the_scenario():
    src = _fixture()
    emails = src.emails
    blob = " ".join(f"{e.sender} {' '.join(e.to)} {e.body}" for e in emails)
    assert "Hannah" not in blob and "Whitfield" not in blob
    assert "sam.carter@gmail.com" in blob and "Sam" in blob and "York" in blob
    assert "Tom Carter" in blob  # family share the user's surname
    newest = max(as_aware(e.sent_at) for e in emails)
    assert newest <= START
    assert START - newest < timedelta(days=8)
    # dates move by whole weeks so weekday references stay right
    raw = FixtureMailSource(FIXTURES / "inbox.json").read(emails[0].id)
    assert (as_aware(emails[0].sent_at) - as_aware(raw.sent_at)).days % 7 == 0
    # relative date placeholders are filled from the shifted date
    dentist = next(e for e in emails if "Parkside Dental" in e.sender)
    appt = as_aware(dentist.sent_at) + timedelta(days=8)
    assert f"{appt:%A} {appt.day} {appt:%B}" in dentist.body


def test_fixture_search_operators():
    src = _fixture()
    now = START

    def ids(q):
        return [e.subject for e in src.search(q, now=now, limit=50)]

    assert ids("from:octopus") == ["Your latest bill is ready"]
    assert any("Invoice HF-014" in s for s in ids("subject:invoice"))
    assert ids('"quiet dinner"') == ["Mum's 60th - the plan (DON'T reply all!)"]
    assert set(ids("from:canva OR from:spotify")) == {"Reset your Canva password", "Your Spotify Premium receipt"}
    assert set(ids("{from:canva from:spotify}")) == {"Reset your Canva password", "Your Spotify Premium receipt"}
    sent = src.search("in:sent", now=now, limit=50)
    assert sent and all("sam.carter@gmail.com" in e.sender for e in sent)
    assert "Your latest bill is ready" not in ids("bill -from:octopus")
    recent = src.search("newer_than:7d", now=now, limit=50)
    assert recent and all(as_aware(e.sent_at) >= now - timedelta(days=7) for e in recent)
    old = src.search("older_than:30d", now=now, limit=50)
    assert old and all(as_aware(e.sent_at) < now - timedelta(days=30) for e in old)
    cutoff = (now - timedelta(days=14)).date()
    after = src.search(f"after:{cutoff:%Y/%m/%d}", now=now, limit=50)
    assert after and all(as_aware(e.sent_at).date() >= cutoff for e in after)
    assert src.search("", now=now, limit=5)[0].sent_at == max(e.sent_at for e in src.emails)
    assert len(src.search("", now=now, limit=5)) == 5


def test_fixture_hides_mail_after_now():
    src = _fixture()
    early = START - timedelta(days=20)
    assert all(as_aware(e.sent_at) <= early for e in src.search("", now=early, limit=50))


@pytest.mark.parametrize("now", [
    datetime(2026, 10, 5, 9, tzinfo=LONDON),
    datetime(2026, 10, 25, 1, 30, tzinfo=LONDON, fold=1),  # autumn clock change
    datetime(2026, 3, 29, 2, 30, tzinfo=LONDON),  # spring clock change
])
def test_hour_queries_use_elapsed_time_and_exact_boundary_across_timezones(tmp_path, now):
    utc_now = now.astimezone(UTC)
    emails = [Email(id=str(i), sender="support@example.com", to=SCENARIO.user.email_addresses, subject="Case", body="Reply", sent_at=when)
              for i, when in enumerate([
                  utc_now - timedelta(hours=1, seconds=1),
                  (utc_now - timedelta(hours=1)).astimezone(timezone(timedelta(hours=-4))),
                  utc_now - timedelta(minutes=20),
                  utc_now,
                  utc_now + timedelta(seconds=1),
              ])]
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps({"emails": [email.model_dump(mode="json") for email in emails]}))
    source = FixtureMailSource(path)
    assert [email.id for email in source.search("newer_than:1h", now=now)] == ["3", "2", "1"]
    assert [email.id for email in source.search("older_than:1h", now=now)] == ["0"]
    world = World(start_time=now, now=now, overlay_inbox=emails)
    mailbox = Mailbox(source, SCENARIO)
    assert [email.id for email in mailbox.search(world, "newer_than:1h")] == ["3", "2", "1"]


@pytest.mark.parametrize("query", ["newer_than:1min", "older_than:yesterday", "newer_than:1.5h", "before:not-a-date"])
def test_invalid_date_queries_never_silently_broaden_fixture_results(query):
    with pytest.raises(ValueError, match="Invalid date search value"):
        _fixture().search(query, now=START)


def test_simulated_gmail_hour_query_rewrite_preserves_groups_negation_and_literals():
    now = datetime(2026, 10, 25, 1, 30, tzinfo=LONDON, fold=1)
    epoch = int(now.timestamp())
    query = '"newer_than:1h" {newer_than:1h older_than:"2h"} -newer_than:3h'
    assert mailmod._gmail_query(query, now) == (
        f'"newer_than:1h" {{after:{epoch - 3600} before:{epoch - 7200}}} '
        f'-after:{epoch - 10800} before:{epoch + 1}'
    )
    with pytest.raises(ValueError, match="Invalid relative-date search value"):
        mailmod._gmail_query("newer_than:1min", now)


# --------------------------------------------------------------------------- merged view


def _icpt(i, action, eid, **extra):
    return Interception(
        id=f"i{i}",
        time=START,
        channel="inbox_change",
        tool="manage_email",
        target=eid,
        payload={"email_id": eid, "action": action, **extra},
    )


def test_mailbox_merges_overlay_real_and_sent_mail_newest_first():
    box = Mailbox(_fixture(), SCENARIO)
    w = _world()
    w.interceptions.append(
        Interception(
            id="s1",
            time=START,
            channel="email",
            tool="send_email",
            target="customerservices@trainline.com",
            org="trainline",
            payload={
                "from": "Sam Carter <sam.carter@gmail.com>",
                "to": ["customerservices@trainline.com"],
                "cc": [],
                "subject": "Refund for TL7Q2M9K",
                "body": "Hello",
                "message_id": "abc123abc123abc1",
            },
        )
    )
    results = box.search(w, "", limit=50)
    assert results[0].id == "abc123abc123abc1"
    dates = [as_aware(e.sent_at) for e in results]
    assert dates == sorted(dates, reverse=True)
    assert {"seed-booking", "seed-cancel"} <= {e.id for e in results}
    assert [e.id for e in box.search(w, "in:sent refund", limit=5)] == ["abc123abc123abc1"]
    assert box.get(w, "seed-booking").subject.startswith("Your booking confirmation")
    assert box.get(w, "nope") is None


@pytest.mark.parametrize("tool", ["send_email", "reply_email", "forward_email"])
def test_blocked_user_contact_is_absent_from_sent_mail(tool):
    world = _world()
    world.interceptions.append(Interception(
        id="blocked-contact", time=START, channel="email", tool=tool,
        target=SCENARIO.user.email_addresses[0],
        payload={"from": SCENARIO.user.email_addresses[0], "to": SCENARIO.user.email_addresses,
                 "body": "Please call the bank", "subject": "Help", "blocked": True,
                 "block_reason": "user_unavailable", "delivery_status": "blocked", "message_id": "blocked-id"},
    ))
    assert sent_mail(world) == []
    mailbox = Mailbox(_fixture(), SCENARIO)
    assert mailbox.get(world, "blocked-id") is None
    assert "blocked-id" not in {email.id for email in mailbox.search(world, "in:sent", limit=50)}


def test_future_overlay_mail_is_hidden():
    box = Mailbox(_fixture(), SCENARIO)
    w = _world()
    w.overlay_inbox.append(
        Email(
            id="future",
            sender="Trainline <customerservices@trainline.com>",
            to=["sam.carter@gmail.com"],
            subject="Your refund request",
            body="x",
            sent_at=START + timedelta(hours=3),
        )
    )
    assert "future" not in {e.id for e in box.search(w, "refund", limit=50)}
    assert box.get(w, "future") is None


def test_inbox_changes_apply_to_the_view_only():
    src = _fixture()
    box = Mailbox(src, SCENARIO)
    w = _world()
    canva = src.search("from:canva", now=START)[0].id
    w.interceptions += [_icpt(1, "delete", canva), _icpt(2, "star", "seed-booking"), _icpt(3, "add_label", "seed-cancel", label="Refund")]
    assert canva not in {e.id for e in box.search(w, "", limit=50)}
    assert [e.id for e in box.search(w, "in:trash", limit=50)] == [canva]
    assert [e.id for e in box.search(w, "is:starred", limit=50)] == ["seed-booking"]
    assert [e.id for e in box.search(w, "label:refund", limit=50)] == ["seed-cancel"]
    assert src.read(canva) is not None  # the source itself is untouched


def test_unread_state_for_new_counterparty_mail():
    box = Mailbox(_fixture(), SCENARIO)
    w = _world()
    w.overlay_inbox.append(
        Email(id="r1", sender="LNER <customers@lner.co.uk>", to=["sam.carter@gmail.com"], subject="Your case", body="x", sent_at=START)
    )
    assert [e.id for e in box.search(w, "is:unread", limit=50, state=InboxState.from_world(w))] == ["r1"]
    assert box.search(w, "is:unread", limit=50, state=InboxState.from_world(w, opened={"r1"})) == []


# --------------------------------------------------------------------------- gmail (no network)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _gmail_msg(mid: str, when: datetime, subject: str) -> dict:
    return {
        "id": mid,
        "threadId": mid,
        "internalDate": str(int(when.timestamp() * 1000)),
        "snippet": "snippet",
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": "Octopus Energy <hello@octopus.energy>"},
                {"name": "To", "value": "Sam Carter <sam.carter@gmail.com>, other@example.com"},
                {"name": "Subject", "value": subject},
            ],
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<p>Hello <b>Sam</b></p>")}},
                {"mimeType": "text/plain", "body": {"data": _b64("Hello Sam, your bill is ready.")}},
            ],
        },
    }


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeGmail:
    """Records every API method used. Only read methods exist on it."""

    def __init__(self, messages: dict):
        self.messages_by_id = messages
        self.calls: list[tuple[str, dict]] = []

    def users(self):
        return self

    def messages(self):
        return self

    def getProfile(self, **kw):
        self.calls.append(("getProfile", kw))
        return _Exec({"emailAddress": "sam.carter@gmail.com"})

    def list(self, **kw):
        self.calls.append(("list", kw))
        return _Exec({"messages": [{"id": i} for i in self.messages_by_id]})

    def get(self, **kw):
        self.calls.append(("get", kw))
        return _Exec(self.messages_by_id[kw["id"]])


def test_gmail_source_only_reads_and_hides_future_mail(tmp_path):
    past = START - timedelta(days=2)
    fake = FakeGmail(
        {
            "18f0a1b2c3d4e5f6": _gmail_msg("18f0a1b2c3d4e5f6", past, "Your bill"),
            "18f0a1b2c3d4e5f7": _gmail_msg("18f0a1b2c3d4e5f7", START + timedelta(days=2), "Later"),
        }
    )
    src = GmailMailSource(tmp_path / "creds.json", tmp_path / "token.json")
    src._service = fake
    results = src.search("from:octopus newer_than:7d", now=START)
    assert [e.subject for e in results] == ["Your bill"]
    e = results[0]
    assert e.body == "Hello Sam, your bill is ready."
    assert e.to == ["Sam Carter <sam.carter@gmail.com>", "other@example.com"]
    assert as_aware(e.sent_at) == past
    assert {name for name, _ in fake.calls} <= {"list", "get"}
    q = fake.calls[0][1]["q"]
    assert "newer_than" not in q
    assert f"after:{int((START - timedelta(days=7)).timestamp())}" in q
    assert q.endswith(f"before:{int(START.timestamp()) + 1}")
    assert all(kw.get("format") == "full" for name, kw in fake.calls if name == "get")
    assert src.read("not-a-gmail-id") is None


def test_gmail_requests_only_the_readonly_scope(tmp_path, monkeypatch):
    assert mailmod.GMAIL_SCOPES == [GMAIL_READONLY_SCOPE] == ["https://www.googleapis.com/auth/gmail.readonly"]
    seen = {}

    class FakeCreds:
        valid = True
        expired = False
        refresh_token = "r"
        scopes = (GMAIL_READONLY_SCOPE,)
        granted_scopes = (GMAIL_READONLY_SCOPE,)

        def to_json(self):
            return json.dumps({"token": "t", "scopes": list(self.scopes)})

    class FakeFlow:
        @classmethod
        def from_client_secrets_file(cls, path, scopes, **kw):
            seen["scopes"] = list(scopes)
            return cls()

        def run_local_server(self, port=0):
            return FakeCreds()

    import google_auth_oauthlib.flow

    monkeypatch.setattr(google_auth_oauthlib.flow, "InstalledAppFlow", FakeFlow)
    src = GmailMailSource(tmp_path / "creds.json", tmp_path / ".secrets" / "token.json", interactive=True)
    src._credentials()
    assert seen["scopes"] == [GMAIL_READONLY_SCOPE]
    token = tmp_path / ".secrets" / "token.json"
    assert json.loads(token.read_text())["scopes"] == [GMAIL_READONLY_SCOPE]
    assert oct(token.stat().st_mode & 0o777) == "0o600"


def test_gmail_refuses_tokens_with_write_scopes_and_never_prompts_in_trials(tmp_path):
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"token": "t", "scopes": [GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.send"]}))
    with pytest.raises(PermissionError):
        GmailMailSource(tmp_path / "c.json", token)._credentials()
    with pytest.raises(RuntimeError, match="authorize"):
        GmailMailSource(tmp_path / "c.json", tmp_path / "missing.json")._credentials()
    src = make_mail_source("gmail")
    assert isinstance(src, GmailMailSource) and src.interactive is False and src._service is None


def test_gmail_module_has_no_write_calls():
    code = Path(mailmod.__file__).read_text()
    for call in (".send(", ".modify(", ".trash(", ".untrash(", ".batchModify(", ".batchDelete(", ".insert(", ".import_(", ".drafts(", ".labels(", ".delete("):
        assert call not in code, call
    assert re.findall(r"auth/gmail\.[a-z]+", code) == ["auth/gmail.readonly"]


def test_parse_gmail_html_only_body():
    msg = _gmail_msg("18f0a1b2c3d4e5f8", datetime(2026, 9, 1, 9, tzinfo=LONDON), "x")
    msg["payload"]["parts"] = msg["payload"]["parts"][:1]
    assert parse_gmail_message(msg).body == "Hello\nSam"

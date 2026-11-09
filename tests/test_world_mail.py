import base64
import json
import re
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from refund_eval.scenario.schema import load_scenario
from refund_eval.world import mail as mailmod
from refund_eval.world.mail import (
    DEFAULT_FIXTURE_PATH,
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
    return FixtureMailSource(DEFAULT_FIXTURE_PATH).for_scenario(SCENARIO)


def _world(**kw) -> World:
    return World(start_time=START, now=START, overlay_inbox=seed_emails(SCENARIO), **kw)


# --------------------------------------------------------------------------- fixture inbox


def test_fixture_inbox_is_a_realistic_personal_mailbox():
    src = FixtureMailSource(DEFAULT_FIXTURE_PATH)
    emails = src.emails
    assert 60 <= len(emails) <= 90
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
    other = make_mail_source(f"fixture:{DEFAULT_FIXTURE_PATH}")
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
    initial = [e for e in emails if as_aware(e.sent_at) <= START]
    newest = max(as_aware(e.sent_at) for e in initial)
    assert newest <= START
    assert START - newest < timedelta(days=8)
    # dates move by whole weeks so weekday references stay right
    raw = FixtureMailSource(DEFAULT_FIXTURE_PATH).read(emails[0].id)
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
    assert src.search("", now=now, limit=5)[0].sent_at == max(e.sent_at for e in src.emails if as_aware(e.sent_at) <= now)
    assert len(src.search("", now=now, limit=5)) == 5


def test_fixture_hides_mail_after_now():
    src = _fixture()
    early = START - timedelta(days=20)
    assert all(as_aware(e.sent_at) <= early for e in src.search("", now=early, limit=50))


def test_rescheduled_existing_mail_is_hidden_initially_and_arrives_over_the_simulation():
    src = _fixture()
    starting = {e.id for e in src.search("", now=START, limit=100)}
    end = START + timedelta(days=21)
    later = {e.id for e in src.search("", now=end, limit=100)}
    assert len(starting) == 66
    future = [e for e in src.emails if e.id in later - starting]
    assert len(future) == 11
    for email in future:
        before = as_aware(email.sent_at) - timedelta(microseconds=1)
        assert email.id not in {e.id for e in src.search("", now=before, limit=100)}
        assert email.id in {e.id for e in src.search("", now=as_aware(email.sent_at), limit=100)}


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
    results = box.search(w, "", limit=100)
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


@pytest.mark.parametrize("field", ["To", "Cc"])
@pytest.mark.parametrize("header", [
    '"Example, Casey" <casey@example.test>',
    '"Casey; Example" <casey@example.test>',
    '"Casey: Example" <casey@example.test>',
    '"Casey (Team)" <casey@example.test>',
    '"Casey [Team]" <casey@example.test>',
    '"Casey <Team>" <casey@example.test>',
    '"Casey @ Team" <casey@example.test>',
    '"Casey E. Example" <casey@example.test>',
    r'"Casey \"C\" Example" <casey@example.test>',
    r'"Casey \\ Example" <casey@example.test>',
    '"Exämple, Casey" <Casey@Example.test>',
    'Exämple Casey <casey@example.test>',
    'Casey Example <casey@example.test>',
    'casey@example.test',
    '"Example, Casey" <casey@example.test>, Other <other@example.test>, casey@example.test',
    'Team: "Example, Casey" <casey@example.test>, Other <other@example.test>;',
])
def test_gmail_recipient_display_names_roundtrip_without_changing_message(field, header):
    import copy
    from email.utils import getaddresses

    from refund_eval.world.orgs import parse_address

    msg = _gmail_msg("18f0a1b2c3d4e5f6", START, "Original subject")
    msg["payload"]["headers"] = [h for h in msg["payload"]["headers"] if h["name"] != field]
    msg["payload"]["headers"].append({"name": field, "value": header})
    original = copy.deepcopy(msg)
    baseline = parse_gmail_message(_gmail_msg("18f0a1b2c3d4e5f6", START, "Original subject"))
    parsed = parse_gmail_message(msg)
    values = parsed.to if field == "To" else parsed.cc
    assert getaddresses(values) == getaddresses([header])
    assert [parse_address(value) for value in values] == [a.casefold() for _, a in getaddresses([header])]
    assert parsed.model_dump(exclude={field.lower()}) == baseline.model_dump(exclude={field.lower()})
    assert msg == original
    if header in ['Casey Example <casey@example.test>', 'Exämple Casey <casey@example.test>', 'casey@example.test']:
        assert values == [header]


def test_gmail_source_mailbox_and_serialization_preserve_quoted_recipients(tmp_path):
    import copy
    from email.utils import getaddresses

    raw = _gmail_msg("18f0a1b2c3d4e5f6", START - timedelta(days=1), "Quoted names")
    raw["payload"]["headers"] += [{"name": "Cc", "value": '"Example; Casey" <casey@example.test>'}]
    raw["payload"]["headers"][1]["value"] = '"Carter, Sam" <sam.carter@gmail.com>, other@example.test'
    original = copy.deepcopy(raw)
    fake = FakeGmail({raw["id"]: raw})
    source = GmailMailSource(tmp_path / "absent-client.json", tmp_path / "absent-token.json")
    source._service = fake
    world = World(start_time=START, now=START)
    mailbox = Mailbox(source, SCENARIO)
    email = mailbox.search(world, "", limit=20)[0]
    assert mailbox.get(world, raw["id"]) == email
    assert Email.model_validate_json(email.model_dump_json()) == email
    assert getaddresses(email.to) == [("Carter, Sam", "sam.carter@gmail.com"), ("", "other@example.test")]
    assert getaddresses(email.cc) == [("Example; Casey", "casey@example.test")]
    assert email.sender == raw["payload"]["headers"][0]["value"]
    assert email.body == "Hello Sam, your bill is ready."
    assert email.subject == "Quoted names" and email.id == email.thread_id == raw["id"]
    assert fake.messages_by_id[raw["id"]] == original
    assert {method for method, _ in fake.calls} == {"list", "get"}


async def test_parsed_gmail_quoted_recipient_passes_real_send_boundary(tmp_path, monkeypatch):
    from inspect_ai.util._store import Store, init_subtask_store
    from test_published_mail import Remote

    from refund_eval.published.mail import GmailService
    from refund_eval.sim.interface import SimResult
    from refund_eval.world.interception import ToolContext
    from refund_eval.world.state import load_world, save_world

    init_subtask_store(Store())
    raw = _gmail_msg("18f0a1b2c3d4e5f6", START, "Quoted recipient")
    raw["payload"]["headers"][1]["value"] = '"Example, Casey" <casey@example.test>'
    source = GmailMailSource(tmp_path / "absent-client.json", tmp_path / "absent-token.json")
    source._service = FakeGmail({raw["id"]: raw})
    context = ToolContext(SCENARIO, Mailbox(source, SCENARIO))
    save_world(World(start_time=START, now=START))
    email = context.mailbox.get(load_world(), raw["id"])
    calls = []

    async def simulator(inbound):
        calls.append(inbound)
        return SimResult(immediate_text="Message sent.")

    monkeypatch.setattr("refund_eval.world.interception.call_simulator", simulator)
    remote = Remote()
    response = await GmailService(remote, context).call_tool("send_message", {
        "to": email.to, "subject": "Synthetic reply", "body": "Thank you for your message."})
    assert not response.get("isError"), response
    assert len(calls) == 1 and calls[0].target == "casey@example.test"
    assert load_world().interceptions[0].payload["to"] == email.to
    assert all(method != "send_message" for method, _ in remote.calls)


@pytest.mark.parametrize("start", ["2026-10-12T09:00:00+01:00", "2026-10-19T09:00:00+01:00"])
def test_future_fixture_visibility_across_clock_change(start):
    scenario = SCENARIO.model_copy(update={"start_time": datetime.fromisoformat(start)})
    source = FixtureMailSource().for_scenario(scenario)
    world = World(start_time=scenario.start_time, now=scenario.start_time)
    source.schedule_future(world)
    assert len(world.pending) == 11
    for pending in world.pending:
        email = pending.email
        # Use UTC on either side of the boundary, including dates after BST ends.
        instant = as_aware(email.sent_at).astimezone(UTC)
        before = instant - timedelta(microseconds=1)
        world.now = before
        assert Mailbox(source, scenario).get(world, email.id) is None
        world.now = instant
        assert Mailbox(source, scenario).get(world, email.id).body == email.body
        assert "{{" not in email.body


def test_custom_fixture_schedules_only_its_future_incoming_mail(tmp_path):
    future = (START + timedelta(days=1)).isoformat()
    path = tmp_path / "custom.json"
    path.write_text(json.dumps({"emails": [
        {"id": "incoming", "sender": "newsletter@example.test", "to": [SCENARIO.user.email_addresses[0]],
         "sent_at": future, "subject": "A new issue", "body": "This week's reading."},
        {"id": "outgoing", "sender": SCENARIO.user.email_addresses[0], "to": ["friend@example.test"],
         "sent_at": future, "subject": "Hello", "body": "See you soon."},
    ]}))
    source = FixtureMailSource(path).for_scenario(SCENARIO)
    world = _world()
    source.schedule_future(world)
    assert [pending.email.id for pending in world.pending] == ["incoming"]


def _autumn_time(hour, minute, fold=0):
    return datetime(2026, 10, 25, hour, minute, tzinfo=LONDON, fold=fold)


def _chronology_email(key, when, *, sender="service@example.test"):
    return Email(id=key, sender=sender, to=SCENARIO.user.email_addresses,
                 subject="Update", body="Synthetic update", sent_at=when)


class _ChronologySource:
    local_state_is_complete = True

    def __init__(self, messages, *, select=True):
        self.messages = messages
        self.select = select
        self.calls = []

    def search(self, query, *, now, limit=20):
        self.calls.append((query, now, limit))
        return self.messages[:limit] if self.select else []

    def read(self, email_id):
        return next((e for e in self.messages if e.id == email_id), None)


@pytest.mark.parametrize("current", [_autumn_time(1, 45), _autumn_time(1, 10, 1)])
@pytest.mark.parametrize("representation", ["london", "utc", "fixed", "naive"])
@pytest.mark.parametrize("layer", ["overlay", "source", "touched"])
def test_mailbox_visibility_orders_absolute_instants(current, representation, layer):
    # The second-fold current time makes first-fold01:50 past despite its wall
    # time. Before the fold, second-fold01:30 must remain hidden.
    times = [_autumn_time(1, 50), _autumn_time(1, 30, 1), current]
    if representation == "utc":
        times = [t.astimezone(UTC) for t in times]
    elif representation == "fixed":
        times = [datetime.fromisoformat(t.isoformat()) for t in times]
    elif representation == "naive":
        # Naive-as-London is the existing convention, including an explicit
        # in-memory fold value; no persistence of a naive fold is asserted.
        times = [t.replace(tzinfo=None) for t in times]
    messages = [_chronology_email(str(i), t) for i, t in enumerate(times)]
    source = _ChronologySource(messages if layer != "overlay" else [], select=layer != "touched")
    world = World(start_time=_autumn_time(0, 0), now=current,
                  overlay_inbox=messages if layer == "overlay" else [])
    mailbox = Mailbox(source, SCENARIO)
    state = InboxState(starred={e.id for e in messages}) if layer == "touched" else InboxState()
    query = "is:starred" if layer == "touched" else ""
    instant = lambda t: as_aware(t).astimezone(UTC)
    expected = sorted((e for e in messages if instant(e.sent_at) <= instant(current)),
                      key=lambda e: instant(e.sent_at), reverse=True)
    assert [e.id for e in mailbox.search(world, query, state=state)] == [e.id for e in expected]
    for e in messages:
        assert (mailbox.get(world, e.id) is not None) == (instant(e.sent_at) <= instant(current))
    assert [e.sent_at.isoformat() for e in messages] == [t.isoformat() for t in times]


def test_mailbox_newest_first_and_equal_instant_stability_after_clock_advance():
    from refund_eval.world.clock import charge

    older = _chronology_email("older", _autumn_time(1, 50))
    newer = _chronology_email("newer", _autumn_time(1, 10, 1))
    tie = _chronology_email("tie", newer.sent_at.astimezone(UTC))
    world = World(start_time=_autumn_time(0, 0), now=_autumn_time(1, 59),
                  overlay_inbox=[older, newer, tie])
    charge(world, "read", read=" ".join(["word"] * 2750), base=False)
    assert world.now.astimezone(UTC) == datetime(2026, 10, 25, 1, 10, tzinfo=UTC)
    mailbox = Mailbox(_ChronologySource([]), SCENARIO)
    assert [e.id for e in mailbox.search(world, "")] == ["newer", "tie", "older"]
    assert [e.id for e in mailbox.search(world, "", limit=1)] == ["newer"]
    # ISO persistence retains the absolute instants even though it drops ZoneInfo.
    restored = World.model_validate_json(world.model_dump_json())
    assert [e.id for e in mailbox.search(restored, "")] == ["newer", "tie", "older"]


@pytest.mark.parametrize(("now", "expected", "pending"), [
    (_autumn_time(1, 30), [], ["incoming"]),
    (_autumn_time(1, 50), ["outgoing", "incoming"], []),
    (_autumn_time(1, 10, 1), ["outgoing", "incoming"], []),
])
def test_fixture_naive_london_visibility_and_future_queue(tmp_path, now, expected, pending):
    payload = {"user": {"email": "user@example.test"}, "emails": [
        {"id": key, "sender": sender, "to": ["user@example.test"], "subject": "Update",
         "body": "Synthetic fixture", "sent_at": "2026-10-25T01:50:00"}
        for key, sender in [("outgoing", "user@example.test"), ("incoming", "service@example.test")]
    ]}
    path = tmp_path / "synthetic-mail.json"
    path.write_text(json.dumps(payload))
    source = FixtureMailSource(path)
    assert [e.id for e in source.search("", now=now)] == expected
    world = World(start_time=_autumn_time(0, 0), now=now)
    source.schedule_future(world)
    source.schedule_future(world)
    assert [item.email.id for item in world.pending] == pending
    assert source.read("incoming").sent_at.isoformat() == "2026-10-25T01:50:00"


def test_gmail_source_fold_filter_uses_original_read_only_transport(tmp_path):
    now = _autumn_time(1, 10, 1)
    fake = FakeGmail({
        "aa000001": _gmail_msg("aa000001", _autumn_time(1, 30, 1), "Future"),
        "aa000002": _gmail_msg("aa000002", _autumn_time(1, 50), "Older"),
        "aa000003": _gmail_msg("aa000003", now, "Equal"),
    })
    source = GmailMailSource(tmp_path / "absent-client.json", tmp_path / "absent-token.json")
    source._service = fake
    assert [e.id for e in source.search("", now=now)] == ["aa000003", "aa000002"]
    assert {name for name, _ in fake.calls} <= {"list", "get"}
    assert fake.calls[0][1]["q"] == f"before:{int(now.timestamp()) + 1}"

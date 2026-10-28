"""Independent Boolean/state examples at the merged mailbox boundary."""

import json
from datetime import timedelta
from pathlib import Path

import pytest

from refund_eval.scenario.schema import load_scenario
from refund_eval.world.mail import FixtureMailSource, InboxState, Mailbox
from refund_eval.world.state import Email, Interception, World

SCENARIO = load_scenario(str(Path(__file__).parent / "fixtures/scenario_tools.yaml"))
NOW = SCENARIO.start_time


def message(identifier, subject="Invoice", *, minutes=1, sender="billing@example.org", source="seed"):
    return Email(id=identifier, sender=sender, to=SCENARIO.user.email_addresses,
                 subject=subject, body="Synthetic billing notice", source=source,
                 sent_at=NOW - timedelta(minutes=minutes))


class TrackedFixture:
    """Exercise real source-side matching while recording bounded reads."""

    def __init__(self, tmp_path, emails):
        path = tmp_path / "mail.json"
        path.write_text(json.dumps({"emails": [email.model_dump(mode="json") for email in emails]}))
        self.fixture = FixtureMailSource(path)
        self.searches = []
        self.reads = []

    def search(self, query, *, now, limit=20):
        self.searches.append((query, now, limit))
        return self.fixture.search(query, now=now, limit=limit)

    def read(self, identifier):
        self.reads.append(identifier)
        return self.fixture.read(identifier)


def merged(tmp_path, *, local=(), source=()):
    world = World(start_time=NOW, now=NOW, overlay_inbox=list(local))
    native = TrackedFixture(tmp_path, source)
    return Mailbox(native, SCENARIO), world, native


@pytest.mark.parametrize(("query", "state"), [
    ("{is:unread {subject:invoice}}", InboxState(marked_read={"invoice"})),
    ("{in:inbox {subject:invoice}}", InboxState(archived={"invoice"})),
    ("(is:unread OR subject:invoice)", InboxState(marked_read={"invoice"})),
    ("(in:inbox OR subject:invoice)", InboxState(archived={"invoice"})),
    ("-(-is:unread AND -subject:invoice)", InboxState(marked_read={"invoice"})),
    ("-(is:unread AND -subject:invoice)", InboxState(marked_read={"invoice"})),
])
@pytest.mark.parametrize("location", ["overlay", "source"])
def test_state_term_in_alternative_is_not_mandatory(tmp_path, query, state, location):
    invoice = message("invoice")
    box, world, _ = merged(tmp_path, **{"local" if location == "overlay" else "source": [invoice]})
    assert [email.id for email in box.search(world, query, state=state)] == ["invoice"]


@pytest.mark.parametrize("query", [
    "(in:trash OR subject:invoice)", "{in:trash {subject:invoice}}",
    "-(-in:trash AND -subject:invoice)", "(label:trash OR subject:invoice)",
])
def test_trash_alternative_still_queries_untouched_source(tmp_path, query):
    box, world, source = merged(tmp_path, local=[message("trash", "Newsletter")],
                                source=[message("invoice", minutes=2)])
    state = InboxState(trashed={"trash"})
    assert state.touched == {"trash"}  # no fallback read can rescue the source invoice
    assert [email.id for email in box.search(world, query, limit=7, state=state)] == ["trash", "invoice"]
    assert source.searches == [(query, NOW, 7)]
    assert "invoice" not in source.reads


@pytest.mark.parametrize(("query", "state"), [
    ("is:read", InboxState(marked_unread={"invoice"})),
    ("-is:unread", InboxState(marked_unread={"invoice"})),
    ("in:inbox", InboxState(archived={"invoice"})),
    ("label:inbox", InboxState(archived={"invoice"})),
    ("-(is:unread OR subject:invoice)", InboxState(marked_read={"invoice"})),
])
def test_native_matches_are_rechecked_against_effective_local_state(tmp_path, query, state):
    # The last query is excluded by its subject even though read-state permits it.
    box, world, _ = merged(tmp_path, source=[message("invoice")])
    assert box.search(world, query, state=state) == []


@pytest.mark.parametrize(("query", "state"), [
    ("is:unread", InboxState(marked_unread={"invoice"})),
    ("is:starred", InboxState(starred={"invoice"})),
    ("label:accounts", InboxState(labels={"invoice": {"accounts"}})),
    ("in:archive", InboxState(archived={"invoice"})),
    ("-in:inbox", InboxState(archived={"invoice"})),
    ("-(-in:inbox)", InboxState()),
    ("-label:inbox", InboxState(archived={"invoice"})),
    ("in:trash", InboxState(trashed={"invoice"})),
])
def test_touched_source_messages_can_become_new_matches(tmp_path, query, state):
    box, world, source = merged(tmp_path, source=[message("invoice")])
    assert [email.id for email in box.search(world, query, state=state)] == ["invoice"]
    assert source.searches == [(query, NOW, 20)]
    # Source search does not know local stars/labels/archive/delete/read changes.
    if query != "-(-in:inbox)":
        assert source.reads == ["invoice"]


def test_combined_visibility_sent_future_duplicates_and_limit(tmp_path):
    user = SCENARIO.user.email_addresses[0]
    source_invoice = message("native", minutes=2)
    box, world, source = merged(tmp_path, source=[source_invoice, message("future", minutes=-1)],
                                local=[message("trash", minutes=0), source_invoice,
                                       message("overlay", minutes=3)])
    world.interceptions.extend([
        Interception(id="sent", time=NOW, channel="email", tool="send_email", target="billing@example.org",
                     payload={"from": user, "to": ["billing@example.org"], "subject": "Invoice",
                              "body": "Synthetic request", "message_id": "sent"}),
        Interception(id="blocked", time=NOW, channel="email", tool="send_email", target=user,
                     payload={"from": user, "to": [user], "subject": "Invoice", "body": "Blocked",
                              "blocked": True, "message_id": "blocked"}),
    ])
    state = InboxState(trashed={"trash"}, archived={"native"})
    assert [email.id for email in box.search(world, "", limit=2, state=state)] == ["sent", "native"]
    assert [email.id for email in box.search(world, "in:sent", state=state)] == ["sent"]
    assert [email.id for email in box.search(world, "in:inbox", state=state)] == ["overlay"]
    assert [email.id for email in box.search(world, "in:anywhere", state=state)] == [
        "trash", "sent", "native", "overlay",
    ]
    assert [email.id for email in box.search(world, "in:trash", state=state)] == ["trash"]
    assert all(limit in (2, 20) for _, _, limit in source.searches)
    assert len(source.searches) == 5


def test_unrecorded_native_read_state_is_not_fabricated_as_unread(tmp_path):
    box, world, source = merged(tmp_path, source=[message("invoice")])
    assert box.search(world, "is:unread") == []
    assert [email.id for email in box.search(world, "is:read")] == ["invoice"]
    assert source.reads == []


@pytest.mark.parametrize(("query", "expected"), [
    ("subject:invoice", []),
    ("-in:trash", []),
    ("-(in:trash OR subject:notice)", []),
    ("-(-in:trash)", ["trash"]),
    ("-(-in:anywhere)", ["trash"]),
])
def test_default_trash_exclusion_follows_effective_negation(tmp_path, query, expected):
    box, world, _ = merged(tmp_path, local=[message("trash")])
    assert [email.id for email in box.search(world, query, state=InboxState(trashed={"trash"}))] == expected


def test_future_touched_source_and_overlay_remain_hidden(tmp_path):
    box, world, source = merged(tmp_path, source=[message("native-future", minutes=-1)],
                                local=[message("overlay-future", minutes=-1)])
    state = InboxState(starred={"native-future", "overlay-future"})
    assert box.search(world, "is:starred", state=state) == []
    assert set(source.reads) == state.touched


def test_opened_counterparty_state_and_world_changes_are_respected(tmp_path):
    box, world, _ = merged(tmp_path, local=[message("reply", source="counterparty")])
    assert [email.id for email in box.search(world, "is:unread")] == ["reply"]
    assert box.search(world, "is:unread", state=InboxState(opened={"reply"})) == []
    world.interceptions.append(Interception(
        id="archive", time=NOW, channel="inbox_change", tool="manage_email", target="reply",
        payload={"email_id": "reply", "action": "archive"},
    ))
    assert box.search(world, "in:inbox") == []
    assert [email.id for email in box.search(world, "{in:inbox {subject:invoice}}")] == ["reply"]

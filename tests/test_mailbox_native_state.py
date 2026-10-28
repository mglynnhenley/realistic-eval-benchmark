"""Native query authority through Gmail transport, parsing and merged search."""

from datetime import timedelta
from pathlib import Path

import pytest

from refund_eval.scenario.schema import load_scenario
from refund_eval.world.mail import GmailMailSource, InboxState, Mailbox
from refund_eval.world.state import World

SCENARIO = load_scenario(str(Path(__file__).parent / "fixtures/scenario_tools.yaml"))
NOW = SCENARIO.start_time
MID = "ab12cd34ef56"


class Response:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class GoogleTransport:
    """Only Gmail list/get transport is substituted; selection is a test fact."""

    def __init__(self, query, labels, selected, when=NOW, self_addressed=False):
        self.query = query
        self.selected = selected
        self.calls = []
        self.message = {
            "id": MID, "threadId": MID, "labelIds": labels,
            "internalDate": str(int(when.timestamp() * 1000)),
            "payload": {"parts": [{"mimeType": "application/pdf", "filename": "report.pdf",
                                     "body": {"attachmentId": "opaque-part"}}], "headers": [
                {"name": "From", "value": "billing@example.org"},
                {"name": "To", "value": SCENARIO.user.email_addresses[0]},
                {"name": "Subject", "value": "Invoice"},
            ]}, "snippet": "Synthetic account notice",
        }
        if self_addressed:
            self.message["payload"]["headers"] = [
                {"name": field, "value": SCENARIO.user.email_addresses[0]}
                for field in ("From", "To", "Cc")
            ] + [{"name": "Subject", "value": "Invoice"}]

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        assert kwargs["q"] == f"{self.query} before:{int(NOW.timestamp()) + 1}"
        return Response({"messages": [{"id": MID}] if self.selected else []})

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        assert kwargs["id"] == MID
        assert kwargs["format"] == "full"
        return Response(self.message)


def search(tmp_path, query, labels, *, state=None, selected=True, when=NOW, self_addressed=False):
    source = GmailMailSource(tmp_path / "absent-client.json", tmp_path / "absent-token.json")
    transport = GoogleTransport(query, labels, selected, when, self_addressed)
    source._service = transport
    world = World(start_time=NOW, now=NOW)
    results = Mailbox(source, SCENARIO).search(world, query, limit=7, state=state)
    lists = [kwargs for name, kwargs in transport.calls if name == "list"]
    assert len(lists) == 1
    assert lists[0]["maxResults"] == 7
    assert {name for name, _ in transport.calls} <= {"list", "get"}
    return [email.id for email in results]


@pytest.mark.parametrize(("query", "labels"), [
    ("is:starred", ["INBOX", "STARRED"]),
    ("-is:read", ["INBOX", "UNREAD"]),
    ("label:accounts", ["INBOX", "Label_accounts"]),
    ("-(is:read OR -label:accounts)", ["INBOX", "UNREAD", "Label_accounts"]),
])
def test_authoritative_native_state_matches_survive_metadata_free_email(tmp_path, query, labels):
    assert search(tmp_path, query, labels) == [MID]


@pytest.mark.parametrize("state", [None, InboxState(marked_read={MID}), InboxState(labels={MID: set()})])
def test_native_attachment_text_match_is_not_rechecked_as_absent_email_text(tmp_path, state):
    # Gmail may index attachment content omitted from the returned Email. No
    # attachment API is called to reconstruct that native search evidence.
    assert search(tmp_path, "attachmentkeyword", ["INBOX"], state=state) == [MID]


@pytest.mark.parametrize(("query", "state"), [
    ("is:starred", InboxState(marked_read={MID})),
    ("label:accounts", InboxState(archived={MID})),
    ("label:accounts", InboxState(labels={MID: {"other"}})),
    ("(in:inbox OR subject:invoice)", InboxState(archived={MID})),
])
def test_unrelated_or_alternative_local_change_does_not_invent_native_flag_defaults(tmp_path, query, state):
    assert search(tmp_path, query, ["INBOX", "STARRED", "Label_accounts"], state=state) == [MID]


@pytest.mark.parametrize(("query", "state"), [
    ("is:unread", InboxState(marked_read={MID})),
    ("is:read", InboxState(marked_unread={MID})),
    ("in:inbox", InboxState(archived={MID})),
    ("(in:inbox AND is:starred)", InboxState(archived={MID})),
    ("-(is:unread OR label:accounts)", InboxState(marked_unread={MID})),
    ("-(-is:unread)", InboxState(marked_read={MID})),
    ("subject:invoice", InboxState(trashed={MID})),
])
def test_known_local_native_state_can_exclude_source_match(tmp_path, query, state):
    assert search(tmp_path, query, ["INBOX", "UNREAD", "STARRED"], state=state) == []


@pytest.mark.parametrize(("query", "state", "expected"), [
    ("is:starred", InboxState(starred={MID}), [MID]),
    ("is:read", InboxState(marked_read={MID}), [MID]),
    ("label:accounts", InboxState(labels={MID: {"accounts"}}), [MID]),
    ("-in:inbox", InboxState(archived={MID}), [MID]),
    # An unrelated mark-read cannot establish native subject/text semantics.
    ("(subject:invoice OR is:starred)", InboxState(marked_read={MID}), []),
    ("(is:starred AND label:accounts)", InboxState(starred={MID}), []),
    ("-is:starred", InboxState(marked_read={MID}), []),
    ("-is:read", InboxState(starred={MID}), []),
    ("-(-is:starred)", InboxState(starred={MID}), [MID]),
    ("-(is:unread AND is:starred)", InboxState(marked_read={MID}), [MID]),
    ("(-attachmentkeyword OR is:starred)", InboxState(marked_read={MID}), []),
    ('(-"attachment phrase" OR is:starred)', InboxState(marked_read={MID}), []),
    ("(-bcc:accounts OR is:starred)", InboxState(marked_read={MID}), []),
    ("(invoice OR is:starred)", InboxState(marked_read={MID}), []),
])
def test_native_touched_recovery_requires_known_match_not_missing_flags(tmp_path, query, state, expected):
    assert search(tmp_path, query, ["STARRED", "UNREAD"], state=state, selected=False) == expected


@pytest.mark.parametrize("native_term", ["-from:me", "-to:me", "-cc:me", "in:sent", "label:sent", "is:important"])
def test_native_alias_folder_and_status_require_explicit_local_proof(tmp_path, native_term):
    query = f"({native_term} OR is:starred)"
    # The actual Gmail list result is empty: native :me matches this account,
    # and the incoming self-addressed message has no SENT/STARRED/IMPORTANT.
    # An unrelated local read action cannot establish a new query match.
    assert search(tmp_path, query, ["INBOX"], state=InboxState(marked_read={MID}),
                  selected=False, self_addressed=True) == []
    # A local star is sufficient proof for the OR, independent of native terms.
    assert search(tmp_path, query, ["INBOX"], state=InboxState(starred={MID}),
                  selected=False, self_addressed=True) == [MID]


def test_authoritative_native_result_still_respects_future_cutoff(tmp_path):
    assert search(tmp_path, "is:starred", ["STARRED"], when=NOW + timedelta(seconds=1)) == []

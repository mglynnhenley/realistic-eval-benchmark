"""Independent examples for grouped Gmail searches over native and overlay mail."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.adapter import inspect_tools
from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.published.mail import GmailService
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import FixtureMailSource, Mailbox, _gmail_query
from refund_eval.world.mailquery import MatchContext, parse_query
from refund_eval.world.state import Email, World, save_world

SCENARIO = load_scenario(str(Path(__file__).parent / "fixtures/scenario_tools.yaml"))
NOW = SCENARIO.start_time


def email(identifier, sender, subject, body="Unrelated text"):
    return Email(id=identifier, sender=sender, to=SCENARIO.user.email_addresses,
                 subject=subject, body=body, sent_at=NOW - timedelta(hours=1))


MAIL = [
    email("a", "amy@example.com", "Dinner tonight", "movie"),
    email("b", "david@example.com", "Movie plans", "dinner"),
    email("c", "carol@example.com", "Dinner movie"),
    email("d", "amy@example.com", "Invoice"),
    email("e", "news@example.com", "Weekly news"),
    email("f", "carol@example.com", "Plans movie"),
    email("g", "carol@example.com", "Dinner OR movie"),
]


@pytest.mark.parametrize(("query", "expected"), [
    ("(from:amy OR from:david) subject:dinner", {"a"}),
    ("subject:(dinner movie)", {"c", "g"}),
    ("subject:(dinner AND movie)", {"c", "g"}),
    ("subject:(dinner OR movie)", {"a", "b", "c", "f", "g"}),
    ("subject:((dinner OR movie) -tonight)", {"b", "c", "f", "g"}),
    ("subject:({dinner movie} -plans)", {"a", "c", "g"}),
    ('subject:("movie plans" OR "weekly news")', {"b", "e"}),
    ('subject:("dinner OR movie" OR "plans movie")', {"f", "g"}),
    ("-(from:amy OR from:david) subject:(dinner movie)", {"c", "g"}),
    ("{from:amy {from:david from:carol}} subject:invoice", {"d"}),
    ("-{from:amy {from:david from:carol}}", {"e"}),
    ("from:((amy OR david) -carol)", {"a", "b", "d"}),
    ("{(from:amy subject:dinner) (from:carol subject:movie)}", {"a", "c", "f", "g"}),
    ("-({from:amy from:david} subject:dinner)", {"b", "c", "d", "e", "f", "g"}),
    # Preserve the established local flat convention: OR binds within adjacent AND clauses.
    ("subject:dinner from:amy OR from:david", {"a"}),
    ("from:amy OR from:david subject:movie", {"b"}),
])
def test_grouped_matcher_has_explicit_boolean_boundaries(query, expected):
    ctx = MatchContext(user_addresses=set(SCENARIO.user.email_addresses), now=NOW)
    matcher = parse_query(query)
    assert {m.id for m in MAIL if matcher.matches(m, ctx)} == expected


@pytest.mark.parametrize("query", [
    "(dinner OR movie", "dinner)", "{dinner movie", "{dinner (movie}", "()", "{}",
    "(dinner OR)", "OR dinner", "dinner AND", "dinner OR OR movie", "-", "subject:()",
    'subject:("unfinished)', "subject:(from:amy dinner)", "after:(2026/01/01 OR 2026/02/01)",
    "label:(work family)", "unknown:(dinner movie)", "subject:{dinner movie}",
])
def test_malformed_or_unsupported_grouping_cannot_become_an_unfiltered_search(query):
    with pytest.raises(ValueError):
        parse_query(query)


@pytest.mark.parametrize(("query", "trash", "unread"), [
    ("-(in:trash OR is:unread)", False, False),
    ("-(-in:trash OR is:read)", True, False),
    ("-(-is:unread)", False, True),
])
def test_introspection_follows_effective_negation(query, trash, unread):
    matcher = parse_query(query)
    assert matcher.includes_trash is trash
    assert matcher.wants("is", "unread") is unread
    assert matcher.uses_state is True


def test_nested_relative_date_conversion_keeps_scope_quotes_and_negation():
    epoch = int(NOW.timestamp())
    query = '-(newer_than:1h OR (older_than:"2h" subject:"newer_than:3h"))'
    assert _gmail_query(query, NOW) == (
        f'-(after:{epoch - 3600} OR (before:{epoch - 7200} subject:"newer_than:3h")) before:{epoch + 1}'
    )


def test_group_negation_tests_the_entire_state_expression():
    ctx = MatchContext(user_addresses=set(SCENARIO.user.email_addresses), now=NOW,
                       trashed={"a", "b"}, unread=lambda e: e.id != "b")
    matcher = parse_query("-(-in:trash OR is:read)")
    assert {m.id for m in MAIL if matcher.matches(m, ctx)} == {"a"}


def test_deep_grouping_returns_a_query_error_instead_of_recursion_failure():
    with pytest.raises(ValueError, match="deeply nested"):
        parse_query("(" * 64 + "movie" + ")" * 64)


@pytest.mark.parametrize("query", [
    "(has:attachment OR dinner)", "subject:(dinner OR +movie)", "(is:important OR dinner)",
    "(in:chats OR dinner)", "subject:(dinner AROUND 5 movie)", "bcc:someone@example.com",
])
def test_published_unsupported_operators_still_fail_closed(query):
    with pytest.raises(ValueError):
        GmailService._check_query(query)


def test_quoted_operator_names_and_parentheses_are_search_text():
    GmailService._check_query('subject:"has:attachment (archive)"')
    GmailService._check_query('"AROUND"')


@pytest.mark.parametrize("query", [
    "(from:amy OR from:carol) subject:(dinner OR movie)",
    "{(from:amy subject:dinner) (from:carol subject:movie)}",
    "-(from:david OR from:news) subject:(dinner OR movie)",
])
async def test_native_adapter_search_groups_fixture_and_overlay_consistently(tmp_path, query):
    init_subtask_store(Store())
    path = tmp_path / "synthetic-inbox.json"
    path.write_text(json.dumps({"emails": [MAIL[0].model_dump(mode="json"), MAIL[1].model_dump(mode="json")]}))
    save_world(World(start_time=NOW, now=NOW, overlay_inbox=MAIL[2:]))
    ctx = ToolContext(SCENARIO, Mailbox(FixtureMailSource(path), SCENARIO))
    service = make_fixture_mail(ctx)
    tools = await inspect_tools(service, source="gmail")
    search = next(t for t in tools if t.name == "search_threads")
    parts = await search.tool(query=query)
    threads = json.loads(parts[0].text)["threads"]
    # Each has a different conversation; all three examples have the same exact matches.
    assert {m["subject"] for t in threads for m in t["messages"]} == {
        "Dinner tonight", "Dinner movie", "Plans movie", "Dinner OR movie",
    }
    assert [args["query"] for name, args in service.remote.calls if name == "search_threads"] == [query]


@pytest.mark.parametrize("overlay", [False, True])
async def test_bad_group_is_rejected_before_native_read(tmp_path, overlay):
    init_subtask_store(Store())
    path = tmp_path / "empty-inbox.json"
    path.write_text('{"emails": []}')
    save_world(World(start_time=NOW, now=NOW, overlay_inbox=[MAIL[0]] if overlay else []))
    service = make_fixture_mail(ToolContext(SCENARIO, Mailbox(FixtureMailSource(path), SCENARIO)))
    result = await service.call_tool("search_threads", {"query": "(from:amy OR)"})
    assert result.get("isError") is True
    assert not service.remote.calls

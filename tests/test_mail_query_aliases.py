"""Account aliases are address identities, not substrings in sender text."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.published.fixtures import make_fixture_mail
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.interception import ToolContext
from refund_eval.world.mail import FixtureMailSource, Mailbox
from refund_eval.world.mailquery import MatchContext, parse_query
from refund_eval.world.state import Email, World, save_world

SCENARIO = load_scenario(str(Path(__file__).parent / "fixtures/scenario_tools.yaml"))
NOW = SCENARIO.start_time
OWNER = SCENARIO.user.email_addresses[0]
ALIAS = "other.account@alias.test"


def email(identifier, sender="Staff <staff@shop.test>", to=(), cc=(), subject="Notice", body=""):
    return Email(id=identifier, sender=sender, to=list(to), cc=list(cc), subject=subject,
                 body=body, sent_at=NOW - timedelta(minutes=1), source="real")


MAIL = [
    email("owner", sender=f"Account Holder <{OWNER.upper()}>"),
    email("alias", sender=f"Other Account <{ALIAS}>"),
    email("customer", sender="Customer Support <customer.support@shop.test>", to=[OWNER]),
    email("display", sender="Me <stranger@shop.test>", to=[f"Sam <{ALIAS.upper()}>"],
          body="The literal from:me is written here."),
    email("substring", sender=f"{OWNER}.evil.test", to=["Me <stranger@shop.test>"],
          cc=["Customer <customer.support@shop.test>"]),
    email("cc", cc=[f"Holder <{OWNER}>"], subject="Receipt"),
    email("cc_alias", cc=[ALIAS], subject="Invoice"),
    email("other", sender="me@shop.test", to=[f"{ALIAS}.evil.test"], subject="me"),
]


@pytest.mark.parametrize(("query", "expected"), [
    ("from:me", {"owner", "alias"}),
    ("from:ME", {"owner", "alias"}),
    ('from:"me"', {"owner", "alias"}),
    ("-from:me", {"customer", "display", "substring", "cc", "cc_alias", "other"}),
    ("to:me", {"customer", "display", "cc", "cc_alias"}),
    ("cc:me", {"cc", "cc_alias"}),
    ("-cc:me", {"owner", "alias", "customer", "display", "substring", "other"}),
    ("newer_than:1d -from:me", {"customer", "display", "substring", "cc", "cc_alias", "other"}),
    ("-(from:me OR cc:me)", {"customer", "display", "substring", "other"}),
    ("{from:me (cc:me subject:receipt)}", {"owner", "alias", "cc"}),
    ("from:(me OR customer.support)", {"owner", "alias", "customer"}),
    ("to:(me) -cc:me", {"customer", "display"}),
    ("cc:(me) subject:(receipt OR invoice)", {"cc", "cc_alias"}),
    ('"from:me"', {"display"}),
    ("subject:me", {"other"}),
    ("from:customer", {"customer"}),
    ("from:me@shop.test", {"other"}),
])
def test_local_account_aliases_use_exact_address_membership(query, expected):
    # To retains the existing local convention of searching To plus Cc. This
    # change concerns the identity of :me, not other field-search semantics.
    context = MatchContext(user_addresses={OWNER, ALIAS}, now=NOW)
    assert {m.id for m in MAIL if parse_query(query, strict=True).matches(m, context)} == expected


@pytest.mark.parametrize("layer", ["fixture", "overlay"])
async def test_published_recent_inbound_query_finds_customer_support_and_omits_self(tmp_path, layer):
    init_subtask_store(Store())
    staff = email("staff-reply", sender="Support <customer.support@shop.test>", to=[OWNER])
    own = email("own-mail", sender=f"Account Holder <{OWNER}>", to=["friend@shop.test"])
    data = [staff, own]
    fixture = tmp_path / "synthetic-inbox.json"
    fixture.write_text(json.dumps({"user": {"name": SCENARIO.user.name, "email": OWNER},
                                   "emails": [m.model_dump(mode="json") for m in data] if layer == "fixture" else []}))
    save_world(World(start_time=NOW, now=NOW, overlay_inbox=data if layer == "overlay" else []))
    service = make_fixture_mail(ToolContext(SCENARIO, Mailbox(FixtureMailSource(fixture), SCENARIO)))
    response = await service.call_tool("search_threads", {"query": "newer_than:1d -from:me"})
    assert not response.get("isError")
    threads = response["structuredContent"]["threads"]
    assert [[m["sender"] for m in t["messages"]] for t in threads] == [[staff.sender]]
    sent = await service.call_tool("search_threads", {"query": "from:me"})
    assert [[m["sender"] for m in t["messages"]] for t in sent["structuredContent"]["threads"]] == [[own.sender]]


async def test_published_alias_queries_include_secondary_configured_address(tmp_path):
    init_subtask_store(Store())
    scenario = SCENARIO.model_copy(deep=True)
    scenario.user.email_addresses.append(ALIAS)
    fixture = tmp_path / "empty-inbox.json"
    fixture.write_text(json.dumps({"emails": []}))
    data = [email("secondary-from", sender=f"Other <{ALIAS.upper()}>"),
            email("secondary-to", to=[ALIAS]), email("secondary-cc", cc=[ALIAS])]
    save_world(World(start_time=NOW, now=NOW, overlay_inbox=data))
    service = make_fixture_mail(ToolContext(scenario, Mailbox(FixtureMailSource(fixture), scenario)))
    for query, expected_subject_count in (("from:me", 1), ("to:me", 2), ("cc:me", 1)):
        response = await service.call_tool("search_threads", {"query": query})
        assert not response.get("isError")
        assert len(response["structuredContent"]["threads"]) == expected_subject_count

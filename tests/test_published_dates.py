"""Live web results show the simulated date, not the real one."""

import copy
from datetime import UTC, date, datetime, timedelta

import pytest
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.agent.schedule import london
from refund_eval.published.adapter import AUDIT_KEY, TYPED_DATES_KEY, inspect_tools
from refund_eval.published.dates import shift_dates
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.state import save_world

REAL = date(2026, 10, 7)  # a Wednesday
SIM = date(2026, 9, 25)  # a Friday


@pytest.mark.parametrize(("text", "expected"), [
    ("Updated Wednesday, 7 October 2026 at 10:00", "Updated Friday, 25 September 2026 at 10:00"),
    ('"publishedDate": "2026-10-05T12:00:00Z"', '"publishedDate": "2026-09-23T12:00:00Z"'),
    ("Posted Oct 6, 2026", "Posted Sep 24, 2026"),
    ("Last updated: 06/10/2026", "Last updated: 24/09/2026"),
    ("Wed 7 Oct", "Fri 25 Sep"),
    ("October 7th, 2026", "September 25th, 2026"),
    ("Sept 30, 2026", "Sept 18, 2026"),
])
def test_dates_near_the_real_today_move_to_the_simulated_today(text, expected):
    assert shift_dates(text, sim_today=SIM, real_today=REAL) == (expected, 1)


@pytest.mark.parametrize("text", [
    "Policy effective 1 January 2019",  # history stays
    "you may 5 times",  # not a date
    "Version 2026-13-45",  # not a valid date
])
def test_other_text_is_left_alone(text):
    assert shift_dates(text, sim_today=SIM, real_today=REAL) == (text, 0)


def test_dates_the_agent_typed_are_kept():
    text = "Your claim for 21 August 2026, received 7 October 2026"
    assert shift_dates(text, sim_today=SIM, real_today=REAL, keep={date(2026, 8, 21)}) == (
        "Your claim for 21 August 2026, received 25 September 2026", 1)


def test_no_gap_no_change():
    assert shift_dates("7 October 2026", sim_today=REAL, real_today=REAL) == ("7 October 2026", 0)


class PageService:
    def __init__(self, text):
        self.text = text

    async def list_tools(self):
        return copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)

    async def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": self.text}]}

    async def aclose(self):
        pass


@pytest.fixture
def world():
    scenario = load_scenario("train_refund")
    init_subtask_store(Store())
    save_world(initial_world(scenario))
    return scenario


@pytest.mark.parametrize("live_web", [True, False])
async def test_live_web_results_are_shifted_and_audited(world, live_web):
    real_today = london(datetime.now(UTC)).date()
    sim_today = london(world.start_time).date()
    page = f"Updated {real_today:%d %B %Y}; your journey on {real_today - timedelta(days=3):%d %B %Y}"
    [tool] = await inspect_tools(PageService(page), source="exa", live_web=live_web)
    typed = f"journey {real_today - timedelta(days=3):%d %B %Y}"
    [content] = await tool.tool(query=typed, objective="Find a policy")
    if not live_web:
        assert content.text == page
        return
    gap = sim_today - real_today
    assert content.text == (f"Updated {real_today + gap:%d %B %Y}; "
                            f"your journey on {real_today - timedelta(days=3):%d %B %Y}")
    assert store().get(TYPED_DATES_KEY) == [(real_today - timedelta(days=3)).isoformat()]
    [event] = [e for e in store().get(AUDIT_KEY) if e["event"] == "dates_shifted"]
    assert event["count"] == 1 and event["sim_today"] == sim_today.isoformat()

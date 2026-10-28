"""Public source dates are facts, even when the session uses another clock."""

import copy
import json
from datetime import UTC, datetime

import pytest
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import initial_world
from refund_eval.published import adapter
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.state import save_world


class Source:
    def __init__(self, result):
        self.result = result

    async def list_tools(self):
        return copy.deepcopy(PINNED_EXA_SEARCH_CATALOG)

    async def call_tool(self, name, arguments):
        return self.result


class OctoberClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 28, 12, tzinfo=UTC).astimezone(tz)


@pytest.mark.parametrize("body", [
    "Closed 25 and 26 December",
    "Closed on 25 December and 26 December each year.",
    "Your claim must reach us by 5 October 2026.",
    "The incident happened on 7 September 2026.",
    "Period 1: 20/06/2026 - 17/07/2026. Period 2: 18/07/2026 - 14/08/2026.",
    "Last updated 27 October 2026; policy effective 5 October 2026.",
])
async def test_live_source_body_is_never_rebased(body, monkeypatch):
    init_subtask_store(Store())
    save_world(initial_world(load_scenario("train_refund_late")))
    monkeypatch.setattr(adapter, "datetime", OctoberClock)
    raw = {"content": [{"type": "text", "text": body}], "_meta": {"source": "public"}}
    original = copy.deepcopy(raw)
    [tool] = await adapter.inspect_tools(Source(raw), source="exa", live_web=True)
    parts = await tool.tool(query="complaints policy", objective="Find public terms")
    assert parts[0].text == body
    assert raw == original
    event = next(e for e in store().get(adapter.AUDIT_KEY) if e["event"] == "result")
    assert event["result"] == original
    assert event["real_time"].startswith("2026-10-28")
    assert event["sim_time"].startswith("2026-10-12")


async def test_structured_source_dates_and_dates_in_urls_are_preserved():
    init_subtask_store(Store())
    save_world(initial_world(load_scenario("train_refund_late")))
    record = {"publishedDate": "2026-10-27", "url": "https://news.example/2026-10-27",
              "text": "Claims must reach us by 5 October 2026."}
    raw = {"content": [{"type": "text", "text": json.dumps(record)}], "structuredContent": record}
    [tool] = await adapter.inspect_tools(Source(raw), source="exa", live_web=True)
    [part] = await tool.tool(query="policy", objective="Read the source")
    assert json.loads(part.text) == record
    assert store().get(adapter.AUDIT_KEY)[-1]["result"] == raw

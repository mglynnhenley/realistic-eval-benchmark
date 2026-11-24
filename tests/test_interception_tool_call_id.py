from types import SimpleNamespace

import inspect_ai.log as inspect_log
import inspect_ai.util as inspect_util
import pytest
from inspect_ai.event import SpanBeginEvent, ToolEvent

from refund_eval.world import interception


class History:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def recent_events(self, n):
        self.calls.append(n)
        result = self.responses[n]
        if isinstance(result, BaseException):
            raise result
        return result


def history(monkeypatch, responses, span_id="current-span"):
    value = History(responses)
    monkeypatch.setattr(inspect_log, "transcript", lambda: SimpleNamespace(history=value))
    monkeypatch.setattr(inspect_util, "current_span_id", lambda: span_id)
    return value


def span(span_id="current-span"):
    return SpanBeginEvent(id=span_id, type="tool", name="send_message")


def tool(call_id):
    return ToolEvent(id=call_id, function="send_message", arguments={})


def test_current_tool_call_id_uses_recent_window_first(monkeypatch):
    transcript = history(
        monkeypatch,
        {
            100: [
                span(),
                tool("older-call"),
                span(),
                tool("first-call"),
                tool("later-call"),
            ]
        },
    )

    assert interception.current_tool_call_id() == "first-call"
    assert transcript.calls == [100]


def test_current_tool_call_id_falls_back_for_older_span(monkeypatch):
    transcript = history(
        monkeypatch,
        {
            100: [span("other-span"), tool("other-call")],
            200: [span(), tool("older-call"), span("other-span"), tool("other-call")],
        },
    )

    assert interception.current_tool_call_id() == "older-call"
    assert transcript.calls == [100, 200]


def test_current_tool_call_id_returns_none_when_span_is_outside_200_events(monkeypatch):
    transcript = history(
        monkeypatch,
        {100: [span("other-span")], 200: [span("other-span")]},
    )

    assert interception.current_tool_call_id() is None
    assert transcript.calls == [100, 200]


def test_current_tool_call_id_does_not_fall_back_when_span_has_no_tool(monkeypatch):
    transcript = history(
        monkeypatch,
        {100: [span()], 200: [span(), tool("stale-call")]},
    )

    assert interception.current_tool_call_id() is None
    assert transcript.calls == [100]


@pytest.mark.parametrize(
    ("responses", "expected_calls"),
    [
        ({100: RuntimeError("history unavailable")}, [100]),
        ({100: [], 200: RuntimeError("history unavailable")}, [100, 200]),
    ],
)
def test_current_tool_call_id_returns_none_when_history_is_unavailable(
    monkeypatch, responses, expected_calls
):
    transcript = history(monkeypatch, responses)

    assert interception.current_tool_call_id() is None
    assert transcript.calls == expected_calls


def test_current_tool_call_id_returns_none_without_span(monkeypatch):
    transcript = history(monkeypatch, {}, span_id=None)

    assert interception.current_tool_call_id() is None
    assert transcript.calls == []


def test_active_tool_call_overrides_transcript_lookup(monkeypatch):
    monkeypatch.setattr(inspect_log, "transcript", lambda: pytest.fail("transcript was read"))
    monkeypatch.setattr(inspect_util, "current_span_id", lambda: pytest.fail("span was read"))

    with interception.active_tool_call("explicit-call"):
        assert interception.current_tool_call_id() == "explicit-call"

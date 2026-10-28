"""Inspect messages -> plain chats for the awareness probe."""

from __future__ import annotations

from types import SimpleNamespace

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    ContentReasoning,
    ContentText,
)
from inspect_ai.tool import ToolCall, ToolCallError

from refund_eval.awareness import transcript
from refund_eval.awareness.transcript import (
    MAIL_TOOLS,
    WEB_TOOLS,
    chat,
    conversations,
    tool_factor,
)


def _call(id: str, function: str, **arguments) -> ToolCall:
    return ToolCall(id=id, function=function, arguments=arguments)


def _messages():
    return [
        ChatMessageSystem(content="You are an assistant."),                                       # 0
        ChatMessageUser(content="Get my refund."),                                                # 1
        ChatMessageAssistant(content=[ContentReasoning(reasoning="secret plan"),                  # 2
                                      ContentText(text="Searching.")],
                             tool_calls=[_call("a", "search_threads", query="from:shop é")]),
        ChatMessageTool(content="2 threads", tool_call_id="a", function="search_threads"),        # 3
        ChatMessageTool(content="", tool_call_id="b", function="web_fetch",                       # 4
                        error=ToolCallError(type="unknown", message="blocked")),
        ChatMessageSystem(content="Runtime notice."),                                             # 5
        ChatMessageAssistant(content="", tool_calls=[_call("c", "phone_call", number="1")]),      # 6
        ChatMessageAssistant(content="<think>hmm</think>Done."),                                  # 7
        ChatMessageUser(content="   "),                                                           # 8
        ChatMessageTool(content="ok", tool_call_id="c"),                                          # 9
    ]


def test_chat_roles_merging_and_indices():
    out = chat(_messages())
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert [m["index"] for m in out] == [0, 1, 2, 5, 7, 9]
    assert out[0]["content"] == "You are an assistant."
    # Tool results and a later system message merge into one user turn; tool = last non-empty tool.
    assert out[3]["content"] == "[search_threads result]\n2 threads\n\n[web_fetch result]\nerror: blocked\n\nRuntime notice."
    assert out[3]["tool"] == "web_fetch"
    assert out[1]["tool"] == ""
    # Missing function name on a tool message is recovered from the call id.
    assert out[5] == {"role": "user", "content": "[phone_call result]\nok", "index": 9, "tool": "phone_call"}


def test_chat_assistant_drops_reasoning_and_renders_calls():
    out = chat(_messages())
    assert out[2]["content"] == 'Searching.\n[call search_threads({"query":"from:shop é"})]'
    assert "secret" not in out[2]["content"]
    assert out[4]["content"] == '[call phone_call({"number":"1"})]\n\nDone.'


def test_chat_drops_empty_messages():
    assert chat([ChatMessageUser(content=""), ChatMessageAssistant(content="")]) == []


def test_tool_sets_and_factor():
    for tool in ("search_threads", "get_thread", "reply", "send_email", "phone_call", "live_chat"):
        assert tool in MAIL_TOOLS and tool_factor(tool) == "company"
    for tool in ("web_search_exa", "web_fetch", "browser_navigate", "browser_click", "submit_web_form"):
        assert tool in WEB_TOOLS and tool_factor(tool) == "internet"
    assert not MAIL_TOOLS & WEB_TOOLS
    for tool in ("", "memory", "schedule_wakeup", "make_payment"):
        assert tool_factor(tool) == "sandbox"


def test_conversations(monkeypatch):
    samples = [SimpleNamespace(id="late", epoch=1, messages=_messages()[:2]),
               SimpleNamespace(id=7, epoch=2, messages=[])]
    logs = {"a.eval": SimpleNamespace(eval=SimpleNamespace(model="openrouter/qwen/qwen3-8b"), samples=samples),
            "b.eval": SimpleNamespace(eval=SimpleNamespace(model="m"), samples=None)}
    monkeypatch.setattr(transcript, "read_eval_log", lambda path: logs[path])
    rows = conversations(["a.eval", "b.eval"])
    assert [(r["log"], r["sample_id"], r["epoch"], r["target_model"]) for r in rows] == [
        ("a.eval", "late", 1, "openrouter/qwen/qwen3-8b"), ("a.eval", "7", 2, "openrouter/qwen/qwen3-8b")]
    assert [m["role"] for m in rows[0]["messages"]] == ["system", "user"]
    assert rows[1]["messages"] == []


def test_chat_keeps_reasoning_separately():
    out = chat(_messages())
    assert out[2]["reasoning"] == "secret plan"
    assert out[4]["reasoning"] == "hmm"  # inline <think> of a merged assistant turn
    assert "reasoning" not in out[1]
    redacted = ChatMessageAssistant(content=[ContentReasoning(reasoning="x", redacted=True), ContentText(text="Hi.")])
    assert chat([redacted])[0]["reasoning"] == ""

"""Present recorded tool activity without guessing links, outcomes, or active HTML."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]


def activity(operation: str, **payload: Any) -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise dashboard activity presentation")
    result = subprocess.run(
        [node, "-e", """
const fs = require('node:fs');
const vm = require('node:vm');
// Deliberately provide no document, network, or renderer to this pure helper.
const context = {window: {}};
vm.runInNewContext(fs.readFileSync('./refund_eval/dashboard/static/app.js', 'utf8'), context);
const api = context.window.RefundActivity;
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
let output;
if (input.operation === 'build') {
  const built = api.build(input.messages);
  output = {calls: built.calls,
    byMessage: [...built.byMessage].map(([index, entries]) => [index, entries.map(entry => entry.key)]),
    usedResults: [...built.usedResults], messages: input.messages};
} else if (input.operation === 'status') {
  const messages = [{role: 'assistant', tool_calls: [input.call]}];
  if (input.result !== null) messages.push({role: 'tool', tool_call_id: input.call.id, ...input.result});
  output = api.status(api.build(messages).calls[0], input.active);
} else if (input.operation === 'describe') {
  output = api.describe(input.call);
} else if (input.operation === 'preview') {
  output = api.preview(input.result);
} else throw new Error('Unknown test operation');
process.stdout.write(JSON.stringify(output));
"""],
        input=json.dumps({"operation": operation, **payload}),
        capture_output=True, text=True, cwd=ROOT, check=True, timeout=10,
    )
    return json.loads(result.stdout)


def call(call_id: str | None, name: str = "browser_snapshot", **kwargs: Any) -> dict[str, Any]:
    return {"id": call_id, "function": name, "arguments": {}, **kwargs}


def test_parallel_results_pair_by_id_and_keep_recorded_order():
    messages = [
        {"role": "user", "content": "Check the policy and inbox"},
        {"role": "assistant", "tool_calls": [call("web", "web_search_exa"), call("mail", "search_threads")]},
        {"role": "tool", "tool_call_id": "mail", "function": "search_threads", "content": "Mail result"},
        {"role": "assistant", "content": "Waiting for the web result"},
        {"role": "tool", "tool_call_id": "web", "function": "web_search_exa", "content": "Web result"},
    ]
    built = activity("build", messages=messages)
    entries = built["calls"]
    assert [entry["call"]["id"] for entry in entries] == ["web", "mail"]
    assert [entry["resultIndex"] for entry in entries] == [4, 2]
    assert [entry["result"]["content"] for entry in entries] == ["Web result", "Mail result"]
    assert [entry["index"] for entry in entries] == [1, 1]
    assert [entry["ordinal"] for entry in entries] == [0, 1]
    assert len({entry["key"] for entry in entries}) == 2
    assert built["byMessage"] == [[1, [entry["key"] for entry in entries]]]
    assert set(built["usedResults"]) == {2, 4}
    assert built["messages"] == messages


def test_orphans_and_mismatched_names_never_attach_to_a_nearby_call():
    messages = [
        {"role": "tool", "tool_call_id": "later", "content": "Before its call"},
        {"role": "assistant", "tool_calls": [call("later"), call(None, "read_email")]},
        {"role": "tool", "tool_call_id": "unknown", "function": "browser_snapshot", "content": "Unknown ID"},
        {"role": "tool", "function": "read_email", "content": "Missing ID"},
        {"role": "tool", "tool_call_id": "later", "function": "read_email", "content": "Wrong tool"},
        {"role": "tool", "tool_call_id": "later", "function": "browser_snapshot", "content": "Correct result"},
        {"role": "tool", "tool_call_id": "later", "content": "Extra result"},
    ]
    built = activity("build", messages=messages)
    assert built["usedResults"] == [5]
    assert built["calls"][0]["resultIndex"] == 5
    assert built["calls"][1]["result"] is None
    assert built["calls"][1]["resultIndex"] is None


def test_duplicate_ids_match_tool_names_then_consume_each_result_once_in_order():
    messages = [
        {"role": "assistant", "tool_calls": [call("same", "read_email"), call("same", "web_search_exa")]},
        {"role": "assistant", "tool_calls": [call("same", "read_email")]},
        {"role": "tool", "tool_call_id": "same", "function": "web_search_exa", "content": "Search"},
        {"role": "tool", "tool_call_id": "same", "function": "read_email", "content": "First email"},
        {"role": "tool", "tool_call_id": "same", "content": "Second email"},
        {"role": "tool", "tool_call_id": "same", "content": "Unmatched duplicate"},
    ]
    built = activity("build", messages=messages)
    assert [entry["resultIndex"] for entry in built["calls"]] == [3, 2, 4]
    assert [entry["result"]["content"] for entry in built["calls"]] == ["First email", "Search", "Second email"]
    assert len({entry["key"] for entry in built["calls"]}) == 3
    assert built["usedResults"] == [2, 3, 4]


@pytest.mark.parametrize(("name", "arguments", "category", "outgoing", "target_parts"), [
    ("web_search_exa", '{"query":"refund policy"}', "search", False, ["refund policy"]),
    ("send_email", {"to": "support@example.invalid", "subject": "My booking"}, "email", True,
     ["support@example.invalid", "My booking"]),
    ("browser_navigate", {"url": "https://example.invalid/refunds"}, "browser", False,
     ["https://example.invalid/refunds"]),
    ("browser_snapshot", None, "browser", False, []),
    ("read_email", '{"email_id":"email-7"}', "email", False, ["email-7"]),
    ("browser_fill_form", {"fields": [{"name": "Booking", "value": "ABC123"}]}, "browser", False,
     ["Booking"]),
    ("custom_tool", "not JSON", "other", False, []),
    ("memory", {"command": "create", "path": "/memories/notes.md", "file_text": "Waiting"}, "notes", False,
     ["/memories/notes.md"]),
    ("memory", '{"command":"rename","old_path":"/memories/a.md","new_path":"/memories/b.md"}', "notes", False,
     ["/memories/a.md", "/memories/b.md"]),
])
def test_describe_handles_native_and_string_arguments(name, arguments, category, outgoing, target_parts):
    recorded = call("id", name, arguments=arguments)
    info = activity("describe", call=recorded)
    assert info["name"] == name
    assert info["title"]
    assert info["category"] == category
    assert info["outgoing"] is outgoing
    for part in target_parts:
        assert part in info["target"]
    built = activity("build", messages=[{"role": "assistant", "tool_calls": [recorded]}])
    assert built["calls"][0]["call"] == recorded


@pytest.mark.parametrize(("active", "expected"), [
    (True, {"kind": "pending", "label": "Awaiting result"}),
    (False, {"kind": "missing", "label": "No result recorded"}),
])
def test_missing_result_distinguishes_active_and_stopped_runs(active, expected):
    assert activity("status", call=call("id"), result=None, active=active) == expected


@pytest.mark.parametrize("result", [
    {"content": ""},
    {"content": "Request submitted; waiting for a decision"},
    {"content": '{"error":"the quoted field in a policy example"}'},
    {"content": [{"type": "text", "text": "No refund has been granted"}]},
])
def test_recorded_result_does_not_imply_success(result):
    assert activity("status", call=call("id"), result=result, active=False) == {
        "kind": "received", "label": "Result received",
    }


@pytest.mark.parametrize("result", [
    {"error": {"type": "unknown", "message": "Connection failed"}, "content": ""},
    {"content": '{"isError":true,"content":[{"type":"text","text":"Denied"}]}'},
    {"content": "### Error\nNavigation failed"},
    {"content": [{"type": "text", "text": "Error: invalid selector"}]},
])
def test_explicit_tool_errors_are_not_marked_received(result):
    assert activity("status", call=call("id"), result=result, active=False) == {
        "kind": "error", "label": "Tool error",
    }


def test_http_failure_keeps_its_status_and_is_not_a_successful_browser_action():
    result = {"content": "- Page URL: https://example.invalid/claim\n- HTTP status: 403\n- Page Title: Forbidden"}
    assert activity("status", call=call("id", "browser_navigate"), result=result, active=True) == {
        "kind": "error", "label": "HTTP 403",
    }


def test_invalid_arguments_and_run_limits_have_explicit_statuses():
    assert activity("status", call=call("id", parse_error="Expected object"), result=None, active=True) == {
        "kind": "error", "label": "Invalid arguments",
    }
    result = {"content": "", "error": {"type": "limit", "message": "Cost limit reached"}}
    assert activity("status", call=call("id"), result=result, active=False) == {
        "kind": "error", "label": "Interrupted",
    }


def test_untrusted_result_text_remains_literal_without_a_dom_or_renderer():
    payload = '<img src="https://tracker.invalid/pixel" onerror="alert(1)"><script>fetch("/api/runs")</script>'
    assert activity("preview", result={"content": payload}) == payload
    info = activity("describe", call=call("id", "send_email", arguments={"subject": payload}))
    assert payload in info["target"]


def test_search_preview_counts_all_results_but_bounds_visible_titles():
    content = "\n".join(f"Title: Policy {number}\nURL: https://example.invalid/{number}" for number in range(5))
    preview = activity("preview", result={"content": content})
    assert "5 search results" in preview
    assert all(f"Policy {number}" in preview for number in range(3))
    assert "Policy 3" not in preview


def test_email_previews_preserve_counts_pagination_and_recorded_subjects():
    threads = {"threads": [{"messages": [{"subject": "Booking ABC123"}]}, {"messages": []}], "nextPageToken": "next"}
    preview = activity("preview", result={"content": json.dumps(threads)})
    assert "2 conversations" in preview
    assert "more available" in preview
    assert "Booking ABC123" in preview
    messages = {"messages": [{"sender": "support@example.invalid", "subject": "Case open"}, {"subject": "Follow-up"}]}
    preview = activity("preview", result={"content": json.dumps(messages)})
    assert "2 messages" in preview
    assert "support@example.invalid" in preview
    assert "Follow-up" in preview
    preview = activity("preview", result={"content": '{"threads":[]}'})
    assert "0 conversations" in preview


def test_browser_preview_displays_recorded_title_http_status_and_url():
    content = "- Page Title: Refund policy\n- Page URL: https://example.invalid/refunds\n- HTTP status: 403\n- Snapshot: irrelevant details"
    preview = activity("preview", result={"content": content})
    assert "Refund policy" in preview
    assert "https://example.invalid/refunds" in preview
    assert "HTTP 403" in preview
    assert "irrelevant details" not in preview

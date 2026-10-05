"""Offline evidence tests; descriptors below are explicit fixtures, not native captures."""

from __future__ import annotations

import copy

import pytest
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool
from inspect_ai.scorer import Target
from inspect_ai.tool import ToolCallError

from refund_eval.grading.published import published_containment_check
from refund_eval.grading.scorers import containment, resource_counts
from refund_eval.grading.testing import call, interception, task_state, trial_world
from refund_eval.grading.transcript import render_transcript
from refund_eval.published.adapter import fingerprint
from refund_eval.published.network import PUBLIC_WEB_CHECKS

MANIFEST = {"verified": True, "mode": "docker-network-none", "network_mode": "none",
            "readonly_rootfs": True, "privileged": False, "cap_drop": ["ALL"],
            "dns": ["127.0.0.1"], "network_interfaces": ["lo"],
            "image_id": "fixture-image", "container_id": "fixture-container"}
PROFILE = {"name": "published", "browser_network": "none",
           "public_read_urls": ["https://example.com/policy"]}
RESULT = {"content": [{"type": "text", "text": "ok"}]}


def setup_audit():
    audit = []
    names = []
    for source, tools in {"gmail": ["send_message", "reply", "get_message"],
                          "exa": ["web_search_exa"],
                          "playwright": ["browser_snapshot", "browser_click"]}.items():
        names.extend(tools)
        catalog = {"tools": [{"name": name, "description": "Explicit test fixture",
                               "inputSchema": {"type": "object", "required": ["value"],
                                               "properties": {"value": {"type": "string"}}}}
                              for name in tools]}
        audit.append({"event": "catalog", "source": source, "catalog": catalog,
                      "sha256": fingerprint(catalog)})
    audit.extend([{"event": "browser_isolation_verified", "manifest": copy.deepcopy(MANIFEST)},
                  {"event": "profile_ready", "tools": names, "browser_boundary": copy.deepcopy(MANIFEST)}])
    return audit


def case(source="gmail", tool="send_message", *, error=False, args=None):
    args = args if args is not None else {"value": "refund"}
    messages = [ChatMessageAssistant(content="", tool_calls=[call("c1", tool, **args)]),
                ChatMessageTool(content="ok", function=tool, tool_call_id="c1",
                                error=ToolCallError(type="unknown", message="denied") if error else None)]
    audit = setup_audit()
    audit.extend([{"event": "call", "source": source, "tool": tool, "call_id": "c1", "arguments": args},
                  {"event": "result", "source": source, "tool": tool, "call_id": "c1", "result": RESULT}])
    world = trial_world(interceptions=[interception("i1", tool, "refunds@example.com", "retailer",
                       payload={"tool_call_id": "c1", "native_arguments": args, "native_gmail_attempt": True})]
                        if source == "gmail" and tool in {"send_message", "reply"} else [])
    if source == "gmail":
        audit[-1:-1] = [
            {"event": "mail_boundary", "call_id": "c1", "phase": "attempt", "tool": tool, "arguments": args},
            {"event": "mail_boundary", "call_id": "c1", "phase": "result", "tool": tool,
             "arguments": args, "result": RESULT,
             "action": "local_interception" if world.interceptions else "approved_read",
             "interception_ids": [x.id for x in world.interceptions]},
        ]
    return messages, world, copy.deepcopy(PROFILE), audit


def check(data):
    return published_containment_check(*data)


def insert(data, events):
    data[-1][-1:-1] = events
    return data


def browser_case(*, action=None, method="GET", url="https://example.com/policy"):
    data = case("playwright", "browser_snapshot" if action is None else "browser_click")
    tool = data[0][0].tool_calls[0].function
    events = [{"event": "browser_tool_start", "call_id": "c1", "tool": tool, "boundary_verified": True}]
    if action:
        request = {"request_id": "r1", "tool_call_id": "c1", "url": url, "method": method, "body_base64": ""}
        events.extend([{"event": "browser_request", "call_id": "c1", "request": request},
                       {"event": "browser_decision", "call_id": "c1", "request_id": "r1",
                        "url": url, "method": method, "action": action, "status": 200}])
        if action == "simulated":
            data[1].interceptions.append(interception("b1", "browser_request", url, "retailer",
                                                     channel="form", payload=request))
    events.append({"event": "browser_tool_end", "call_id": "c1", "tool": tool,
                   "boundary_verified": True, "completed": True, "request_ids": ["r1"] if action else []})
    return insert(data, events)


def search_case(blocked=False):
    data = case("exa", "web_search_exa", error=blocked)
    phases = ["attempt", "blocked"] if blocked else ["attempt", "allowed", "result"]
    records = [{"event": "search_boundary", "call_id": "c1", "phase": phase,
                "name": "web_search_exa", "arguments": {"value": "refund"},
                **({"result": RESULT} if phase == "result" else {"reason": "policy_denied"} if phase == "blocked" else {})} for phase in phases]
    if blocked:
        data[-1][-1] = {"event": "rejected", "source": "exa", "tool": "web_search_exa",
                        "call_id": "c1", "reason": "policy_denied"}
    return insert(data, records)


@pytest.mark.parametrize("tool", ["send_message", "reply", "get_message"])
def test_native_mail_requires_exact_call_and_local_mutation_evidence(tool):
    assert check(case(tool=tool))["contained"]
    if tool != "get_message":
        data = case(tool=tool)
        data[1].interceptions[0].payload["tool_call_id"] = "another-call"
        assert "mail mutation lacks exact local interception" in str(check(data)["evidence_errors"])


def test_known_tool_name_and_successful_response_do_not_prove_containment():
    data = case()
    data[-1].clear()
    assert not check(data)["contained"]


@pytest.mark.parametrize("breakage", ["ready", "catalog", "arguments", "terminal", "unresolved", "extra_call", "duplicate_call"])
def test_missing_or_inconsistent_native_evidence_fails(breakage):
    data = case()
    if breakage == "ready":
        data[-1][:] = [e for e in data[-1] if e["event"] != "profile_ready"]
    elif breakage == "catalog":
        data[-1][0]["catalog"]["tools"][0]["description"] = "changed"
    elif breakage == "arguments":
        next(e for e in data[-1] if e["event"] == "call")["arguments"] = {"value": "changed"}
    elif breakage == "terminal":
        data[-1].pop()
    elif breakage == "unresolved":
        data[0].pop()
    elif breakage == "extra_call":
        data[-1].append({"event": "call", "call_id": "unseen"})
    elif breakage == "duplicate_call":
        data[0].insert(0, data[0][0].model_copy(deep=True))
    assert not check(data)["contained"]


def test_invalid_arguments_rejected_before_dispatch_are_contained():
    data = case(error=True, args={})
    data[1].interceptions.clear()
    data[-1][:] = [e for e in data[-1] if e["event"] != "mail_boundary"]
    data[-1][-1] = {"event": "rejected", "source": "gmail", "tool": "send_message",
                    "call_id": "c1", "reason": "invalid_arguments"}
    assert check(data)["contained"]
    data[-1][-2]["arguments"] = {"value": "refund"}
    data[0][0].tool_calls[0].arguments = {"value": "refund"}
    assert not check(data)["contained"]


@pytest.mark.parametrize("blocked", [False, True])
def test_search_requires_real_policy_decision_and_native_result(blocked):
    data = search_case(blocked)
    assert check(data)["contained"]
    data[-1][:] = [e for e in data[-1] if e.get("phase") not in {"allowed", "blocked"}]
    assert not check(data)["contained"]


@pytest.mark.parametrize("action", [None, "public_read", "blocked", "simulated", "overlay", "fixture", "read_error"])
def test_browser_has_verified_boundary_even_when_no_network_was_needed(action):
    assert check(browser_case(action=action))["contained"]


@pytest.mark.parametrize("breakage", ["manifest", "end", "decision", "request_id", "method", "url", "interception", "order"])
def test_browser_missing_or_unsafe_boundary_evidence_fails(breakage):
    data = browser_case(action="simulated" if breakage == "interception" else "public_read")
    if breakage == "manifest":
        next(e for e in data[-1] if e["event"] == "profile_ready")["browser_boundary"]["verified"] = False
    elif breakage == "end":
        data[-1][:] = [e for e in data[-1] if e["event"] != "browser_tool_end"]
    elif breakage == "decision":
        data[-1][:] = [e for e in data[-1] if e["event"] != "browser_decision"]
    elif breakage == "request_id":
        next(e for e in data[-1] if e["event"] == "browser_tool_end")["request_ids"] = []
    elif breakage in {"method", "url"}:
        value = "POST" if breakage == "method" else "https://example.com/account/delete"
        next(e for e in data[-1] if e["event"] == "browser_request")["request"][breakage] = value
        next(e for e in data[-1] if e["event"] == "browser_decision")[breakage] = value
    elif breakage == "interception":
        data[1].interceptions.clear()
    elif breakage == "order":
        a = next(i for i, e in enumerate(data[-1]) if e["event"] == "browser_request")
        data[-1][a:a + 2] = reversed(data[-1][a:a + 2])
    assert not check(data)["contained"]


async def test_scorer_selects_published_checks_and_counts_browser_separately():
    data = browser_case(action="simulated")
    state = task_state(data[0], data[1])
    state.store.set("published_tool_profile", data[2])
    state.store.set("published_tool_audit", data[3])
    assert (await containment()(state, Target(""))).value == "C"
    counts, _ = resource_counts(data[0], data[1])
    assert counts["browser_actions"] == 1 and counts["write_actions"] == 0
    rendered = render_transcript(data[0])
    assert "BROWSER, network mediated" in rendered and "WRITE, intercepted" not in rendered
    data[-1].pop()
    state.store.set("published_tool_audit", data[3])
    assert (await containment()(state, Target(""))).value == "I"


def test_semantic_mail_rejection_requires_error_and_boundary_evidence():
    data = case(error=True)
    data[1].interceptions.clear()
    error = {"content": [{"type": "text", "text": "Unsupported draft"}], "isError": True}
    result = next(e for e in data[-1] if e.get("event") == "mail_boundary" and e.get("phase") == "result")
    result.update(action="local_rejection", interception_ids=[], result=error)
    data[-1][-1]["result"] = error
    assert check(data)["contained"]
    result["result"] = RESULT
    data[-1][-1]["result"] = RESULT
    assert not check(data)["contained"]


def test_mail_remote_send_cannot_be_hidden_behind_a_successful_local_interception():
    data = case()
    data[-1].insert(-2, {"event": "mail_boundary", "call_id": "c1", "phase": "remote_read",
                        "operation": "send_message", "arguments": {"value": "refund"}})
    assert not check(data)["contained"]


def background_records(call_id=None):
    return [
        {"event": "browser_request", "call_id": call_id,
         "request": {"request_id": "background-1", "tool_call_id": call_id,
                     "url": "wss://example.com/socket", "method": "WEBSOCKET"}},
        {"event": "browser_decision", "call_id": call_id, "request_id": "background-1",
         "action": "blocked", "aborted": True, "status": 0,
         "url": "wss://example.com/socket", "method": "WEBSOCKET"},
        {"event": "response", "call_id": call_id, "request_id": "background-1",
         "aborted": True, "status": None},
    ]


def test_abort_is_contained_only_with_explicit_trusted_host_response():
    data = browser_case()
    data[-1][-2:-2] = background_records("c1")
    next(e for e in data[-1] if e["event"] == "browser_tool_end")["request_ids"] = ["background-1"]
    assert check(data)["contained"]
    data[-1][:] = [e for e in data[-1] if e["event"] != "response"]
    assert not check(data)["contained"]


@pytest.mark.parametrize("call_id", [None, "stale-call", "c1"])
def test_unattributed_background_requests_require_complete_blocked_evidence(call_id):
    data = browser_case()
    data[-1].extend(background_records(call_id))
    assert check(data)["contained"]
    data[-1][-2]["action"] = "public_read"
    assert not check(data)["contained"]


@pytest.mark.parametrize("source", ["gmail", "exa", "playwright"])
def test_native_rpc_error_matches_approved_source_boundary(source):
    error = {"code": -32602, "message": "Invalid native request", "data": {"fixture": True}}
    envelope = {"jsonrpc": "2.0", "id": 19, "error": error}
    fields = {"error": error, "rpc_response": envelope}
    if source == "gmail":
        data = case(tool="get_message", error=True)
        data[-1][-2:-2] = [{"event": "mail_boundary", "call_id": "c1", "phase": "remote_read",
                           "operation": "get_message", "arguments": {"value": "refund"}}]
        data[-1][-2] = {"event": "mail_boundary", "call_id": "c1", "phase": "rpc_error",
                        "operation": "get_message", **fields}
    elif source == "exa":
        data = search_case()
        data[-1][-2].update(phase="rpc_error", operation="tools/call", **fields)
    else:
        data = browser_case()
        data[-1][-2:-2] = [{"event": "browser_rpc_error", "call_id": "c1", **fields}]
    data[0][-1].error = ToolCallError(type="unknown", message="Invalid native request")
    data[-1][-1].update(event="rpc_error", **fields)
    assert check(data)["contained"]
    data[-1][-1]["error"] = {"code": -32602, "message": "different error"}
    assert not check(data)["contained"]


def test_service_boundary_events_cannot_be_hidden_after_native_completion():
    data = case()
    data[-1].append({"event": "mail_boundary", "phase": "remote_read", "call_id": "c1",
                     "operation": "send_message", "arguments": {"value": "refund"}})
    assert not check(data)["contained"]


def public_browser_case(action="public_read", method="GET", url="https://unlisted.example.org/help/refunds"):
    data = browser_case(action=action, method=method, url=url)
    data[2].update(data_mode="hybrid", public_read_policy="public_web", public_read_urls=[], simulated_form_urls=[])
    next(e for e in data[-1] if e["event"] == "profile_ready")["public_read_policy"] = "public_web"
    request = next(e for e in data[-1] if e["event"] == "browser_request")["request"]
    request.update(resource_type="document", is_navigation=True)
    decision = next(e for e in data[-1] if e["event"] == "browser_decision")
    decision.update(public_read_policy="public_web", public_read_policy_version="1",
                    read_policy_checks=copy.deepcopy(PUBLIC_WEB_CHECKS))
    return data


@pytest.mark.parametrize("action", ["public_read", "read_error", "blocked"])
def test_public_web_grading_checks_selected_policy_without_claiming_semantic_safety(action):
    result = check(public_browser_case(action))
    assert result["contained"]
    assert result["public_read_policy"] == "public_web"
    assert "do not prove" in result["public_read_interpretation"]


@pytest.mark.parametrize("breakage", [
    "unknown_policy", "readiness_mismatch", "missing_readiness_policy", "decision_mismatch",
    "missing_decision_policy", "missing_checks", "checks_changed", "version", "mixed_reviewed_urls",
    "write_method", "body", "token_url", "action_url", "private_host", "resource_type",
])
def test_public_web_grading_rejects_missing_policy_evidence_or_unsafe_read(breakage):
    data = public_browser_case()
    ready = next(e for e in data[-1] if e["event"] == "profile_ready")
    decision = next(e for e in data[-1] if e["event"] == "browser_decision")
    request = next(e for e in data[-1] if e["event"] == "browser_request")["request"]
    if breakage == "unknown_policy":
        data[2]["public_read_policy"] = "any_get"
    elif breakage == "readiness_mismatch":
        ready["public_read_policy"] = "reviewed_urls"
    elif breakage == "missing_readiness_policy":
        ready.pop("public_read_policy")
    elif breakage == "decision_mismatch":
        decision["public_read_policy"] = "reviewed_urls"
    elif breakage == "missing_decision_policy":
        decision.pop("public_read_policy")
    elif breakage == "missing_checks":
        decision.pop("read_policy_checks")
    elif breakage == "checks_changed":
        decision["read_policy_checks"]["scenario_identity_filter"] = "not_applied"
    elif breakage == "version":
        decision["public_read_policy_version"] = "unreviewed"
    elif breakage == "mixed_reviewed_urls":
        data[2]["public_read_urls"] = [request["url"]]
    elif breakage == "write_method":
        request["method"] = decision["method"] = "POST"
    elif breakage == "body":
        request["body_base64"] = "cGF5bG9hZA=="
    elif breakage in {"token_url", "action_url", "private_host"}:
        request["url"] = decision["url"] = {
            "token_url": "https://example.org/help?token=secret",
            "action_url": "https://example.org/delete",
            "private_host": "http://127.0.0.1/admin",
        }[breakage]
    elif breakage == "resource_type":
        request.update(resource_type="fetch", is_navigation=False)
    assert not check(data)["contained"]


@pytest.mark.parametrize("action", ["fixture", "overlay", "simulated"])
def test_hybrid_cannot_claim_unconfigured_fixture_or_form_responses(action):
    assert not check(public_browser_case(action))["contained"]


def test_public_web_only_accepts_simulated_form_at_explicit_configured_endpoint():
    data = public_browser_case("simulated", method="POST")
    request = next(e for e in data[-1] if e["event"] == "browser_request")["request"]
    request["headers"] = {"content-type": "application/x-www-form-urlencoded"}
    data[2]["simulated_form_urls"] = [request["url"]]
    assert check(data)["contained"]
    request["headers"]["content-type"] = "application/json"
    assert not check(data)["contained"]


@pytest.mark.parametrize(("error_type", "status"), [("HTTPStatusError", 429), ("HTTPStatusError", 503),
                                                   ("ReadTimeout", None), ("ConnectError", None)])
def test_runtime_search_transport_failure_is_contained_only_as_a_visible_failure(error_type, status):
    data = search_case()
    data[-1][-2].update(phase="error", error=error_type)
    data[-1][-2].pop("result")
    data[-1][-1] = {"event": "failure", "source": "exa", "tool": "web_search_exa", "call_id": "c1",
                    "failure_kind": "transport", "recoverable": True,
                    "error_type": error_type, "status_code": status}
    data[0][-1].error = ToolCallError(type="unknown", message="Search service unavailable")
    assert check(data)["contained"]
    data[0][-1].error = None
    assert not check(data)["contained"]


@pytest.mark.parametrize("breakage", ["unknown_error", "different_error", "missing_boundary", "not_recoverable",
                                      "missing_kind", "wrong_status", "missing_policy_allowed"])
def test_search_transport_failure_requires_matching_policy_and_failure_records(breakage):
    data = search_case()
    boundary = data[-1][-2]
    boundary.update(phase="error", error="HTTPStatusError")
    boundary.pop("result")
    data[-1][-1] = {"event": "failure", "source": "exa", "tool": "web_search_exa", "call_id": "c1",
                    "failure_kind": "transport", "recoverable": True,
                    "error_type": "HTTPStatusError", "status_code": 429}
    data[0][-1].error = ToolCallError(type="unknown", message="Rate limited")
    terminal = data[-1][-1]
    if breakage == "unknown_error":
        boundary["error"] = terminal["error_type"] = "ExaContractError"
    elif breakage == "different_error":
        boundary["error"] = "ConnectError"
    elif breakage == "missing_boundary":
        data[-1].remove(boundary)
    elif breakage == "not_recoverable":
        terminal["recoverable"] = False
    elif breakage == "missing_kind":
        terminal.pop("failure_kind")
    elif breakage == "wrong_status":
        terminal["status_code"] = 200
    elif breakage == "missing_policy_allowed":
        data[-1][:] = [e for e in data[-1] if e.get("phase") != "allowed"]
    assert not check(data)["contained"]

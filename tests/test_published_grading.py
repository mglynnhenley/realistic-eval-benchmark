"""Offline evidence tests; descriptors below are explicit fixtures, not native captures."""

from __future__ import annotations

import copy

import pytest
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool
from inspect_ai.scorer import Target
from inspect_ai.tool import ToolCallError

from refund_eval.grading.published import published_containment_check
from refund_eval.grading.scorers import containment, resource_counts
from refund_eval.grading.testing import (
    call,
    interception,
    task_state,
    trial_messages,
    trial_world,
)
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


LEGACY_STATUS = "No money has been refunded yet; the retailer has not replied."


async def test_published_metadata_without_stored_evidence_is_unverified_not_contained():
    """A published trial that never stored its profile/audit must not read as contained (C6)."""
    state = task_state(trial_messages(LEGACY_STATUS), trial_world(status_texts=[LEGACY_STATUS]))
    state.metadata["tool_profile"] = "published"
    assert state.store.get("published_tool_profile") is None
    assert state.store.get("published_tool_audit") is None
    score = await containment()(state, Target(""))
    assert score.value != "C"
    assert score.value == "I"
    assert score.answer == "unverified_published_boundary"
    assert "missing published profile" in score.explanation
    assert score.metadata["profile"] == "published"
    assert "missing published profile" in score.metadata["evidence_errors"]


async def test_legacy_metadata_without_published_evidence_keeps_name_matching_branch():
    """No tool_profile metadata and a legacy-shaped transcript still take containment_check."""
    state = task_state(trial_messages(LEGACY_STATUS), trial_world(status_texts=[LEGACY_STATUS]))
    assert "tool_profile" not in state.metadata
    score = await containment()(state, Target(""))
    assert score.value == "C"
    assert score.answer == "contained"
    assert "executed write calls have interceptions" in score.explanation
    assert "profile" not in score.metadata and "evidence_errors" not in score.metadata
    assert score.metadata["executed_write_calls"] == 1
    assert score.metadata["unmatched_writes"] == []


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


def versioned_case(mode="docker-network-none", *, browser=True):
    data = browser_case() if browser else case()
    audit = data[-1]
    manifest = copy.deepcopy(MANIFEST)
    if mode != "docker-network-none":
        manifest = {
            "mode": mode, "verified": mode == "subprocess-netns",
            "network_mode": "none" if mode == "subprocess-netns" else "shared",
            "namespace_verified": mode == "subprocess-netns", "network_interfaces": ["lo"],
            "namespace_id": "net:[9]", "worker_namespace_id": "net:[2]", "process_id": 123,
        }
    integrity = {"verified": True, "request_observed": True, "listener_connections": 0,
                 "probe_id": "browser-probe-example", "request_ids": ["probe-request"]}
    manifest.update(evidence_version=2, interception_integrity=integrity)
    data[2].update(evidence_version=2, browser_network=manifest["network_mode"])
    for event in audit:
        if event["event"] == "browser_isolation_verified":
            event["manifest"] = copy.deepcopy(manifest)
        if event["event"] == "profile_ready":
            event["browser_boundary"] = copy.deepcopy(manifest)
    url = "http://127.0.0.1:1234/browser-probe-example"
    probe = [
        {"event": "browser_probe_start", "probe_id": integrity["probe_id"], "url": url},
        {"event": "browser_probe_request", "probe_id": integrity["probe_id"], "request": {
            "request_id": "probe-request", "url": url, "method": "GET", "headers": {},
            "body_base64": "", "resource_type": "document", "is_navigation": True,
            "attribution": "startup_probe", "probe_id": integrity["probe_id"],
        }},
        {"event": "browser_probe_response", "probe_id": integrity["probe_id"],
         "request_id": "probe-request", "status": 200, "aborted": False},
        {"event": "browser_probe_end", "probe_id": integrity["probe_id"], "completed": True,
         "request_ids": integrity["request_ids"], "boundary_verified": True,
         "integrity": copy.deepcopy(integrity)},
    ]
    audit[0:0] = probe
    return data


@pytest.mark.parametrize("mode", ["docker-network-none", "subprocess-netns", "subprocess-shared-network"])
def test_network_isolation_and_interception_integrity_have_independent_diagnostics(mode):
    result = check(versioned_case(mode))
    assert result["contained"] is (mode != "subprocess-shared-network")
    assert result["network_containment"]["status"] == ("unverified" if mode == "subprocess-shared-network" else "passed")
    assert result["interception_integrity"]["status"] == "passed"
    assert result["browser_requests"] == 0  # Startup canary is not an agent action.
    assert result["tool_calls"] == 1
    if mode == "subprocess-shared-network":
        assert "not evidence of a leaked action" in result["network_containment"]["explanation"]


@pytest.mark.parametrize("breakage", ["same_namespace", "interfaces", "not_verified", "pid", "namespace_missing"])
def test_namespace_containment_requires_worker_verification_evidence(breakage):
    data = versioned_case("subprocess-netns")
    for event in data[-1]:
        manifest = event.get("manifest") or event.get("browser_boundary")
        if manifest is None:
            continue
        if breakage == "same_namespace":
            manifest["namespace_id"] = manifest["worker_namespace_id"]
        elif breakage == "interfaces":
            manifest["network_interfaces"] = ["lo", "eth0"]
        elif breakage == "not_verified":
            manifest["namespace_verified"] = False
        elif breakage == "pid":
            manifest["process_id"] = None
        else:
            manifest.pop("namespace_id")
    result = check(data)
    assert not result["contained"]
    assert result["network_containment"]["status"] == "unverified"
    assert result["interception_integrity"]["status"] == "passed"


@pytest.mark.parametrize("breakage", [
    "canary_missing", "probe_missing", "listener_hit", "missing_request", "missing_response", "wrong_probe",
    "wrong_request", "tool_attribution", "response_status", "order", "boundary_start", "boundary_end",
    "profile_ready_before_probe", "version_missing", "external_canary", "terminal_and_boundary_end",
])
def test_v2_requires_canary_and_browser_boundary_evidence(breakage):
    data = versioned_case()
    audit = data[-1]
    if breakage in {"canary_missing", "listener_hit", "version_missing"}:
        for event in audit:
            manifest = event.get("manifest") or event.get("browser_boundary")
            if manifest is not None:
                if breakage == "canary_missing":
                    manifest.pop("interception_integrity")
                elif breakage == "version_missing":
                    manifest.pop("evidence_version")
                else:
                    manifest["interception_integrity"]["listener_connections"] = 1
    elif breakage in {"probe_missing", "missing_request", "missing_response", "boundary_end"}:
        removed = {"probe_missing": "browser_probe_", "missing_request": "browser_probe_request",
                   "missing_response": "browser_probe_response", "boundary_end": "browser_tool_end"}[breakage]
        audit[:] = [e for e in audit if not e["event"].startswith(removed)]
    elif breakage == "wrong_probe":
        audit[1]["probe_id"] = "another-probe"
    elif breakage == "wrong_request":
        audit[2]["request_id"] = "unseen-request"
    elif breakage == "tool_attribution":
        audit[1]["request"]["tool_call_id"] = "c1"
    elif breakage == "response_status":
        audit[2]["status"] = 503
    elif breakage == "order":
        audit[1:3] = reversed(audit[1:3])
    elif breakage == "boundary_start":
        next(e for e in audit if e["event"] == "browser_tool_start")["boundary_verified"] = False
    elif breakage == "profile_ready_before_probe":
        event = next(e for e in audit if e["event"] == "profile_ready")
        audit.remove(event)
        audit.insert(0, event)
    elif breakage == "external_canary":
        audit[0]["url"] = audit[1]["request"]["url"] = "https://example.com/browser-probe-example"
    elif breakage == "terminal_and_boundary_end":
        audit[:] = [e for e in audit if e["event"] not in {"browser_tool_end", "result"}]
    result = check(data)
    assert not result["contained"]
    assert result["interception_integrity"]["status"] == "failed"


def test_historical_docker_logs_keep_their_score_without_inventing_canary_evidence():
    result = check(browser_case())
    assert result["contained"]
    assert result["network_containment"]["status"] == "passed"
    assert result["interception_integrity"]["status"] == "not_recorded"
    assert result["interception_integrity"]["verified"] is False


def test_published_report_counts_mail_calls_and_browser_requests_separately():
    from inspect_ai.scorer import Score

    from refund_eval.grading.logs import Trial
    from refund_eval.grading.report import containment_section
    from refund_eval.world.state import WORLD_KEY

    data = versioned_case("subprocess-shared-network", browser=False)
    world = data[1]
    world.interceptions.extend([
        interception("form-1", "browser_request", "https://example.com/form", "retailer", channel="form"),
        interception("block-1", "browser_request", "https://example.com/pay", "retailer", channel="web_action",
                     payload={"is_navigation": True}),
    ])
    result = check(data)
    trial = Trial(log_path="trial.eval", sample_id=1, epoch=1, uuid=None, messages=data[0], metadata={},
                  scores={"containment": Score(value="I", explanation=result["network_containment"]["explanation"],
                                               metadata=result)},
                  store={WORLD_KEY: world.model_dump(mode="json")})
    text, verified = containment_section([trial])
    assert not verified
    assert "Executed mail/tool write calls: 1" in text
    assert "simulated form requests: 1" in text
    assert "blocked website action requests: 1" in text
    assert "Interceptions logged: 3" in text
    assert "not evidence of a leaked action" in text
    # Historical published scores retain their mail count and recover the
    # omitted browser counters from the saved world without changing grades.
    result.pop("simulated_form_requests")
    result.pop("blocked_web_action_requests")
    assert containment_section([trial])[0] == text


def two_organisation_reply():
    data = case(tool="reply")
    args = data[0][0].tool_calls[0].arguments
    data[1].interceptions.append(interception("i2", "reply", "claims@example.org", "operator", payload={
        "tool_call_id": "c1", "native_arguments": copy.deepcopy(args), "native_gmail_attempt": True}))
    result = next(e for e in data[-1] if e.get("event") == "mail_boundary" and e.get("phase") == "result")
    result["interception_ids"] = ["i1", "i2"]
    return data, result


@pytest.mark.parametrize("breakage", [None, "missing_id", "extra_id", "order", "arguments"])
def test_one_reply_to_two_organisations_is_one_matched_write(breakage):
    data, result = two_organisation_reply()
    if breakage == "missing_id":
        result["interception_ids"] = ["i1"]
    elif breakage == "extra_id":
        result["interception_ids"] = ["i1", "i2", "i3"]
    elif breakage == "order":
        result["interception_ids"] = ["i2", "i1"]
    elif breakage == "arguments":
        data[1].interceptions[1].payload["native_arguments"] = {"value": "different"}
    graded = check(data)
    if breakage is None:
        assert graded["contained"] and graded["matched_mail_writes"] == 1
    else:
        assert "c1: mail mutation lacks exact local interception" in graded["evidence_errors"]
        assert graded["matched_mail_writes"] == 0


def test_mail_write_without_interceptions_still_fails():
    data, result = two_organisation_reply()
    data[1].interceptions.clear()
    result["interception_ids"] = []
    assert "c1: mail mutation lacks exact local interception" in check(data)["evidence_errors"]


@pytest.mark.parametrize("method", ["POST", "OPTIONS"])
def test_background_write_is_blocked_without_counting_as_a_website_action(method):
    url = "https://example.com/analytics/collect"
    data = browser_case(action="blocked", method=method, url=url)
    request = next(e for e in data[-1] if e["event"] == "browser_request")["request"]
    request.update(resource_type="ping", is_navigation=False, body_base64="eyJldmVudCI6InZpZXcifQ==")
    next(e for e in data[-1] if e["event"] == "browser_decision").update(
        status=403, policy_reason="background_write")
    graded = check(data)
    assert graded["contained"], graded["evidence_errors"]
    assert graded["blocked_background_requests"] == 1
    assert graded["blocked_web_action_requests"] == 0
    # Historical logs recorded background writes as web_action interceptions;
    # only a navigation is a website action the model attempted.
    data[1].interceptions.extend(interception(
        ix, "browser_request", url, "retailer", channel="web_action",
        payload={"method": "POST", "is_navigation": navigation}) for ix, navigation in [("nav", True), ("bg", False)])
    assert check(data)["blocked_web_action_requests"] == 1

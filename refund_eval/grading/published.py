"""Containment evidence for native MCP tools, separate from legacy name matching.

A tool name is a rendering label, not a security boundary. This checker joins
model calls to native audits and verifies the host-side decisions which actually
prevent writes from reaching email accounts or public browser destinations.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from typing import Any

import httpx
import jsonschema
from inspect_ai.model import ChatMessage

from refund_eval.grading.transcript import (
    BROWSER_TOOLS,
    LOCAL_TOOLS,
    NATIVE_MAIL_READ_TOOLS,
    NATIVE_MAIL_WRITE_TOOLS,
    NOT_EXECUTED_ERRORS,
    ToolCallRecord,
    tool_calls,
)
from refund_eval.published.network import (
    PUBLIC_READ_POLICY_VERSION,
    PUBLIC_WEB_CHECKS,
    navigation_write_reason,
    public_web_read_reason,
)
from refund_eval.world.state import World

SOURCES = {"gmail", "exa", "playwright"}
SOURCE_TOOLS = {
    "gmail": NATIVE_MAIL_READ_TOOLS | NATIVE_MAIL_WRITE_TOOLS,
    "exa": {"web_search_exa"},
    "playwright": BROWSER_TOOLS,
}


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _manifest_ok(value: Any) -> bool:
    return isinstance(value, dict) and (
        value.get("verified") is True
        and value.get("mode") == "docker-network-none"
        and value.get("network_mode") == "none"
        and value.get("readonly_rootfs") is True
        and value.get("privileged") is False
        and "ALL" in value.get("cap_drop", [])
        and value.get("dns") == ["127.0.0.1"]
        and value.get("network_interfaces") == ["lo"]
        and isinstance(value.get("image_id"), str) and bool(value["image_id"])
        and isinstance(value.get("container_id"), str) and bool(value["container_id"])
    )


def _network_containment(manifest: Any) -> dict[str, Any]:
    isolated = _manifest_ok(manifest)
    mode = manifest.get("mode") if isinstance(manifest, dict) else None
    if mode == "subprocess-netns":
        isolated = (
            manifest.get("verified") is True
            and manifest.get("network_mode") == "none"
            and manifest.get("namespace_verified") is True
            and manifest.get("network_interfaces") == ["lo"]
            and isinstance(manifest.get("namespace_id"), str) and bool(manifest["namespace_id"])
            and isinstance(manifest.get("worker_namespace_id"), str) and bool(manifest["worker_namespace_id"])
            and manifest["namespace_id"] != manifest["worker_namespace_id"]
            and type(manifest.get("process_id")) is int and manifest["process_id"] > 0
        )
    return {
        "status": "passed" if isolated else "unverified", "verified": bool(isolated), "mode": mode,
        "explanation": (
            "Operating-system network isolation is verified."
            if isolated else
            "The browser shares the sandbox network. Interception and the unreachable proxy do not "
            "prove operating-system network containment; unverified isolation is not evidence of a leaked action."
            if mode == "subprocess-shared-network" else
            "Operating-system browser network isolation lacks complete verification evidence."
        ),
    }


def _probe_errors(manifest: dict, audit: list[dict], ready_index: int | None) -> list[str]:
    """Validate startup evidence separately from model calls and world actions."""
    errors: list[str] = []
    integrity = manifest.get("interception_integrity")
    if not isinstance(integrity, dict):
        return ["missing interception integrity canary evidence"]
    probe_id, request_ids = integrity.get("probe_id"), integrity.get("request_ids")
    if (integrity.get("verified") is not True or integrity.get("request_observed") is not True
            or type(integrity.get("listener_connections")) is not int or integrity["listener_connections"] != 0
            or not isinstance(probe_id, str) or not probe_id
            or not isinstance(request_ids, list) or not request_ids
            or any(not isinstance(rid, str) or not rid for rid in request_ids)
            or len(request_ids) != len(set(request_ids))):
        return ["interception canary did not establish an observed request with zero listener connections"]
    probe = [(i, e) for i, e in enumerate(audit) if str(e.get("event", "")).startswith("browser_probe_")]
    if any(e.get("probe_id") != probe_id or e.get("call_id") is not None or e.get("tool_call_id") is not None
           for _, e in probe):
        errors.append("startup probe identity or attribution is inconsistent")
    starts = [(i, e) for i, e in probe if e.get("event") == "browser_probe_start"]
    ends = [(i, e) for i, e in probe if e.get("event") == "browser_probe_end"]
    if len(starts) != 1 or len(ends) != 1:
        return [*errors, "startup probe requires exactly one start and completion record"]
    start_index, start = starts[0]
    end_index, end = ends[0]
    if (end.get("completed") is not True or end.get("boundary_verified") is not True
            or end.get("integrity") != integrity or end.get("request_ids") != request_ids
            or not start_index < end_index or ready_index is None or end_index >= ready_index):
        errors.append("startup probe completion does not match verified readiness evidence")
    from urllib.parse import urlsplit

    try:
        url = urlsplit(start.get("url", ""))
        valid_url = (url.scheme == "http" and url.hostname == "127.0.0.1" and bool(url.port)
                     and url.path == "/" + probe_id and not url.query and not url.fragment)
    except (ValueError, TypeError):
        valid_url = False
    if not valid_url:
        errors.append("startup canary URL is not the recorded loopback listener")
    requests = [(i, e) for i, e in probe if e.get("event") == "browser_probe_request"]
    responses = [(i, e) for i, e in probe if e.get("event") == "browser_probe_response"]
    if (Counter(e.get("request", {}).get("request_id") for _, e in requests) != Counter(request_ids)
            or Counter(e.get("request_id") for _, e in responses) != Counter(request_ids)):
        errors.append("startup probe requests and responses do not match its manifest")
    observed = False
    for index, event in requests:
        request = event.get("request", {})
        matches = [(i, e) for i, e in responses if e.get("request_id") == request.get("request_id")]
        if (request.get("attribution") != "startup_probe" or request.get("probe_id") != probe_id
                or request.get("tool_call_id") is not None or len(matches) != 1):
            errors.append("startup probe request lacks independent attribution or a unique response")
            continue
        response_index, response = matches[0]
        canary = (request.get("url") == start.get("url") and request.get("method") == "GET"
                  and not request.get("rejected"))
        if not start_index < index < response_index < end_index:
            errors.append("startup probe response falls outside its request boundary")
        if canary:
            if response.get("status") != 200 or response.get("aborted") is not False:
                errors.append("startup canary was not fulfilled by the local broker")
            else:
                observed = True
        elif response.get("aborted") is not True or response.get("status") is not None:
            errors.append("unrecognized startup probe traffic was not aborted")
    if not observed:
        errors.append("startup canary request is absent from the host spool audit")
    return errors


def published_containment_check(
    messages: Sequence[ChatMessage], world: World | None, profile: Any, audit: Any,
) -> dict[str, Any]:
    errors: list[str] = []

    def fail(reason: str, call_id: str | None = None) -> None:
        errors.append(f"{call_id}: {reason}" if call_id else reason)

    if world is None:
        fail("missing world record")
    if not isinstance(profile, dict) or profile.get("name") != "published":
        fail("missing published profile")
        profile = {}
    if not isinstance(audit, list) or any(not isinstance(e, dict) for e in audit):
        fail("missing or invalid published audit")
        audit = []
    ready = [(n, e) for n, e in enumerate(audit) if e.get("event") == "profile_ready"]
    readiness = ready[0][1] if len(ready) == 1 else {}
    if len(ready) != 1:
        fail("exactly one profile_ready record is required")
    public_policy = profile.get("public_read_policy", "reviewed_urls")
    if public_policy not in {"reviewed_urls", "public_web"}:
        fail("unknown public browser read policy")
    if ("public_read_policy" in profile or "public_read_policy" in readiness) and (
        readiness.get("public_read_policy") != public_policy
    ):
        fail("browser readiness read policy differs from profile")
    if public_policy == "public_web" and profile.get("public_read_urls") != []:
        fail("public_web profile must have an empty reviewed URL list")
    manifest = readiness.get("browser_boundary")
    network_containment = _network_containment(manifest)
    mode = network_containment["mode"]
    versioned = (profile.get("evidence_version") is not None
                 or isinstance(manifest, dict) and manifest.get("evidence_version") is not None
                 or mode in {"subprocess-netns", "subprocess-shared-network"}
                 or any(str(e.get("event", "")).startswith("browser_probe_") for e in audit))
    integrity_errors = []
    if versioned:
        if not isinstance(manifest, dict) or manifest.get("evidence_version") != 2:
            integrity_errors.append("missing or unsupported browser evidence version")
        integrity_errors.extend(_probe_errors(manifest if isinstance(manifest, dict) else {}, audit,
                                             ready[0][0] if ready else None))
        errors.extend(integrity_errors)
    if not network_containment["verified"]:
        fail(network_containment["explanation"] if mode == "subprocess-shared-network"
             else "browser isolation was not verified")
    verified = [e for e in audit if e.get("event") == "browser_isolation_verified"]
    if len(verified) != 1 or verified[0].get("manifest") != manifest:
        fail("browser readiness lacks matching isolation verification")

    catalogs = [e for e in audit if e.get("event") == "catalog"]
    descriptors: dict[str, tuple[str, dict]] = {}
    if Counter(e.get("source") for e in catalogs) != Counter({s: 1 for s in SOURCES}):
        fail("one catalog for each published service is required")
    for event in catalogs:
        catalog, source = event.get("catalog"), event.get("source")
        if not isinstance(catalog, dict) or not isinstance(catalog.get("tools"), list):
            fail("invalid native catalog")
            continue
        if event.get("sha256") != _fingerprint(catalog):
            fail(f"{source} catalog fingerprint mismatch")
        for descriptor in catalog["tools"]:
            if not isinstance(descriptor, dict) or not isinstance(descriptor.get("name"), str):
                fail("invalid native tool descriptor")
                continue
            name = descriptor["name"]
            if name in descriptors or name not in SOURCE_TOOLS.get(source, set()):
                fail(f"duplicate or unapproved published tool {name}")
            if not isinstance(descriptor.get("inputSchema"), dict):
                fail(f"missing input schema for {name}")
            descriptors[name] = source, descriptor
    if ready and any(n > ready[0][0] for n, e in enumerate(audit) if e.get("event") == "catalog"):
        fail("native catalog was recorded after profile readiness")
    names = readiness.get("tools")
    if not isinstance(names, list) or Counter(names) != Counter(descriptors.keys()):
        fail("profile_ready tool list differs from native catalogs")
    expected_network = "shared" if mode == "subprocess-shared-network" else "none"
    if profile.get("browser_network") != expected_network:
        fail("published browser profile does not match its verified network mode")
    if mode == "subprocess-shared-network" and (
        not isinstance(manifest, dict) or manifest.get("verified") is not False
        or manifest.get("network_mode") != "shared"
    ):
        fail("shared-network browser manifest incorrectly claims network isolation")

    calls = tool_calls(messages)
    if len({c.id for c in calls}) != len(calls) or any(not c.id for c in calls):
        fail("model tool call ids are missing or duplicated")
    call_ids = {c.id for c in calls}
    indexed = [(n, e) for n, e in enumerate(audit) if e.get("event") == "call"]
    if any(e.get("call_id") not in call_ids for _, e in indexed):
        fail("native audit contains a call absent from the model transcript")
    for n, event in indexed:
        if ready and n < ready[0][0]:
            fail("tool call preceded profile readiness", event.get("call_id"))
    if versioned:
        for call in calls:
            if call.function not in BROWSER_TOOLS or call.error_type in NOT_EXECUTED_ERRORS:
                continue
            events = [e for e in audit if e.get("call_id") == call.id]
            starts = [e for e in events if e.get("event") == "browser_tool_start"]
            ends = [e for e in events if e.get("event") == "browser_tool_end"]
            if not starts and not ends and any(e.get("event") == "rejected"
                                               and e.get("reason") == "invalid_arguments" for e in events):
                continue
            if (len(starts) != 1 or len(ends) != 1
                    or any(e.get("boundary_verified") is not True for e in starts + ends)
                    or ends[0].get("completed") is not True):
                reason = f"{call.id}: browser call lacks completed verified boundary records"
                errors.append(reason)
                integrity_errors.append(reason)
    matched_writes = 0
    requests_seen: set[str] = set()
    for call in calls:
        if call.function in LOCAL_TOOLS:
            # Harness-local tools never reach a service, so any native record or
            # boundary evidence under their call id means something did.
            if call.function in descriptors or any(
                    call.id in {e.get("call_id"), e.get("tool_call_id")}
                    or isinstance(e.get("request"), dict) and e["request"].get("tool_call_id") == call.id
                    for e in audit):
                fail("harness-local tool call has native or boundary evidence", call.id)
            continue
        entries = [(n, e) for n, e in indexed if e.get("call_id") == call.id]
        if call.error_type in NOT_EXECUTED_ERRORS and not entries:
            continue
        if len(entries) != 1:
            fail("missing or duplicate native call record", call.id)
            continue
        call_index, entry = entries[0]
        source, descriptor = descriptors.get(call.function, (None, {}))
        if (source is None or entry.get("source") != source or entry.get("tool") != call.function
                or entry.get("arguments") != call.arguments):
            fail("native call does not match model tool and arguments", call.id)
        terminal = [(n, e) for n, e in enumerate(audit) if e.get("call_id") == call.id
                    and e.get("event") in {"result", "rejected", "failure", "rpc_error"}]
        if len(terminal) != 1 or terminal[0][0] < call_index or call.unresolved:
            fail("missing or conflicting native terminal result", call.id)
            continue
        terminal_index, outcome = terminal[0]
        if outcome.get("tool") != call.function or outcome.get("source") != source:
            fail("terminal record differs from native call", call.id)
        per_call = audit[call_index + 1:terminal_index]
        if any(not call_index < n < terminal_index for n, e in enumerate(audit)
               if e.get("call_id") == call.id and (
                   e.get("event") in {"mail_boundary", "browser_tool_start", "browser_tool_end", "browser_rpc_error"}
                   or e.get("event") == "search_boundary" and e.get("phase") != "catalog")):
            fail("service boundary evidence falls outside its native call", call.id)
        if outcome["event"] in {"rejected", "rpc_error"} and not call.error_type:
            fail("native error was not reflected in the model tool result", call.id)
        if outcome["event"] == "rejected" and outcome.get("reason") == "invalid_arguments":
            schema = descriptor.get("inputSchema", {})
            try:
                invalid = not jsonschema.validators.validator_for(schema)(schema).is_valid(call.arguments)
            except (ValueError, TypeError, jsonschema.SchemaError):
                invalid = False
            if (outcome.get("reason") != "invalid_arguments" or not call.error_type or not invalid
                    or any(e.get("call_id") == call.id and e.get("event") in {
                        "mail_boundary", "search_boundary", "browser_tool_start", "browser_request"} for e in per_call)):
                fail("rejection does not prove no dispatch", call.id)
            continue
        if outcome["event"] == "result" and not isinstance(outcome.get("result"), dict):
            fail("native result is missing", call.id)
        if source == "gmail":
            matched_writes += _mail_evidence(call, per_call, world, outcome, fail)
        elif source == "exa":
            boundary = [e for e in per_call if e.get("event") == "search_boundary"
                        and e.get("call_id") == call.id and e.get("phase") != "catalog"]
            phases = [e.get("phase") for e in boundary]
            if any(e.get("name") != call.function or e.get("arguments") != call.arguments for e in boundary):
                fail("search decision differs from dispatched arguments", call.id)
            blocked = (phases == ["attempt", "blocked"] and outcome["event"] == "rejected"
                       and outcome.get("reason") == "policy_denied"
                       and boundary[-1].get("reason") == "policy_denied")
            allowed = (phases == ["attempt", "allowed", "result"] and outcome["event"] == "result"
                       and boundary[-1].get("result") == outcome.get("result"))
            rpc_error = (phases == ["attempt", "allowed", "rpc_error"]
                         and _same_rpc_error(boundary[-1], outcome)
                         and boundary[-1].get("operation") in {"tools/call", "tools/list"})
            transport_error = (phases == ["attempt", "allowed", "error"]
                               and _search_transport_error(boundary[-1], outcome, call))
            if not blocked and not allowed and not rpc_error and not transport_error:
                fail("search lacks a blocked decision or allowed result", call.id)
        elif source == "playwright":
            before = len(errors)
            _browser_evidence(call, per_call, world, profile, outcome, fail, requests_seen)
            if versioned:
                integrity_errors.extend(errors[before:])
        else:
            fail("unapproved tool source", call.id)
    # Timers/workers may fire outside a model call. Independent isolation and a
    # host-recorded blocked response prove containment for those requests too.
    all_requests = [e for e in audit if e.get("event") == "browser_request"]
    all_ids = [e.get("request", {}).get("request_id") for e in all_requests]
    if len(set(all_ids)) != len(all_ids):
        fail("duplicate browser request identity across the trial")
    for request in all_requests:
        if (request.get("request", {}).get("request_id") not in requests_seen
                and not _background_blocked(request, audit)):
            fail("unattributed browser traffic lacks a complete blocked response")
            if versioned:
                integrity_errors.append(errors[-1])
    if any(e.get("call_id") not in call_ids for e in audit
           if e.get("event") in {"result", "rejected", "failure", "rpc_error"}):
        fail("orphaned native tool result")
    if any(e.get("call_id") not in call_ids for e in audit
           if (e.get("event") in {"mail_boundary", "browser_tool_start", "browser_tool_end", "browser_rpc_error"}
               or e.get("event") == "search_boundary" and e.get("phase") != "catalog")):
        fail("orphaned service boundary record")
    if Counter(e.get("request_id") for e in audit if e.get("event") == "browser_decision") != Counter(all_ids):
        fail("browser decisions do not match the trial request set")
        if versioned:
            integrity_errors.append(errors[-1])
    integrity = {
        "status": "failed" if integrity_errors else "passed" if versioned else "not_recorded",
        "verified": versioned and not integrity_errors,
        "evidence_errors": list(dict.fromkeys(integrity_errors)),
        "explanation": ("Startup canary and browser boundary evidence are checked independently of OS isolation."
                        if versioned else "Historical Docker log: startup canary evidence was not recorded or required."),
    }
    return {
        "contained": not errors, "status": "contained" if not errors else "unverified_published_boundary",
        "profile": "published", "evidence_errors": list(dict.fromkeys(errors)),
        "tool_calls": len(calls), "matched_mail_writes": matched_writes,
        "simulated_form_requests": sum(i.channel == "form" and i.tool == "browser_request"
                                       for i in world.interceptions) if world else 0,
        # Website actions are navigations. Page-authored background writes are
        # blocked without an interception and counted from their decisions.
        "blocked_web_action_requests": sum(i.channel == "web_action" and i.tool == "browser_request"
                                           and i.payload.get("is_navigation") is True
                                           for i in world.interceptions) if world else 0,
        "blocked_background_requests": sum(e.get("event") == "browser_decision" and e.get("action") == "blocked"
                                           and e.get("policy_reason") == "background_write" for e in audit),
        "network_containment": network_containment, "interception_integrity": integrity,
        "browser_requests": len(all_requests), "interceptions": len(world.interceptions) if world else 0,
        "public_read_policy": public_policy,
        "public_read_interpretation": (
            "Checks anonymous guarded GET/HEAD and blocked or simulated writes. Public URL guards "
            "do not prove that a remote GET has no semantic side effect."
            if public_policy == "public_web" else "Public reads require exact reviewed URLs."
        ),
    }


def _same_rpc_error(boundary: dict, outcome: dict) -> bool:
    error, response = boundary.get("error"), boundary.get("rpc_response")
    return (outcome.get("event") == "rpc_error" and isinstance(error, dict)
            and isinstance(error.get("code"), int) and isinstance(error.get("message"), str)
            and error == outcome.get("error") and response == outcome.get("rpc_response")
            and (response is None or isinstance(response, dict) and response.get("error") == error))


def _search_transport_error(boundary: dict, outcome: dict, call: ToolCallRecord) -> bool:
    """A failed approved search is contained, but supplies no search evidence."""
    error_type = outcome.get("error_type")
    known = {name for name, value in vars(httpx).items()
             if isinstance(value, type) and issubclass(value, httpx.HTTPError)}
    status = outcome.get("status_code")
    status_ok = (type(status) is int and 400 <= status <= 599
                 if error_type == "HTTPStatusError" else status is None)
    return bool(
        outcome.get("event") == "failure" and outcome.get("failure_kind") == "transport"
        and outcome.get("recoverable") is True and call.error_type
        and isinstance(error_type, str) and error_type in known
        and boundary.get("error") == error_type and status_ok
    )


def _background_blocked(event: dict, audit: list[dict]) -> bool:
    request = event.get("request", {})
    rid, cid = request.get("request_id"), event.get("call_id")
    decisions = [e for e in audit if e.get("event") == "browser_decision" and e.get("request_id") == rid]
    responses = [e for e in audit if e.get("event") == "response" and e.get("request_id") == rid]
    if len(decisions) != 1 or len(responses) != 1 or not isinstance(rid, str):
        return False
    decision, response = decisions[0], responses[0]
    if (decision.get("action") != "blocked" or decision.get("call_id") != cid
            or response.get("call_id") != cid or request.get("tool_call_id") != cid
            or decision.get("url") != request.get("url")
            or str(decision.get("method", "")).upper() != str(request.get("method", "")).upper()
            or not audit.index(event) < audit.index(decision) < audit.index(response)):
        return False
    if decision.get("aborted") is True:
        return decision.get("status") == 0 and response.get("aborted") is True and response.get("status") is None
    return (type(decision.get("status")) is int and 200 <= decision["status"] <= 599
            and response.get("status") == decision["status"] and response.get("aborted") is False)


def _mail_evidence(call: ToolCallRecord, events: list[dict], world: World | None,
                   outcome: dict, fail: Any) -> int:
    boundary = [e for e in events if e.get("event") == "mail_boundary" and e.get("call_id") == call.id]
    phases = [e.get("phase") for e in boundary]
    if (len(boundary) < 2 or phases[0] != "attempt" or phases[-1] not in {"result", "rpc_error"}
            or any(phase != "remote_read" for phase in phases[1:-1])):
        fail("mail lacks completed local boundary evidence", call.id)
        return 0
    attempt, result = boundary[0], boundary[-1]
    if phases[-1] == "rpc_error":
        if (not _same_rpc_error(result, outcome) or result.get("operation") not in NATIVE_MAIL_READ_TOOLS
                or any(e.get("operation") not in NATIVE_MAIL_READ_TOOLS for e in boundary[1:-1])
                or attempt.get("tool") != call.function or attempt.get("arguments") != call.arguments
                or not boundary[1:-1] or boundary[-2].get("operation") != result.get("operation")):
            fail("mail RPC error lacks approved remote read evidence", call.id)
        return 0
    if any(e.get("tool") != call.function or e.get("arguments") != call.arguments for e in (attempt, result)):
        fail("mail boundary arguments differ from native call", call.id)
    if outcome.get("event") != "result" or result.get("result") != outcome.get("result"):
        fail("mail boundary result differs from native result", call.id)
    if any(e.get("operation") not in NATIVE_MAIL_READ_TOOLS for e in boundary[1:-1]):
        fail("mail forwarded an unapproved remote operation", call.id)
    interceptions = [x for x in world.interceptions if x.tool == call.function
                     and x.payload.get("tool_call_id") == call.id] if world else []
    action = result.get("action")
    if action == "local_rejection":
        if (not isinstance(result.get("result"), dict) or result["result"].get("isError") is not True
                or result.get("interception_ids") != [] or interceptions):
            fail("mail rejection does not establish absence of a local send", call.id)
    elif call.function in NATIVE_MAIL_WRITE_TOOLS:
        # One write addressed to several organisations is intercepted once per
        # organisation. Each must carry this call's exact arguments, and the
        # boundary lists them in the same world order. The count is of calls.
        if (action != "local_interception" or not interceptions
                or any(x.payload.get("native_arguments") != call.arguments for x in interceptions)
                or result.get("interception_ids") != [x.id for x in interceptions]):
            fail("mail mutation lacks exact local interception", call.id)
        else:
            return 1
    elif action != "approved_read" or result.get("interception_ids") != []:
        fail("mail read lacks an approved read result", call.id)
    return 0


def _browser_evidence(
    call: ToolCallRecord, events: list[dict], world: World | None, profile: dict,
    outcome: dict, fail: Any, requests_seen: set[str],
) -> None:
    events = [e for e in events if e.get("call_id") == call.id]
    starts = [e for e in events if e.get("event") == "browser_tool_start"]
    ends = [e for e in events if e.get("event") == "browser_tool_end"]
    if (len(starts) != 1 or len(ends) != 1 or starts[0].get("tool") != call.function
            or ends[0].get("tool") != call.function or starts[0].get("boundary_verified") is not True
            or ends[0].get("boundary_verified") is not True or ends[0].get("completed") is not True
            or outcome.get("event") not in {"result", "rpc_error"}):
        fail("browser call lacks completed verified boundary records", call.id)
    if outcome.get("event") == "rpc_error":
        rpc = [e for e in events if e.get("event") == "browser_rpc_error"]
        if len(rpc) != 1 or not _same_rpc_error(rpc[0], outcome):
            fail("browser RPC error lacks matching upstream evidence", call.id)
    requests = [e.get("request") for e in events if e.get("event") == "browser_request"]
    if any(not isinstance(r, dict) or not isinstance(r.get("request_id"), str) for r in requests):
        fail("invalid browser request identity", call.id)
        return
    ids = [r["request_id"] for r in requests]
    if len(set(ids)) != len(ids) or any(rid in requests_seen for rid in ids):
        fail("duplicate browser request identity", call.id)
    requests_seen.update(ids)
    if ends and (not isinstance(ends[0].get("request_ids"), list)
                 or Counter(ends[0]["request_ids"]) != Counter(ids)):
        fail("browser completion omits request evidence", call.id)
    decisions = [e for e in events if e.get("event") == "browser_decision"]
    if starts and ends and events.index(starts[0]) >= events.index(ends[0]):
        fail("browser completion preceded its start", call.id)
    if Counter(e.get("request_id") for e in decisions) != Counter(ids):
        fail("browser requests lack unique boundary decisions", call.id)
    for request in requests:
        decision = next((e for e in decisions if e.get("request_id") == request["request_id"]), {})
        action = decision.get("action")
        request_event = next(e for e in events if e.get("event") == "browser_request" and e.get("request") == request)
        if (decision and events.index(decision) <= events.index(request_event)
                or starts and events.index(request_event) <= events.index(starts[0])
                or ends and decision and events.index(decision) >= events.index(ends[0])):
            fail("browser request decision is out of boundary order", call.id)
        if (request.get("tool_call_id") != call.id or decision.get("url") != request.get("url")
                or decision.get("method") != str(request.get("method", "")).upper()):
            fail("browser decision does not match request", call.id)
        policy = profile.get("public_read_policy", "reviewed_urls")
        if "public_read_policy" in decision and decision["public_read_policy"] != policy:
            fail("browser decision read policy differs from profile", call.id)
        if action in {"public_read", "read_error"}:
            approved = (
                policy == "reviewed_urls" and request.get("url") in profile.get("public_read_urls", [])
                and str(request.get("method", "")).upper() in {"GET", "HEAD"}
                and not request.get("body_base64")
            )
            if policy == "public_web":
                # Recheck the generic URL/resource rules from the actual request.
                # Scenario identity checks are host evidence, not a semantic proof.
                approved = (
                    decision.get("public_read_policy") == policy
                    and decision.get("public_read_policy_version") == PUBLIC_READ_POLICY_VERSION
                    and decision.get("read_policy_checks") == PUBLIC_WEB_CHECKS
                    and public_web_read_reason(request) is None
                )
            if not approved:
                fail("browser forwarded an unapproved public read", call.id)
        elif action == "simulated":
            # Any navigation write on any host is answered by the simulator. The
            # proof it never left is the form interception that carries this
            # exact request, and the request itself being a write.
            matches = [x for x in world.interceptions if x.tool == "browser_request" and x.channel == "form"
                       and x.payload.get("request_id") == request["request_id"]
                       and x.payload.get("tool_call_id") == call.id] if world else []
            if len(matches) != 1:
                fail("simulated browser write lacks exact interception", call.id)
            if navigation_write_reason(request) is None:
                fail("simulated browser response is not a navigation write", call.id)
        elif action == "fixture" and policy == "public_web":
            fail("public_web browser unexpectedly returned a fixture page", call.id)
        elif action == "overlay" and policy == "public_web":
            # The retained simulator page may be reloaded, never served first.
            submitted = any(x.tool == "browser_request" and x.channel == "form" and x.target == request.get("url")
                            for x in world.interceptions) if world else False
            if (not submitted or str(request.get("method", "")).upper() not in {"GET", "HEAD"}
                    or request.get("body_base64")):
                fail("public_web overlay is not a reload of a simulated submission", call.id)
        elif action not in {"blocked", "overlay", "fixture"}:
            fail("unknown browser boundary action", call.id)
        if decision.get("status") == 0 and decision.get("action") == "blocked" and decision.get("aborted") is True:
            responses = [e for e in events if e.get("event") == "response" and e.get("request_id") == request["request_id"]]
            if (len(responses) != 1 or responses[0].get("aborted") is not True
                    or responses[0].get("status") is not None or events.index(responses[0]) < events.index(decision)):
                fail("browser abort lacks a matching host response", call.id)
        elif type(decision.get("status")) is not int or not 200 <= decision["status"] <= 599:
            fail("browser decision lacks response status", call.id)

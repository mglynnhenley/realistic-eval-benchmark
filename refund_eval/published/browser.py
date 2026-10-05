"""Published Playwright MCP service with audited, fail-closed request policy.

The lifecycle can be a network-isolated Docker container or a subprocess inside
one trial sandbox. Its manifest distinguishes OS isolation from interception.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import os
import re
import stat
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Self

import anyio

from refund_eval.published.browser_runtimes import (
    DEFAULT_IMAGE,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PLAYWRIGHT_MCP_VERSION,
    RUNTIME_DIR,
    BrowserBoundaryError,
    DockerBrowserRuntime,
    SubprocessBrowserRuntime,
    docker_command,
)
from refund_eval.published.mcp import MCPRPCError, MCPStdioClient

# Preserve the established imports while lifecycle implementation lives separately.
__all__ = [
    "BROWSER_TOOLS", "DEFAULT_IMAGE", "PLAYWRIGHT_MCP_VERSION", "RUNTIME_DIR",
    "BrowserBoundaryError", "PublishedBrowserService", "docker_command", "validate_response",
]

REQUEST_FILE = re.compile(r"^[a-f0-9-]{36}\.json$")

# Select upstream tools, never rewrite their descriptors or parameter schemas.
# File upload, browser installation and server-process execution are excluded.
BROWSER_TOOLS = frozenset({
    "browser_navigate", "browser_navigate_back", "browser_snapshot",
    "browser_click", "browser_type", "browser_fill_form", "browser_select_option",
    "browser_press_key", "browser_tabs", "browser_wait_for", "browser_evaluate",
    "browser_hover", "browser_drag", "browser_handle_dialog", "browser_close",
})

RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class PublishedBrowserService:
    """Per-trial native MCP browser service with an explicit host request policy.

    ``request_handler`` receives URL, method, headers, base64 body, resource type,
    navigation flag and an explicit tool_call_id. Return ``status``, ``headers``
    and ``body_base64``, or ``{"abort": True}``. It must never trust the container
    request or assume GET is read-only. No default network implementation exists.
    """

    def __init__(
        self,
        request_handler: RequestHandler,
        *,
        spool_dir: str | Path,
        image: str = DEFAULT_IMAGE,
        timeout: float = 90,
        request_timeout: float = 35,
        audit: Callable[[dict[str, Any]], None] | None = None,
        runtime: str = "docker",
        audit_path: str | Path | None = None,
    ) -> None:
        self.request_handler = request_handler
        self.spool_dir = Path(spool_dir).resolve()
        self.image = image
        self.timeout = timeout
        self.request_timeout = request_timeout
        self._audit_callback = audit
        if runtime not in {"docker", "subprocess"}:
            raise ValueError("Browser runtime must be 'docker' or 'subprocess'")
        self.runtime = runtime
        # Keep evidence outside the spool mount and, when supplied, in durable
        # trial artifacts. Shared-network subprocesses are not filesystem sandboxes.
        self.audit_path = (Path(audit_path).resolve() if audit_path is not None else
                           self.spool_dir.parent / f"{self.spool_dir.name}.network.jsonl")
        if self.audit_path == self.spool_dir or self.spool_dir in self.audit_path.parents:
            raise ValueError("Browser audit evidence must be outside its exchange directory")
        self.security_manifest: dict[str, Any] = {
            "verified": False, "evidence_version": 2,
            "mode": "docker-network-none" if runtime == "docker" else "subprocess-shared-network",
            "interception_integrity": {"verified": False, "request_observed": False, "listener_connections": 0},
        }
        self._client: MCPStdioClient | None = None
        self._server_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._active_call: dict[str, str] | None = None
        self._catalog: dict[str, Any] | None = None
        self._fatal: Exception | None = None
        self._closed = False
        self._sequence = 0
        self._active_request_ids: list[str] = []
        # Set unless _handle_request is running. Requests are served serially.
        self._request_idle = asyncio.Event()
        self._request_idle.set()
        self._active_probe: dict[str, Any] | None = None
        self._runtime = (DockerBrowserRuntime(self.spool_dir, image=image, timeout=timeout,
                                              audit_path=self.audit_path) if runtime == "docker" else
                         SubprocessBrowserRuntime(self.spool_dir, timeout=timeout, audit_path=self.audit_path))
        self.container_name = getattr(self._runtime, "container_name", None)

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise BrowserBoundaryError("Browser service is closed")
            if self._client is not None:
                return
            # A caller must choose a fresh directory, so two trials cannot share
            # outstanding traffic, browser responses, or audit records.
            self.spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if any(self.spool_dir.iterdir()):
                raise BrowserBoundaryError("Browser spool must be empty for a new trial")
            (self.spool_dir / "requests").mkdir(mode=0o700)
            (self.spool_dir / "responses").mkdir(mode=0o700)
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            if self.audit_path.exists() and self.audit_path.stat().st_size:
                raise BrowserBoundaryError("Browser audit must be empty for a new trial")
            self._audit({"event": "browser_start", "runtime": self.runtime,
                         "image": self.image if self.runtime == "docker" else None})
            self._server_task = asyncio.create_task(self._serve_requests())
            try:
                self._client = await self._runtime.start()
                self.security_manifest = {**await self._runtime.verify_isolation(), "evidence_version": 2}
                await self._verify_interception_integrity()
                self._audit({"event": "browser_isolation_verified", "manifest": self.security_manifest})
            except BaseException:
                await self.aclose()
                raise

    async def initialize(self) -> dict[str, Any]:
        await self._ensure_started()
        assert self._client is not None
        return await self._client.initialize()

    async def list_tools(self) -> dict[str, Any]:
        await self._ensure_started()
        assert self._client is not None
        if self._catalog is None:
            result = await self._client.list_tools()
            tools = result.get("tools")
            if not isinstance(tools, list):
                raise BrowserBoundaryError("Playwright returned an invalid tool catalog")
            selected = [tool for tool in tools if tool.get("name") in BROWSER_TOOLS]
            names = {tool["name"] for tool in selected}
            if names != BROWSER_TOOLS:
                raise BrowserBoundaryError(f"Pinned Playwright catalog changed: missing {sorted(BROWSER_TOOLS - names)}")
            self._catalog = {**result, "tools": selected}
        return self._catalog

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, tool_call_id: str | None = None
    ) -> dict[str, Any]:
        if name not in BROWSER_TOOLS:
            raise BrowserBoundaryError(f"Browser tool is outside the selected published subset: {name}")
        await self.list_tools()
        async with self._call_lock:
            self._raise_if_failed()
            assert self._client is not None
            self._active_call = {"tool": name, "tool_call_id": tool_call_id or f"browser-{uuid.uuid4().hex}"}
            self._active_request_ids = []
            call = dict(self._active_call)
            completed = False
            self._audit({"event": "browser_tool_start", **call, "boundary_verified": self.security_manifest.get("interception_integrity", {}).get("verified", False)})
            try:
                result = await self._client.call_tool(
                    name, arguments, meta={"refund-eval/tool-call-id": call["tool_call_id"]}
                )
                self._raise_if_failed()
                completed = True
                return result
            except MCPRPCError as error:
                # An upstream error envelope is a completed response. Preserve
                # the native error and browser state so the agent can recover.
                completed = True
                self._audit({"event": "browser_rpc_error", **call, "error": error.error,
                             "rpc_response": error.response})
                raise
            except BaseException:
                # Cancellation/timeout ends the browser, including pending
                # callback work. Never leave an active page after a killed run.
                await self.aclose()
                raise
            finally:
                self._active_call = None
                try:
                    await self._drain_admitted_request()
                except BaseException:
                    await self.aclose()
                    raise
                finally:
                    self._audit({"event": "browser_tool_end", **call, "completed": completed,
                                 "request_ids": list(self._active_request_ids),
                                 "boundary_verified": self.security_manifest.get("interception_integrity", {}).get("verified", False)})

    async def _drain_admitted_request(self) -> None:
        """Let a request admitted to the ending call record its decision first.

        Playwright can return while that request still awaits host policy. Its
        decision and response belong inside the call's evidence window, so they
        must precede browser_tool_end. Later requests are already unattributed.
        """
        if self._closed or self._server_task is None or self._server_task.done():
            return  # aclose() has cancelled and awaited any handler.
        with contextlib.suppress(TimeoutError):
            # The handler bounds its policy call by the same timeout; this only
            # stops a wedged handler holding the tool call open indefinitely.
            await asyncio.wait_for(self._request_idle.wait(), timeout=self.request_timeout)

    async def _verify_interception_integrity(self) -> None:
        """Exercise the real browser without dispatching an experiment/model call."""
        assert self._client is not None
        probe_id = f"browser-probe-{uuid.uuid4().hex}"
        probe: dict[str, Any] = {"probe_id": probe_id, "request_ids": [],
                                 "request_observed": False, "listener_connections": 0}

        async def canary(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            probe["listener_connections"] += 1
            writer.close()
            await writer.wait_closed()

        listener = await asyncio.start_server(canary, "127.0.0.1", 0)
        port = listener.sockets[0].getsockname()[1]
        probe["url"] = f"http://127.0.0.1:{port}/{probe_id}"
        self._active_probe = probe
        self._audit({"event": "browser_probe_start", "probe_id": probe_id, "url": probe["url"]})
        completed = False
        try:
            result = await self._client.call_tool(
                "browser_navigate", {"url": probe["url"]}, meta={"refund-eval/tool-call-id": probe_id})
            self._raise_if_failed()
            if result.get("isError"):
                raise BrowserBoundaryError("Browser interception canary navigation failed")
            # Do not leave a diagnostic page or pending page resources in the
            # experiment's browser. about:blank requires no network dispatch.
            reset = await self._client.call_tool(
                "browser_navigate", {"url": "about:blank"}, meta={"refund-eval/tool-call-id": probe_id})
            if reset.get("isError"):
                raise BrowserBoundaryError("Browser interception canary cleanup failed")
            await asyncio.sleep(0.05)
            completed = True
        finally:
            listener.close()
            await listener.wait_closed()
            self._active_probe = None
            integrity = {
                "verified": bool(completed and probe["request_observed"] and probe["listener_connections"] == 0),
                "request_observed": probe["request_observed"],
                "listener_connections": probe["listener_connections"],
                "probe_id": probe_id, "request_ids": list(probe["request_ids"]),
            }
            self.security_manifest["interception_integrity"] = integrity
            self._audit({"event": "browser_probe_end", "probe_id": probe_id,
                         "completed": completed, "request_ids": list(probe["request_ids"]),
                         "boundary_verified": integrity["verified"], "integrity": integrity})
        if not integrity["verified"]:
            raise BrowserBoundaryError("Browser interception integrity check failed")

    def _handle_probe_request(self, request: dict[str, Any]) -> dict[str, Any]:
        assert self._active_probe is not None
        probe = self._active_probe
        request_id = request["request_id"]
        probe["request_ids"].append(request_id)
        request.pop("tool_call_id", None)
        request.update({"attribution": "startup_probe", "probe_id": probe["probe_id"]})
        self._audit({"event": "browser_probe_request", "probe_id": probe["probe_id"], "request": request})
        if request["url"] == probe["url"] and request["method"] == "GET" and not request.get("rejected"):
            probe["request_observed"] = True
            response = {"status": 200,
                        "headers": {"content-type": "text/html", "content-security-policy": "default-src 'none'"},
                        "body_base64": base64.b64encode(b"<title>Interception verified</title>").decode()}
        else:
            response = {"abort": True}
        self._audit({"event": "browser_probe_response", "probe_id": probe["probe_id"],
                     "request_id": request_id, "status": response.get("status"),
                     "aborted": bool(response.get("abort"))})
        return response

    def _raise_if_failed(self) -> None:
        if self._fatal is not None:
            raise BrowserBoundaryError(f"Browser boundary failed: {self._fatal}") from self._fatal
        if self._closed:
            raise BrowserBoundaryError("Browser service is closed")

    def _audit(self, event: dict[str, Any]) -> None:
        self._sequence += 1
        record = {"sequence": self._sequence, **event}
        if "tool_call_id" in record:
            record["call_id"] = record["tool_call_id"]
        # Persist before forwarding. Failure to record a request stops its
        # dispatch; a crash cannot erase previously admitted traffic.
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if self._audit_callback is not None:
            self._audit_callback(record)

    async def _serve_requests(self) -> None:
        try:
            while not self._closed:
                for filename in sorted((self.spool_dir / "requests").glob("*.json")):
                    if not REQUEST_FILE.fullmatch(filename.name):
                        self._audit({"event": "invalid_spool_filename", "filename": filename.name})
                        raise BrowserBoundaryError("Invalid browser request filename")
                    await self._handle_request(filename)
                await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — every boundary failure must stop admission.
            self._fatal = error

    async def _handle_request(self, filename: Path) -> None:
        request_id = filename.stem
        response: dict[str, Any] = {"abort": True}
        request: dict[str, Any] = {"request_id": request_id}
        self._request_idle.clear()
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
                    raise BrowserBoundaryError("Invalid or oversized browser request file")
                request = json.loads(stream.read(MAX_REQUEST_BYTES + 1))
            if not isinstance(request, dict) or request.get("request_id") != request_id:
                raise BrowserBoundaryError("Invalid browser request identity")
            for key in ("url", "method", "body_base64", "resource_type"):
                if not isinstance(request.get(key), str):
                    raise BrowserBoundaryError(f"Invalid browser request {key}")
            if not isinstance(request.get("headers"), dict):
                raise BrowserBoundaryError("Invalid browser request headers")
            probe = self._active_probe
            if probe is not None and request.get("tool_call_id") == probe["probe_id"]:
                response = self._handle_probe_request(request)
                return
            active = dict(self._active_call) if self._active_call else None
            if not active or request.get("tool_call_id") != active["tool_call_id"]:
                active = None
            request.update(active or {"tool": None, "tool_call_id": request.get("tool_call_id")})
            request["attribution"] = "active_tool_window" if active else "background_or_stale"
            if active:
                self._active_request_ids.append(request_id)
            self._audit({"event": "request", "request": request})
            if active is None:
                response = {"status": 503, "headers": {"content-type": "text/plain"},
                            "body_base64": base64.b64encode(b"Connection unavailable").decode()}
            elif request.get("rejected"):
                response = {"abort": True}
            else:
                response = await asyncio.wait_for(self.request_handler(request), timeout=self.request_timeout)
                response = validate_response(response)
            if active is None or request.get("rejected"):
                self._audit({"event": "browser_request", "call_id": request.get("tool_call_id"),
                             "request": request})
                self._audit({"event": "browser_decision", "call_id": request.get("tool_call_id"),
                             "request_id": request_id, "action": "blocked", "status": response.get("status", 0),
                             "url": request.get("url"), "method": request.get("method"),
                             "aborted": bool(response.get("abort"))})
            self._audit({"event": "response", "request_id": request_id,
                         "status": response.get("status"), "aborted": bool(response.get("abort")),
                         "tool_call_id": request.get("tool_call_id")})
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — callback errors abort; there is no network fallback.
            response = {"abort": True}
            self._audit({"event": "request_error", "request_id": request_id,
                         "error": f"{type(error).__name__}: {error}"})
        finally:
            try:
                output = self.spool_dir / "responses" / filename.name
                temporary = output.with_suffix(".tmp")
                # One producer; exclusive creation also refuses a malicious symlink.
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(response, stream)
                temporary.replace(output)
                filename.unlink(missing_ok=True)
            finally:
                self._request_idle.set()

    async def aclose(self) -> None:
        if self._closed:
            return
        # Teardown usually runs inside a cancelled anyio scope (Inspect time
        # limits, operator cancellation, Ctrl-C) that re-cancels every await.
        # Shield it so the MCP child, the container and the runtime tempdir are
        # always released; mark closed only after teardown so a failure retries.
        with anyio.CancelScope(shield=True):
            self._active_call = None
            if self._server_task is not None:
                self._server_task.cancel()
                await asyncio.gather(self._server_task, return_exceptions=True)
            try:
                if self._client is not None:
                    await self._client.aclose()
            finally:
                await self._runtime.aclose()
            self._closed = True

    async def __aenter__(self) -> Self:
        await self._ensure_started()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()


def validate_response(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        raise BrowserBoundaryError("Browser policy must return a response object")
    if response.get("abort") is True:
        return {"abort": True}
    if type(response.get("status")) is not int or not 200 <= response["status"] <= 599:
        raise BrowserBoundaryError("Browser policy returned an invalid HTTP status")
    headers = response.get("headers")
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise BrowserBoundaryError("Browser policy returned invalid headers")
    try:
        body = base64.b64decode(response.get("body_base64", ""), validate=True)
    except (ValueError, TypeError, binascii.Error) as error:
        raise BrowserBoundaryError("Browser policy returned invalid base64") from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise BrowserBoundaryError("Browser policy response exceeds its size limit")
    return response

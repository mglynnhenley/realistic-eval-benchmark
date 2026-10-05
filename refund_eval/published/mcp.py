"""Small raw MCP transports for published tools, without schema/result rewriting.

Implements the MCP initialization lifecycle and Streamable HTTP/stdio messages:
https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle
https://modelcontextprotocol.io/specification/2025-06-18/basic/transports

These clients are transport infrastructure, not a security sandbox or policy.
Callers must mediate tool calls and isolate subprocess filesystem/network access.
No automatic tool-call retries are made: a lost response is not proof that a
remote action did not happen.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

import anyio
import httpx

MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2024-11-05", "2025-03-26", MCP_PROTOCOL_VERSION}
MAX_RESPONSE_BYTES = 8_000_000


class MCPError(RuntimeError):
    """Transport or protocol failure, never a synthesized tool result."""


class MCPProtocolError(MCPError):
    """Malformed or unsupported MCP exchange."""


class MCPRPCError(MCPError):
    """A complete upstream JSON-RPC error, available as ``error``."""

    def __init__(self, error: dict[str, Any], *, response: dict[str, Any] | None = None):
        self.error = copy.deepcopy(error)
        self.response = copy.deepcopy(response)
        super().__init__(str(error.get("message", "MCP JSON-RPC error")))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _decode(value: bytes | str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except (ValueError, UnicodeError) as exc:
        raise MCPProtocolError("Invalid JSON from MCP server") from exc
    if not isinstance(parsed, dict) or parsed.get("jsonrpc") != "2.0":
        raise MCPProtocolError("Invalid JSON-RPC message from MCP server")
    return parsed


def process_parents() -> dict[int, int]:
    """Map every visible pid to its parent pid, from /proc or portable ps(1) output.

    Returns an empty mapping where neither source exists, so callers degrade to
    process-group signalling alone.
    """
    parents: dict[int, int] = {}
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                status = (entry / "stat").read_text()
            except OSError:
                continue
            # The command name may contain spaces or parentheses; fields follow it.
            _, _, fields = status.rpartition(")")
            parts = fields.split()
            if len(parts) > 1 and parts[1].isdigit():
                parents[int(entry.name)] = int(parts[1])
        if parents:
            return parents
    ps = shutil.which("ps")
    if ps is None:
        return {}
    try:
        output = subprocess.run([ps, "-eo", "pid=,ppid="], capture_output=True,
                                text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            parents[int(parts[0])] = int(parts[1])
    return parents


def descendant_pids(pid: int) -> set[int]:
    """Snapshot every descendant of ``pid`` right now, through any wrapper.

    Playwright starts Chromium detached in its own process group, so signalling
    the server's group alone can leave the browser alive; record its tree first.
    """
    children: dict[int, list[int]] = {}
    for child, parent in process_parents().items():
        children.setdefault(parent, []).append(child)
    found: set[int] = set()
    pending = [pid]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found and child != pid:
                found.add(child)
                pending.append(child)
    return found


def kill_processes(pids: Iterable[int]) -> None:
    """SIGKILL recorded descendants that outlived their group; ignore the gone."""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _result(message: dict[str, Any], request_id: int) -> dict[str, Any]:
    if (
        type(message.get("id")) is not int
        or message["id"] != request_id
        or "method" in message
        or ("result" in message) == ("error" in message)
    ):
        raise MCPProtocolError("MCP response did not match the request")
    if "error" in message:
        if not isinstance(message["error"], dict):
            raise MCPProtocolError("Invalid MCP error object")
        raise MCPRPCError(message["error"], response=message)
    if not isinstance(message["result"], dict):
        raise MCPProtocolError("MCP result must be an object")
    return message["result"]


class _MCPClient:
    def __init__(self, *, timeout: float, max_response_bytes: int):
        if timeout <= 0 or max_response_bytes < 1:
            raise ValueError("MCP timeout and response limit must be positive")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self._lock = asyncio.Lock()
        self._request_id = 0
        self._closed = False
        self._initialized = False
        self.protocol_version = MCP_PROTOCOL_VERSION
        self.initialization_result: dict[str, Any] | None = None
        self.server_info: dict[str, Any] | None = None
        self.catalog_pages: list[dict[str, Any]] = []
        self.notifications: deque[dict[str, Any]] = deque(maxlen=16)
        self.catalog_generation = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def initialize(self) -> dict[str, Any]:
        async with self._lock:
            await self._initialize_locked()
            return copy.deepcopy(self.initialization_result)

    async def _initialize_locked(self) -> None:
        if self._closed:
            raise MCPError("MCP client is closed")
        if self._initialized:
            return
        result = await self._request(
            "initialize",
            {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
             "clientInfo": {"name": "refund-eval", "version": "0.1.0"}},
        )
        version = result.get("protocolVersion")
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise MCPProtocolError("Server negotiated an unsupported MCP protocol version")
        if not isinstance(result.get("capabilities"), dict):
            raise MCPProtocolError("Server omitted MCP capabilities")
        if not isinstance(result.get("serverInfo"), dict):
            raise MCPProtocolError("Server omitted MCP serverInfo")
        self.protocol_version = version
        self.initialization_result = copy.deepcopy(result)
        self.server_info = copy.deepcopy(result["serverInfo"])
        await self._request("notifications/initialized", notification=True)
        self._initialized = True

    async def list_tools(self) -> dict[str, Any]:
        """Return all native descriptors; retain exact pages in catalog_pages.

        A single page is unchanged. For pagination only, concatenate ``tools``
        and consume ``nextCursor``; retain other first-page envelope fields.
        ``catalog_pages`` records every original response including metadata.
        """
        async with self._lock:
            await self._initialize_locked()
            pages: list[dict[str, Any]] = []
            cursor: str | None = None
            cursors: set[str] = set()
            names: set[str] = set()
            size = 0
            while True:
                page = await self._request("tools/list", {"cursor": cursor} if cursor else {})
                size += len(_json_bytes(page))
                if size > self.max_response_bytes:
                    raise MCPProtocolError("MCP catalog exceeded the response limit")
                tools = page.get("tools")
                if not isinstance(tools, list):
                    raise MCPProtocolError("MCP tools/list omitted tools")
                for tool in tools:
                    if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                        raise MCPProtocolError("Malformed MCP tool descriptor")
                    if tool["name"] in names:
                        raise MCPProtocolError("MCP catalog contains duplicate tool names")
                    names.add(tool["name"])
                pages.append(page)
                if "nextCursor" not in page:
                    break
                cursor = page["nextCursor"]
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise MCPProtocolError("Invalid or repeated MCP catalog cursor")
                cursors.add(cursor)
            self.catalog_pages = copy.deepcopy(pages)
            if len(pages) == 1:
                return pages[0]
            result = copy.deepcopy(pages[0])
            result.pop("nextCursor", None)
            result["tools"] = [tool for page in pages for tool in page["tools"]]
            return result

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise TypeError("MCP tool calls require a name and an arguments object")
        params = copy.deepcopy({"name": name, "arguments": arguments})
        if meta is not None:
            if not isinstance(meta, dict):
                raise TypeError("MCP request metadata must be an object")
            params["_meta"] = copy.deepcopy(meta)
        _json_bytes(params)
        async with self._lock:
            await self._initialize_locked()
            return await self._request("tools/call", params)

    def _notification(self, message: dict[str, Any]) -> None:
        if "id" in message or not isinstance(message.get("method"), str):
            raise MCPProtocolError("Unexpected MCP message")
        self.notifications.append(message)
        if message["method"] == "notifications/tools/list_changed":
            self.catalog_generation += 1

    async def _request(
        self, method: str, params: dict[str, Any] | None = None, *, notification: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if not notification:
            self._request_id += 1
            payload["id"] = self._request_id
        async with asyncio.timeout(self.timeout):
            return await self._exchange(payload)

    async def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError


class MCPHTTPClient(_MCPClient):
    """Streamable HTTP with explicit HTTPS endpoint/auth and no redirect/proxy fallback."""

    def __init__(
        self, endpoint: str, *, headers: Mapping[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ):
        super().__init__(timeout=timeout, max_response_bytes=max_response_bytes)
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint)):
            raise ValueError("MCP HTTP endpoint must be an explicit HTTPS URL without credentials or fragments")
        _ = parsed.port  # Reject malformed ports before creating a client.
        reserved = {"host", "content-length", "content-type", "accept", "mcp-session-id", "mcp-protocol-version"}
        if any(key.lower() in reserved for key in (headers or {})):
            raise ValueError("MCP transport headers cannot be overridden")
        self.endpoint = endpoint
        self._session_id: str | None = None
        self._client = httpx.AsyncClient(
            headers={"Accept": "application/json, text/event-stream", **dict(headers or {})},
            transport=transport, timeout=timeout, follow_redirects=False, trust_env=False,
        )

    async def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"MCP-Protocol-Version": self.protocol_version}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        async with self._client.stream("POST", self.endpoint, json=payload, headers=headers) as response:
            if response.status_code == 404:
                self._initialized = False
                self._session_id = None
            response.raise_for_status()
            if payload["method"] == "initialize":
                session_id = response.headers.get("Mcp-Session-Id")
                if session_id and not re.fullmatch(r"[!-~]+", session_id):
                    raise MCPProtocolError("Invalid MCP session ID")
                self._session_id = session_id
            if "id" not in payload:
                if response.status_code != 202:
                    raise MCPProtocolError("MCP notification was not accepted")
                return {}
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/json", "text/event-stream"}:
                raise MCPProtocolError("Unsupported MCP HTTP response type")
            buffer = bytearray()
            size = 0
            event_data: list[bytes] = []
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self.max_response_bytes:
                    raise MCPProtocolError("MCP response exceeded the size limit")
                buffer.extend(chunk)
                if content_type == "application/json":
                    continue
                while b"\n" in buffer:
                    line, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    line = bytes(line).removesuffix(b"\r")
                    if line.startswith(b"data:"):
                        event_data.append(line[5:].removeprefix(b" "))
                    elif not line and event_data:
                        message = _decode(b"\n".join(event_data))
                        event_data = []
                        if "id" in message:
                            return _result(message, payload["id"])
                        self._notification(message)
            if content_type == "application/json":
                return _result(_decode(bytes(buffer)), payload["id"])
            raise MCPProtocolError("MCP stream ended without a matching response")

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True
            await self._client.aclose()


class MCPStdioClient(_MCPClient):
    """Run an existing MCP executable with an isolated default environment.

    Explicit ``env`` replaces the entire child environment, never extends the
    parent environment. Without it, PATH/LANG and an empty temporary HOME/TMPDIR
    are supplied. This avoids accidental credential inheritance but does not
    prevent filesystem access; use an OS/container sandbox where required.
    Stderr is continuously drained into a bounded tail and never printed.
    """

    def __init__(
        self, command: Sequence[str], *, env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None, timeout: float = 30.0,
        shutdown_timeout: float = 2.0, max_response_bytes: int = MAX_RESPONSE_BYTES,
        stderr_limit: int = 64_000,
    ):
        super().__init__(timeout=timeout, max_response_bytes=max_response_bytes)
        if isinstance(command, (str, bytes)) or not command or any(not isinstance(x, str) or not x for x in command):
            raise ValueError("MCP command must be a nonempty argument sequence, never a shell string")
        if shutdown_timeout <= 0 or stderr_limit < 0:
            raise ValueError("Invalid subprocess shutdown timeout or stderr limit")
        self.command = tuple(command)
        self.env = dict(env) if env is not None else None
        self.cwd = str(cwd) if cwd is not None else None
        self.shutdown_timeout = shutdown_timeout
        self.stderr_limit = stderr_limit
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr = bytearray()
        self._temporary_home: tempfile.TemporaryDirectory | None = None
        self._broken = False

    @property
    def stderr_tail(self) -> str:
        return self._stderr.decode("utf-8", errors="replace")

    @property
    def process_id(self) -> int | None:
        return self._process.pid if self._process else None

    async def _start(self) -> None:
        if self._broken:
            raise MCPError("MCP subprocess failed; create a new client to restart")
        if self._process is not None:
            return
        child_env = self.env
        if child_env is None:
            self._temporary_home = tempfile.TemporaryDirectory(prefix="refund-mcp-")
            child_env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HOME": self._temporary_home.name,
                         "TMPDIR": self._temporary_home.name, "XDG_CONFIG_HOME": self._temporary_home.name}
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd, env=child_env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=self.max_response_bytes + 1,
                start_new_session=(os.name == "posix"),
            )
        except BaseException:
            if self._temporary_home:
                self._temporary_home.cleanup()
            raise
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        while chunk := await self._process.stderr.read(8192):
            if self.stderr_limit:
                self._stderr.extend(chunk)
                del self._stderr[:-self.stderr_limit]

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._process and self._process.stdin
        self._process.stdin.write(_json_bytes(payload) + b"\n")
        await self._process.stdin.drain()

    async def _exchange(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            await self._start()
            await self._send(payload)
            if "id" not in payload:
                return {}
            assert self._process and self._process.stdout
            size = 0
            while True:
                try:
                    line = await self._process.stdout.readline()
                except ValueError as exc:
                    raise MCPProtocolError("MCP stdout exceeded the size limit") from exc
                size += len(line)
                if size > self.max_response_bytes:
                    raise MCPProtocolError("MCP stdout exceeded the size limit")
                if not line:
                    raise MCPError("MCP subprocess closed stdout before responding")
                message = _decode(line)
                if "method" in message and "id" in message:
                    # We advertise no sampling, elicitation or filesystem roots.
                    reply = {"jsonrpc": "2.0", "id": message["id"]}
                    if message["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": "Client capability not supported"}
                    await self._send(reply)
                    continue
                if "id" in message:
                    return _result(message, payload["id"])
                self._notification(message)
        except MCPRPCError:
            raise  # A protocol error response does not desynchronize the stream.
        except BaseException:
            self._broken = True
            await self._stop()
            raise

    def _signal(self, sig: signal.Signals) -> None:
        if not self._process:
            return
        try:
            if os.name == "posix":
                os.killpg(self._process.pid, sig)
            elif self._process.returncode is None:
                self._process.send_signal(sig)
        except ProcessLookupError:
            pass

    async def _stop(self) -> None:
        # Shutdown often runs inside a cancelled anyio scope (Inspect time
        # limits, operator cancellation, Ctrl-C), which re-cancels every await.
        # Shield it: an unsignalled child and its browser would outlive the trial.
        with anyio.CancelScope(shield=True):
            process = self._process
            if process:
                # Record the tree before signalling; once the server dies, its
                # detached Chromium is reparented and no longer reachable from it.
                descendants = descendant_pids(process.pid) if os.name == "posix" else set()
                if process.stdin:
                    process.stdin.close()
                self._signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), self.shutdown_timeout)
                except TimeoutError:
                    self._signal(signal.SIGKILL)
                    await asyncio.wait_for(process.wait(), self.shutdown_timeout)
                finally:
                    # Also reap descendants that outlived the original executable,
                    # including those in their own process group.
                    self._signal(signal.SIGKILL)
                    kill_processes(descendants)
            if self._stderr_task:
                try:
                    await asyncio.wait_for(self._stderr_task, self.shutdown_timeout)
                except TimeoutError:
                    self._stderr_task.cancel()
                    await asyncio.gather(self._stderr_task, return_exceptions=True)
            if self._temporary_home:
                self._temporary_home.cleanup()

    async def aclose(self) -> None:
        with anyio.CancelScope(shield=True):
            async with self._lock:
                if self._closed:
                    return
                # Mark closed only once teardown finished, so a failed attempt
                # is retried by the next caller instead of silently skipped.
                await self._stop()
                self._closed = True

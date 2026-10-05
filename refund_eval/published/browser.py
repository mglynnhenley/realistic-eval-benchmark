"""Run the real Playwright MCP server with no container network.

Only a per-trial filesystem spool crosses the container boundary. A trusted host
callback must classify every request before resolving a hostname or forwarding
anything. CDP request interception gives us real page behavior and checks each
redirect; Docker's network=none contains any worker or tool which bypasses it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import shutil
import stat
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Self

from refund_eval.published.mcp import MCPRPCError, MCPStdioClient

PLAYWRIGHT_MCP_VERSION = "0.0.83"
DEFAULT_IMAGE = f"refund-eval-playwright:{PLAYWRIGHT_MCP_VERSION}"
RUNTIME_DIR = Path(__file__).with_name("browser_runtime")
MAX_REQUEST_BYTES = 3_000_000
MAX_RESPONSE_BYTES = 8_000_000
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


class BrowserBoundaryError(RuntimeError):
    """The browser boundary failed; continuing the trial is not supported."""


def docker_command(
    spool_dir: Path, container_name: str, *, image: str = DEFAULT_IMAGE,
    docker_binary: str = "docker", docker_host: str | None = None,
) -> list[str]:
    """No host networking, credentials, Docker socket, shared profile or host cwd."""
    return [docker_binary, *(["--host", docker_host] if docker_host else []),
        "run", "--rm", "-i", "--name", container_name,
        "--network", "none", "--dns", "127.0.0.1", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--pids-limit", "256", "--memory", "1g", "--cpus", "2",
        "--shm-size", "256m", "--user", f"{os.getuid()}:{os.getgid()}",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
        "--mount", f"type=bind,source={spool_dir},target=/exchange",
        image,
    ]


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
    ) -> None:
        self.request_handler = request_handler
        self.spool_dir = Path(spool_dir).resolve()
        self.image = image
        self.timeout = timeout
        self.request_timeout = request_timeout
        self._audit_callback = audit
        self.container_name = f"refund-browser-{uuid.uuid4().hex}"
        # Evidence is outside the mount: the browser cannot overwrite its audit.
        self.audit_path = self.spool_dir.parent / f"{self.spool_dir.name}.network.jsonl"
        self.security_manifest: dict[str, Any] = {"verified": False, "mode": "docker-network-none"}
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
        self._docker_binary = shutil.which("docker") or "docker"
        self._docker_host: str | None = None

    async def _ensure_started(self) -> None:
        async with self._start_lock:
            if self._closed:
                raise BrowserBoundaryError("Browser service is closed")
            if self._client is not None:
                return
            if shutil.which(self._docker_binary) is None:
                raise BrowserBoundaryError("Docker is required for the published browser")
            # A caller must choose a fresh directory, so two trials cannot share
            # outstanding traffic, browser responses, or audit records.
            self.spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if any(self.spool_dir.iterdir()):
                raise BrowserBoundaryError("Browser spool must be empty for a new trial")
            (self.spool_dir / "requests").mkdir(mode=0o700)
            (self.spool_dir / "responses").mkdir(mode=0o700)
            self._audit({"event": "browser_start", "image": self.image, "network": "none"})
            self._server_task = asyncio.create_task(self._serve_requests())
            # MCP children have an empty HOME. Resolve only the active daemon
            # endpoint now, then pass it explicitly, without docker auth/config.
            process = await asyncio.create_subprocess_exec(
                self._docker_binary, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
            except BaseException:
                process.kill()
                await process.wait()
                await self.aclose()
                raise
            if process.returncode != 0:
                await self.aclose()
                raise BrowserBoundaryError("Cannot resolve the active Docker daemon")
            self._docker_host = os.environ.get("DOCKER_HOST") or stdout.decode().strip()
            self._client = MCPStdioClient(
                docker_command(self.spool_dir, self.container_name, image=self.image,
                               docker_binary=self._docker_binary, docker_host=self._docker_host),
                timeout=self.timeout,
                max_response_bytes=MAX_RESPONSE_BYTES * 2,
            )
            try:
                await self._client.initialize()
                await self._verify_isolation()
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
            self._audit({"event": "browser_tool_start", **call, "boundary_verified": self.security_manifest["verified"]})
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
                self._audit({"event": "browser_tool_end", **call, "completed": completed,
                             "request_ids": list(self._active_request_ids),
                             "boundary_verified": self.security_manifest["verified"]})

    async def _docker_output(self, *arguments: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            self._docker_binary, *(["--host", self._docker_host] if self._docker_host else []),
            *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise BrowserBoundaryError(f"Docker boundary verification failed: {stderr.decode()[:500]}")
        return stdout

    async def _verify_isolation(self) -> None:
        inspected = json.loads(await self._docker_output("inspect", self.container_name))[0]
        host = inspected["HostConfig"]
        mounts = inspected.get("Mounts", [])
        valid_mount = (len(mounts) == 1 and mounts[0].get("Type") == "bind"
                       and mounts[0].get("Source") == str(self.spool_dir)
                       and mounts[0].get("Destination") == "/exchange")
        if (host.get("NetworkMode") != "none" or not host.get("ReadonlyRootfs")
                or host.get("Privileged") or "ALL" not in host.get("CapDrop", [])
                or host.get("Dns") != ["127.0.0.1"] or not valid_mount):
            raise BrowserBoundaryError("Browser container isolation does not match its required policy")
        runtime = json.loads(await self._docker_output("exec", self.container_name, "node", "-e",
            "console.log(JSON.stringify({interfaces:require('node:os').networkInterfaces(),"
            "version:require('/opt/browser/node_modules/@playwright/mcp/package.json').version}))"))
        interfaces = runtime["interfaces"]
        if runtime.get("version") != PLAYWRIGHT_MCP_VERSION:
            raise BrowserBoundaryError("Browser image contains an unexpected Playwright MCP version")
        if any(not item.get("internal") for values in interfaces.values() for item in values):
            raise BrowserBoundaryError("Browser container has a non-loopback network interface")
        self.security_manifest = {
            "verified": True, "mode": "docker-network-none", "network_mode": host["NetworkMode"],
            "readonly_rootfs": host["ReadonlyRootfs"], "privileged": host["Privileged"],
            "cap_drop": host["CapDrop"], "dns": host["Dns"],
            "image_id": inspected["Image"], "container_id": inspected["Id"],
            "network_interfaces": sorted(interfaces), "audit_path": str(self.audit_path),
            "package": "@playwright/mcp", "package_version": PLAYWRIGHT_MCP_VERSION,
        }
        self._audit({"event": "browser_isolation_verified", "manifest": self.security_manifest})

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
            output = self.spool_dir / "responses" / filename.name
            temporary = output.with_suffix(".tmp")
            # One producer; exclusive creation also refuses a malicious symlink.
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as stream:
                json.dump(response, stream)
            temporary.replace(output)
            filename.unlink(missing_ok=True)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._active_call = None
        if self._server_task is not None:
            self._server_task.cancel()
            await asyncio.gather(self._server_task, return_exceptions=True)
        try:
            if self._client is not None:
                await self._client.aclose()
        finally:
            await self._remove_container()

    async def _remove_container(self) -> None:
        # Reaping docker's client does not guarantee the container stopped.
        # Remove by our unpredictable per-trial name; never stop other workloads.
        if shutil.which("docker"):
            process = await asyncio.create_subprocess_exec(
                self._docker_binary, *(["--host", self._docker_host] if self._docker_host else []),
                "rm", "--force", self.container_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()

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

"""Browser process lifecycle, separate from request policy and audit dispatch."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Protocol

from refund_eval.published.mcp import MCPStdioClient

PLAYWRIGHT_MCP_VERSION = "0.0.83"
DEFAULT_IMAGE = f"refund-eval-playwright:{PLAYWRIGHT_MCP_VERSION}"
RUNTIME_DIR = Path(__file__).with_name("browser_runtime")
MAX_REQUEST_BYTES = 3_000_000
MAX_RESPONSE_BYTES = 8_000_000

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


class BrowserRuntime(Protocol):
    async def start(self) -> MCPStdioClient: ...
    async def verify_isolation(self) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


class DockerBrowserRuntime:
    """The original read-only Docker container with its network disabled."""

    def __init__(self, spool_dir: Path, *, image: str, timeout: float, audit_path: Path):
        self.spool_dir = spool_dir
        self.image = image
        self.timeout = timeout
        self.audit_path = audit_path
        self.container_name = f"refund-browser-{uuid.uuid4().hex}"
        self.client: MCPStdioClient | None = None
        self._docker_binary = shutil.which("docker") or "docker"
        self._docker_host: str | None = None

    async def start(self) -> MCPStdioClient:
        if shutil.which(self._docker_binary) is None:
            raise BrowserBoundaryError("Docker is required for the published browser")
        # Resolve the active endpoint before the child receives its empty HOME.
        process = await asyncio.create_subprocess_exec(
            self._docker_binary, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise BrowserBoundaryError("Cannot resolve the active Docker daemon")
        self._docker_host = os.environ.get("DOCKER_HOST") or stdout.decode().strip()
        self.client = MCPStdioClient(
            docker_command(self.spool_dir, self.container_name, image=self.image,
                           docker_binary=self._docker_binary, docker_host=self._docker_host),
            timeout=self.timeout, max_response_bytes=MAX_RESPONSE_BYTES * 2,
        )
        await self.client.initialize()
        return self.client

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

    async def verify_isolation(self) -> dict[str, Any]:
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
        return {
            "verified": True, "mode": "docker-network-none", "network_mode": host["NetworkMode"],
            "readonly_rootfs": host["ReadonlyRootfs"], "privileged": host["Privileged"],
            "cap_drop": host["CapDrop"], "dns": host["Dns"],
            "image_id": inspected["Image"], "container_id": inspected["Id"],
            "network_interfaces": sorted(interfaces), "audit_path": str(self.audit_path),
            "package": "@playwright/mcp", "package_version": PLAYWRIGHT_MCP_VERSION,
        }

    async def aclose(self) -> None:
        if self.client is not None:
            await self.client.aclose()
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


"""Browser process lifecycle, separate from request policy and audit dispatch."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Mapping
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


def require_verified_browser_isolation(manifest: Any) -> None:
    """Reject startup unless the browser manifest proves an isolated network."""
    if not isinstance(manifest, Mapping) or manifest.get("verified") is not True:
        raise BrowserBoundaryError("Browser network isolation is not verified")
    if manifest.get("network_mode") != "none":
        raise BrowserBoundaryError("Browser network isolation evidence is inconsistent")
    mode = manifest.get("mode")
    if mode == "docker-network-none":
        return
    if mode == "subprocess-netns" and manifest.get("namespace_verified") is True:
        return
    raise BrowserBoundaryError("Browser network isolation evidence is missing or unknown")


def resolve_local_image_id(image: str, *, docker_binary: str | None = None) -> str:
    """Resolve only an image already present in the active local daemon; never pull."""
    binary = docker_binary or shutil.which("docker")
    if binary is None:
        raise BrowserBoundaryError("Docker is unavailable; refusing to substitute a browser image")
    try:
        result = subprocess.run(
            [binary, "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BrowserBoundaryError("Cannot inspect the local browser image; refusing to pull or substitute") from error
    image_id = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise BrowserBoundaryError(
            f"Browser image {image!r} is unavailable locally; refusing to pull or substitute another image"
        )
    return image_id


def docker_command(
    spool_dir: Path, container_name: str, *, image: str = DEFAULT_IMAGE,
    docker_binary: str = "docker", docker_host: str | None = None,
) -> list[str]:
    """No host networking, credentials, Docker socket, shared profile or host cwd."""
    return [docker_binary, *(["--host", docker_host] if docker_host else []),
        "run", "--pull=never", "--rm", "-i", "--name", container_name,
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
        self.image_id: str | None = None

    async def start(self) -> MCPStdioClient:
        if shutil.which(self._docker_binary) is None:
            raise BrowserBoundaryError("Docker is required for the published browser")
        self.image_id = resolve_local_image_id(self.image, docker_binary=self._docker_binary)
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
            docker_command(self.spool_dir, self.container_name, image=self.image_id,
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
        if inspected.get("Image") != self.image_id:
            raise BrowserBoundaryError("Browser container is not using the resolved immutable image ID")
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


def subprocess_server_path() -> Path:
    """The Harbor image installs the pinned runtime at this stable location."""
    return Path(os.environ.get("REFUND_EVAL_BROWSER_SERVER", "/opt/browser/server.mjs")).resolve()


def subprocess_environment(home: Path, spool_dir: Path, workspace: Path, output: Path) -> dict[str, str]:
    """Build a fresh allowlisted environment without model or provider credentials."""
    return {
        "PATH": os.defpath,
        "HOME": str(home),
        "NODE_ENV": "production",
        "PLAYWRIGHT_BROWSERS_PATH": os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/playwright-browsers"),
        "REFUND_EVAL_BROWSER_EXCHANGE": str(spool_dir),
        "REFUND_EVAL_BROWSER_WORKSPACE": str(workspace),
        "REFUND_EVAL_BROWSER_OUTPUT": str(output),
    }


def subprocess_readiness() -> dict[str, Any]:
    """Check Node, the installed package pin and Chromium without starting a trial."""
    import subprocess
    import tempfile

    node = shutil.which("node")
    server = subprocess_server_path()
    if not node:
        return {"configured": False, "reason": "Browser runtime: Node is not installed in the trial sandbox."}
    if not server.is_file():
        return {"configured": False, "reason": f"Browser runtime: packaged server is missing: {server}"}
    script = (
        "const fs=require('node:fs');const pkg=require('@playwright/mcp/package.json');"
        "const executable=require('playwright').chromium.executablePath();"
        "console.log(JSON.stringify({package_version:pkg.version,chromium_executable:executable,"
        "chromium_present:fs.existsSync(executable)}));"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="refund-browser-check-") as directory:
            path = Path(directory)
            result = subprocess.run(
                [node, "-e", script], cwd=server.parent,
                env=subprocess_environment(path, path, path, path),
                capture_output=True, timeout=10, check=False,
            )
        if result.returncode:
            return {"configured": False, "reason": "Browser runtime: pinned Node packages are missing or cannot load."}
        info = json.loads(result.stdout)
        if info.get("package_version") != PLAYWRIGHT_MCP_VERSION:
            return {"configured": False, "reason": f"Browser runtime: expected @playwright/mcp {PLAYWRIGHT_MCP_VERSION}."}
        if info.get("chromium_present") is not True:
            return {"configured": False, "reason": "Browser runtime: packaged Chromium executable is missing."}
        return {"configured": True, "reason": "Pinned browser runtime and Chromium are installed.", **info}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"configured": False, "reason": "Browser runtime: packaged component check failed."}


# Kernels with built-in tunnel drivers (Modal's VMs) add these fallback devices to every new network
# namespace; they have no addresses or routes, so the namespace is still loopback-only.
FALLBACK_TUNNELS = frozenset({"tunl0", "gre0", "gretap0", "erspan0", "ip_vti0", "ip6_vti0", "sit0", "ip6tnl0",
                              "ip6gre0"})


def _loopback_only(process_id: int, interfaces: list[str]) -> bool:
    if interfaces == ["lo"]:
        return True
    if "lo" not in interfaces or not set(interfaces) - {"lo"} <= FALLBACK_TUNNELS:
        return False
    routes = Path(f"/proc/{process_id}/net/route").read_text().splitlines()[1:]
    return not any(line.strip() for line in routes)


class SubprocessBrowserRuntime:
    """Run browser and MCP inside the trial sandbox, with explicit isolation evidence.

    Linux user/network namespaces are preferred when the host permits them. A
    sandbox that rejects namespaces runs with the unreachable Chromium proxy;
    its manifest explicitly does not claim OS network isolation.
    """

    def __init__(self, spool_dir: Path, *, timeout: float, audit_path: Path, output_dir: Path | None = None):
        self.spool_dir = spool_dir
        self.timeout = timeout
        self.audit_path = audit_path
        # Where the browser writes its files (console logs and their aliases); None: a temporary directory.
        self.output_dir = output_dir
        self.client: MCPStdioClient | None = None
        self._directories: Any = None
        self._namespace = False
        self._namespace_reason = "Network namespaces are unavailable on this host."
        self._component_info: dict[str, Any] = {}

    async def _namespace_available(self, env: dict[str, str]) -> bool:
        unshare = shutil.which("unshare")
        if not unshare or not Path("/proc/self/ns/net").exists():
            return False
        process = await asyncio.create_subprocess_exec(
            unshare, "-rn", "true", env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        self._namespace_reason = stderr.decode(errors="replace").strip()[:500]
        return process.returncode == 0

    async def start(self) -> MCPStdioClient:
        import tempfile

        self._component_info = await asyncio.to_thread(subprocess_readiness)
        if not self._component_info["configured"]:
            raise BrowserBoundaryError(self._component_info["reason"])
        self._directories = tempfile.TemporaryDirectory(prefix="refund-browser-")
        directory = Path(self._directories.name)
        home, workspace, output = (directory / name for name in ("home", "workspace", "output"))
        for path in (home, workspace, output):
            path.mkdir(mode=0o700)
        if self.output_dir is not None:
            output = self.output_dir
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
        env = subprocess_environment(home, self.spool_dir, workspace, output)
        self._namespace = await self._namespace_available(env)
        command = [shutil.which("node") or "node", str(subprocess_server_path())]
        if self._namespace:
            command = [shutil.which("unshare") or "unshare", "-rn", *command]
        self.client = MCPStdioClient(command, env=env, cwd=workspace, timeout=self.timeout,
                                     max_response_bytes=MAX_RESPONSE_BYTES * 2)
        try:
            await self.client.initialize()
        except BaseException as error:
            if isinstance(error, Exception):
                raise BrowserBoundaryError(
                    f"Browser startup failed: {error}; {self.client.stderr_tail[-2000:]}"
                ) from error
            raise
        return self.client

    async def verify_isolation(self) -> dict[str, Any]:
        assert self.client is not None
        process_id = self.client.process_id
        manifest: dict[str, Any] = {
            "verified": False,
            "mode": "subprocess-shared-network", "network_mode": "shared",
            "namespace_verified": False, "namespace_unavailable_reason": self._namespace_reason,
            "process_id": process_id, "audit_path": str(self.audit_path),
            "package": "@playwright/mcp", "package_version": PLAYWRIGHT_MCP_VERSION,
            "chromium_executable": self._component_info["chromium_executable"],
            "unreachable_proxy": "http://127.0.0.1:9", "proxy_bypass_list": "<-loopback>",
        }
        if self._namespace:
            try:
                namespace = os.readlink(f"/proc/{process_id}/ns/net")
                worker_namespace = os.readlink("/proc/self/ns/net")
                # /proc retains loopback even if it is down and has no addresses.
                devices = Path(f"/proc/{process_id}/net/dev").read_text().splitlines()[2:]
                interfaces = sorted(line.split(":", 1)[0].strip() for line in devices)
            except OSError as error:
                raise BrowserBoundaryError("Cannot verify browser network namespace") from error
            if namespace == worker_namespace or not _loopback_only(process_id, interfaces):
                raise BrowserBoundaryError("Browser network namespace is not isolated")
            manifest.update({"verified": True, "mode": "subprocess-netns", "network_mode": "none",
                             "namespace_verified": True, "namespace_id": namespace,
                             "worker_namespace_id": worker_namespace,
                             "network_interfaces": [name for name in interfaces if name not in FALLBACK_TUNNELS]})
            if fallback := [name for name in interfaces if name in FALLBACK_TUNNELS]:
                manifest["fallback_tunnels_without_routes"] = fallback
            manifest.pop("namespace_unavailable_reason", None)
        return manifest

    async def aclose(self) -> None:
        try:
            if self.client is not None:
                # MCPStdioClient signals the server's process group and then
                # kills descendants it recorded beforehand, which covers the
                # Chromium that Playwright starts in its own detached group.
                await self.client.aclose()
        finally:
            if self._directories is not None:
                self._directories.cleanup()
                self._directories = None

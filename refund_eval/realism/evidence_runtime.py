"""Unpaid evidence tools shared by the continuous loop's investigators.

Reproductions run in disposable copies of committed source with credentials
removed. This provides working-directory isolation, not a hostile-code sandbox;
the same authorized coding agents already have local execution access.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

MAX_REFERENCE_BYTES = 2_000_000
MAX_REPRODUCTION_BYTES = 100_000
MAX_OUTPUT_BYTES = 2_000_000


async def _public_url(url: str) -> None:
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password):
        raise ValueError("A public HTTP(S) URL without credentials is required")
    addresses = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, parsed.port or 443)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Reference host must resolve to public addresses")


async def public_reference(url: str) -> dict:
    """Fetch a complete public text response, or record why it was inaccessible."""
    requested = url
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            for _ in range(6):
                await _public_url(url)
                async with client.stream("GET", url, headers={"User-Agent": "RealismEvidence/1.0"}) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Reference redirect has no destination")
                        url = str(response.url.join(location))
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "")
                    if not any(kind in content_type for kind in ("text/", "json", "xml")):
                        return {"status": "inaccessible", "url": requested,
                                "reason": "Reference is not text; no content was silently omitted"}
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_REFERENCE_BYTES:
                            return {"status": "inaccessible", "url": requested,
                                    "reason": "Reference exceeds the text limit; no partial content returned"}
                    return {"status": "complete", "url": requested, "resolved_url": str(response.url),
                            "content_type": content_type, "bytes": len(data),
                            "content": bytes(data).decode(response.encoding or "utf-8", errors="strict")}
            raise ValueError("Reference exceeded the redirect limit")
    except (ValueError, OSError, httpx.HTTPError) as error:
        return {"status": "inaccessible", "url": requested, "reason": str(error)}


def _source_copy(source_root: Path, destination: Path) -> str:
    """Export tracked application/test inputs, excluding local evidence and secrets."""
    commit = subprocess.run(["git", "-C", str(source_root), "rev-parse", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    archive = destination.parent / "source.tar"
    with archive.open("wb") as output:
        subprocess.run(["git", "-C", str(source_root), "archive", commit, "--", "refund_eval",
                        "tests", "pyproject.toml", "uv.lock", "README.md", "LICENSE"],
                       stdout=output, stderr=subprocess.PIPE, check=True)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if any(not (item.isfile() or item.isdir()) for item in members):
            raise ValueError("Reproduction source contains links or special files")
        source.extractall(destination, members=members, filter="data")
    archive.unlink()
    return commit


def isolated_reproducer(source_root: Path, directory: Path):
    """Return the same isolated Python experiment capability for every investigator."""
    source_root, directory = Path(source_root).resolve(), Path(directory).resolve()

    async def reproduce(code: str | None = None, *, experiment: dict | None = None,
                        scratch: Path | None = None) -> dict:
        if experiment is not None:
            code = experiment.get("code")
        if not isinstance(code, str) or len(code.encode()) > MAX_REPRODUCTION_BYTES:
            return {"status": "inaccessible", "reason": "Reproduction needs bounded Python source in experiment.code"}
        destination = Path(scratch).resolve() if scratch is not None else directory
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        scratch = Path(tempfile.mkdtemp(prefix="experiment-", dir=destination))
        working = scratch / "source"
        working.mkdir()
        try:
            commit = await asyncio.to_thread(_source_copy, source_root, working)
            script = scratch / "experiment.py"
            script.write_text(code)
            script.chmod(0o600)
            environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                           "PYTHON_DOTENV_DISABLED": "1", "MODAL_CONFIG_PATH": "/dev/null",
                           "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
            wrapper = "import runpy, sys; sys.path.insert(0, sys.argv[1]); runpy.run_path(sys.argv[2], run_name='__main__')"
            output_path = scratch / "output.txt"
            with output_path.open("wb") as output:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-I", "-c", wrapper, str(working), str(script),
                    cwd=working, env=environment, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True)
                try:
                    await asyncio.wait_for(process.wait(), timeout=60)
                except TimeoutError:
                    return {"status": "inaccessible", "reason": "Reproduction timed out",
                            "scratch": str(scratch), "output_path": str(output_path)}
                finally:
                    # The experiment may have spawned children even if its main
                    # process completed. End its entire process group.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await process.wait()
            if output_path.stat().st_size > MAX_OUTPUT_BYTES:
                return {"status": "inaccessible", "reason": "Output exceeds limit; full output saved, none truncated",
                        "scratch": str(scratch), "output_path": str(output_path)}
            return {"status": "complete", "source_commit": commit, "returncode": process.returncode,
                    "output": output_path.read_text(errors="replace"), "scratch": str(scratch)}
        except (ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
            return {"status": "inaccessible", "reason": str(error), "scratch": str(scratch)}

    return reproduce

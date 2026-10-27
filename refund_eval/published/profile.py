"""Explicit published-tool profile; no credential/provider-dependent fallback."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from functools import lru_cache
from pathlib import Path

from inspect_ai.util import store

from refund_eval.config import HYBRID_EXA_KEY_ENV, RunConfig
from refund_eval.output_paths import validate_private_output
from refund_eval.published.adapter import audit, fingerprint, inspect_tools
from refund_eval.published.browser_runtimes import (
    DEFAULT_IMAGE as DEFAULT_BROWSER_IMAGE,
)
from refund_eval.scenario.schema import Scenario
from refund_eval.world.interception import ToolContext, current_tool_call_id
from refund_eval.world.mail import FixtureMailSource, Mailbox, make_mail_source

PROFILE_KEY = "published_tool_profile"


def validate_fixture_path(fixture_path: str | None, *, data_mode: str) -> str | None:
    """Resolve an explicit inbox and fail before provider/runtime setup."""
    if fixture_path is None:
        return None
    if data_mode not in {"fixture", "hybrid"}:
        raise ValueError("fixture_path requires published_data='fixture' or 'hybrid'")
    path = Path(fixture_path).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Fixture inbox file does not exist: {path}")
    try:
        FixtureMailSource(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        raise ValueError(f"Invalid fixture inbox at {path}: {error}") from error
    return str(path)


def fixture_readiness(*, browser_runtime: str = "docker") -> dict:
    """Simulated data needs only the local, network-isolated browser runtime."""
    if browser_runtime == "subprocess":
        from refund_eval.published.browser_runtimes import subprocess_readiness

        return subprocess_readiness()
    if browser_runtime != "docker":
        return {"configured": False, "reason": f"Unknown browser runtime: {browser_runtime}"}
    return dict(_fixture_readiness(int(time.monotonic() // 5)))


def hybrid_readiness(*, browser_runtime: str = "docker") -> dict:
    """Check only the local browser; no Gmail config or live requests are needed."""
    status = fixture_readiness(**({"browser_runtime": browser_runtime} if browser_runtime != "docker" else {}))
    if status["configured"]:
        status["reason"] = (
            "Simulated inbox and public web browser are ready; no Gmail connection is needed. "
            + ("Live Exa search uses the REFUND_EVAL_EXA_API_KEY account" if os.environ.get(HYBRID_EXA_KEY_ENV)
               else "Live Exa search uses keyless access with provider rate limits; set REFUND_EVAL_EXA_API_KEY to use an account")
            + "; contracts are checked at startup."
        )
    return status


@lru_cache(maxsize=2)
def _fixture_readiness(_time_bucket: int) -> dict:
    if not shutil.which("docker"):
        return {"configured": False, "reason": "Install and start Docker to run the simulated browser."}
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", DEFAULT_BROWSER_IMAGE],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"configured": False, "reason": "Docker is not responding. Start Docker and retry."}
    if result.returncode:
        return {
            "configured": False,
            "reason": "Start Docker and build the browser image: "
                      f"docker build -t {DEFAULT_BROWSER_IMAGE} refund_eval/published/browser_runtime",
        }
    return {"configured": True, "reason": "Simulated inbox and websites are ready; no Gmail connection is needed."}


def allow_search_request(request: dict) -> bool:
    """Every query goes out and is audited, the user's own details included:
    privacy is graded from the audit, not enforced by a block. Only the argument
    shape is checked and credential-shaped text withheld."""

    args = request.get("arguments", {})
    if set(args) - {"query", "objective", "numResults"}:
        return False
    values = [args.get("query"), args.get("objective")]
    if not all(isinstance(value, str) and value.strip() for value in values):
        return False
    text = " ".join(values).casefold()
    return not re.search(r"(?:token|password|secret)\s*[:=]", text)


@asynccontextmanager
async def published_tools(
    scenario: Scenario, *, data_mode: str = RunConfig.model_fields["data_mode"].default,
    fixture_path: str | None = None, browser_runtime: str = "docker",
    artifact_dir: str | None = None,
):
    from refund_eval.published.browser import PublishedBrowserService
    from refund_eval.published.fixtures import (
        FIXTURE_MAIL_PROVENANCE,
        make_fixture_mail,
    )
    from refund_eval.published.network import BrowserBoundary
    from refund_eval.published.search import ExaSearchService

    if data_mode not in {"fixture", "hybrid"}:
        raise ValueError("published_data must be 'fixture' or 'hybrid'")
    fixture_path = validate_fixture_path(fixture_path, data_mode=data_mode)
    artifact_root = validate_private_output(artifact_dir or ".context/artifacts")
    artifacts = validate_private_output(artifact_root / "browser")
    runtime_args = {"browser_runtime": browser_runtime} if browser_runtime != "docker" else {}
    status = (fixture_readiness(**runtime_args) if data_mode == "fixture" else
              hybrid_readiness(**runtime_args))
    if not status["configured"]:
        raise ValueError(status["reason"])
    from refund_eval.runtime.snapshots import emit_snapshot

    emit_snapshot(phase="checking_tools")
    source = FixtureMailSource(fixture_path) if fixture_path is not None else make_mail_source("fixture")
    ctx = ToolContext(scenario, Mailbox(source, scenario))
    mode_details = ({"mail_data_mode": "fixture", "web_data_mode": "live",
                     "web_time_basis": "wall_clock",
                     "search_auth": "api_key" if os.environ.get(HYBRID_EXA_KEY_ENV) else "keyless"}
                    if data_mode == "hybrid" else {})
    inbox_provenance = {
        "name": source.path.name,
        "sha256": hashlib.sha256(source.path.read_bytes()).hexdigest(),
    }
    browser_image = DEFAULT_BROWSER_IMAGE

    if data_mode == "fixture":
        from refund_eval.published.fixture_web import (
            FIXTURE_WEB_DATASET,
            FixtureSearchService,
            fixture_web_config,
        )
        boundary_config = fixture_web_config(ctx)
        profile_config = {
            "data_mode": "fixture", "mail": FIXTURE_MAIL_PROVENANCE,
            "web_dataset": FIXTURE_WEB_DATASET, "browser_image": browser_image,
            "web_sha256": fingerprint(boundary_config),
            "inbox": inbox_provenance,
        }
    else:
        boundary_config = {"public_read_policy": "public_web", "public_read_urls": []}
        profile_config = {
            "data_mode": "hybrid", **mode_details,
            "mail": FIXTURE_MAIL_PROVENANCE, "browser_image": browser_image,
            "public_read_policy": "public_web", "search_provider": "exa",
            "inbox": inbox_provenance,
        }
    store().set(
        PROFILE_KEY,
        {
            "name": "published",
            "data_mode": data_mode,
            "fixture_provenance": profile_config,
            **mode_details,
            "config_sha256": fingerprint(profile_config),
            "browser_network": "unverified",
            "browser_runtime": browser_runtime,
            "public_read_policy": boundary_config.get("public_read_policy", "reviewed_urls"),
            "public_read_urls": boundary_config["public_read_urls"],
        },
    )
    async with AsyncExitStack() as stack:
        trial_dir = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="refund-browser-"))
        )
        spool = trial_dir / "exchange"
        mail = make_fixture_mail(ctx, audit=audit)
        stack.push_async_callback(mail.aclose)
        # Fail Gmail capability checks before starting the browser or paid models.
        tools = await inspect_tools(mail, source="gmail")

        def search_audit(event):
            audit(
                {"event": "search_boundary", "call_id": current_tool_call_id(), **event}
            )

        if data_mode == "fixture":
            search = FixtureSearchService(ctx, policy=allow_search_request, audit=search_audit)
        else:
            search = ExaSearchService(
                policy=allow_search_request,
                audit=search_audit,
                api_key=os.environ.get(HYBRID_EXA_KEY_ENV),
            )
        stack.push_async_callback(search.aclose)
        tools.extend(await inspect_tools(search, source="exa", live_web=data_mode != "fixture"))
        artifacts.mkdir(parents=True, exist_ok=True)
        network_audit = artifacts / f"{uuid.uuid4().hex}.network.jsonl"
        emit_snapshot(phase="starting_browser")
        browser = PublishedBrowserService(
            BrowserBoundary(ctx, **boundary_config),
            spool_dir=spool,
            image=browser_image,
            runtime=browser_runtime,
            audit_path=network_audit,
            audit=audit,
        )
        stack.push_async_callback(browser.aclose)
        # No date shift for the browser: its clock already shows the simulated date, and shifting
        # page text again would move calendar labels away from the dates they select.
        tools.extend(await inspect_tools(browser, source="playwright"))
        manifest = getattr(browser, "security_manifest", {}) or {}
        profile = store().get(PROFILE_KEY)
        profile["browser_network"] = manifest.get("network_mode", "unverified")
        profile["browser_audit"] = str(network_audit.relative_to(artifacts.parent))
        profile["browser_runtime"] = browser_runtime
        profile["browser_boundary"] = manifest
        if manifest.get("evidence_version") == 2:
            profile["evidence_version"] = 2
        store().set(PROFILE_KEY, profile)
        names = [t.name for t in tools]
        if len(set(names)) != len(names):
            raise ValueError("Published MCP catalogs contain duplicate tool names")
        audit(
            {
                "event": "profile_ready",
                "data_mode": data_mode,
                **mode_details,
                "public_read_policy": boundary_config.get("public_read_policy", "reviewed_urls"),
                "tools": names,
                "browser_boundary": getattr(browser, "security_manifest", None),
            }
        )
        emit_snapshot(phase="running")
        yield tools

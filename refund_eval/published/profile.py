"""Explicit published-tool profile; no credential/provider-dependent fallback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import AsyncExitStack, asynccontextmanager
from functools import lru_cache
from pathlib import Path

from inspect_ai.util import store
from pydantic import BaseModel, ConfigDict, Field

from refund_eval.published.adapter import audit, fingerprint, inspect_tools
from refund_eval.scenario.schema import Scenario
from refund_eval.world.interception import ToolContext, current_tool_call_id
from refund_eval.world.mail import FixtureMailSource, Mailbox, make_mail_source

PROFILE_KEY = "published_tool_profile"
DEFAULT_CONFIG = ".secrets/published-tools.json"
DEFAULT_BROWSER_IMAGE = "refund-eval-playwright:0.0.83"


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


class PublishedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gmail_catalog: str
    gmail_token_env: str = "GMAIL_MCP_ACCESS_TOKEN"
    gmail_project_env: str = "GOOGLE_CLOUD_PROJECT"
    exa_key_env: str = "EXA_API_KEY"
    public_read_urls: list[str] = Field(default_factory=list)
    simulated_form_urls: list[str] = Field(default_factory=list)
    browser_image: str = DEFAULT_BROWSER_IMAGE


def load_config(path: str | None = None) -> PublishedConfig:
    location = (
        Path(path or os.environ.get("REFUND_EVAL_PUBLISHED_CONFIG", DEFAULT_CONFIG))
        .expanduser()
        .resolve()
    )
    if not location.is_file():
        raise ValueError(
            f"Published tools need a config file at {location}. See docs/published-tools.md."
        )
    config = PublishedConfig.model_validate_json(location.read_text())
    catalog = Path(config.gmail_catalog).expanduser()
    if not catalog.is_absolute():
        catalog = location.parent / catalog
    config.gmail_catalog = str(catalog.resolve())
    return config


def readiness(path: str | None = None) -> dict:
    """Local configuration checks only; live contracts are verified on startup."""
    try:
        config = load_config(path)
        if not Path(config.gmail_catalog).is_file():
            raise ValueError(
                "Gmail catalog has not been captured. Run the discovery command first."
            )
        if not os.environ.get(config.gmail_token_env):
            raise ValueError(
                f"Gmail is not connected: {config.gmail_token_env} is missing."
            )
    except (OSError, ValueError) as exc:
        return {"configured": False, "reason": str(exc)}
    return {
        "configured": True,
        "reason": "Configured; live Gmail, Exa and browser contracts checked before model execution.",
    }


def fixture_readiness() -> dict:
    """Simulated data needs only the local, network-isolated browser runtime."""
    return dict(_fixture_readiness(int(time.monotonic() // 5)))


def hybrid_readiness() -> dict:
    """Check only the local browser; no Gmail config or live requests are needed."""
    status = fixture_readiness()
    if status["configured"]:
        status["reason"] = (
            "Simulated inbox and public web browser are ready; no Gmail connection is needed. "
            "Live Exa search uses keyless access with provider rate limits; contracts are checked at startup."
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
                      "docker build -t refund-eval-playwright:0.0.83 refund_eval/published/browser_runtime",
        }
    return {"configured": True, "reason": "Simulated inbox and websites are ready; no Gmail connection is needed."}


def search_policy(scenario: Scenario):
    # An explicit basic disclosure policy, not a claim to prevent every possible
    # encoding of personal information. Exact outbound arguments remain audited.
    private = [
        *scenario.user.email_addresses,
        scenario.user.name,
        scenario.user.phone,
        scenario.journey.booking_ref,
    ]
    private = [str(v).casefold() for v in private if v]

    def allow(request: dict) -> bool:
        args = request.get("arguments", {})
        if set(args) - {"query", "objective", "numResults"}:
            return False
        values = [args.get("query"), args.get("objective")]
        if not all(isinstance(value, str) and value.strip() for value in values):
            return False
        text = " ".join(values).casefold()
        return not (
            any(value in text for value in private)
            or re.search(
                r"[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:token|password|secret)\s*[:=]", text
            )
        )

    return allow


@asynccontextmanager
async def published_tools(
    scenario: Scenario, *, config_path: str | None = None, data_mode: str = "live",
    fixture_path: str | None = None,
):
    from refund_eval.published.browser import PublishedBrowserService
    from refund_eval.published.mail import GMAIL_ENDPOINT, GmailService
    from refund_eval.published.mcp import MCPHTTPClient
    from refund_eval.published.network import BrowserBoundary
    from refund_eval.published.search import ExaSearchService

    if data_mode not in {"fixture", "hybrid", "live"}:
        raise ValueError("published_data must be 'fixture', 'hybrid' or 'live'")
    fixture_path = validate_fixture_path(fixture_path, data_mode=data_mode)
    status = (fixture_readiness() if data_mode == "fixture" else
              hybrid_readiness() if data_mode == "hybrid" else readiness(config_path))
    if not status["configured"]:
        raise ValueError(status["reason"])
    source = FixtureMailSource(fixture_path) if fixture_path is not None else make_mail_source("fixture")
    ctx = ToolContext(scenario, Mailbox(source, scenario))
    mode_details = ({"mail_data_mode": "fixture", "web_data_mode": "live",
                     "web_time_basis": "wall_clock", "search_auth": "keyless"}
                    if data_mode == "hybrid" else {})
    if data_mode in {"fixture", "hybrid"}:
        from refund_eval.published.fixtures import (
            FIXTURE_MAIL_PROVENANCE,
            make_fixture_mail,
        )

    if data_mode == "fixture":
        from refund_eval.published.fixture_web import (
            FIXTURE_WEB_DATASET,
            FixtureSearchService,
            fixture_web_config,
        )
        boundary_config = fixture_web_config(ctx)
        browser_image = DEFAULT_BROWSER_IMAGE
        profile_config = {
            "data_mode": "fixture", "mail": FIXTURE_MAIL_PROVENANCE,
            "web_dataset": FIXTURE_WEB_DATASET, "browser_image": browser_image,
            "web_sha256": fingerprint(boundary_config),
            **({"inbox": {"path": fixture_path,
                          "sha256": hashlib.sha256(Path(fixture_path).read_bytes()).hexdigest()}}
               if fixture_path is not None else {}),
        }
    elif data_mode == "hybrid":
        boundary_config = {
            "public_read_policy": "public_web", "public_read_urls": [], "simulated_form_urls": [],
        }
        browser_image = DEFAULT_BROWSER_IMAGE
        profile_config = {
            "data_mode": "hybrid", **mode_details,
            "mail": FIXTURE_MAIL_PROVENANCE, "browser_image": browser_image,
            "public_read_policy": "public_web", "search_provider": "exa",
            "inbox": {"path": str(source.path.resolve()),
                      "sha256": hashlib.sha256(source.path.read_bytes()).hexdigest()},
        }
    else:
        config = load_config(config_path)
        snapshot = json.loads(Path(config.gmail_catalog).read_text())
        boundary_config = {
            "public_read_urls": config.public_read_urls,
            "simulated_form_urls": config.simulated_form_urls,
        }
        browser_image = config.browser_image
        profile_config = config.model_dump()
    store().set(
        PROFILE_KEY,
        {
            "name": "published",
            "data_mode": data_mode,
            **({"fixture_provenance": profile_config} if data_mode in {"fixture", "hybrid"} else {}),
            **mode_details,
            "config_sha256": fingerprint(profile_config),
            "browser_network": "none",
            "public_read_policy": boundary_config.get("public_read_policy", "reviewed_urls"),
            "public_read_urls": boundary_config["public_read_urls"],
            "simulated_form_urls": boundary_config["simulated_form_urls"],
        },
    )
    async with AsyncExitStack() as stack:
        trial_dir = Path(
            stack.enter_context(tempfile.TemporaryDirectory(prefix="refund-browser-"))
        )
        spool = trial_dir / "exchange"
        if data_mode in {"fixture", "hybrid"}:
            mail = make_fixture_mail(ctx, audit=audit)
        else:
            headers = {"Authorization": f"Bearer {os.environ[config.gmail_token_env]}"}
            if project := os.environ.get(config.gmail_project_env):
                headers["x-goog-user-project"] = project
            remote = MCPHTTPClient(GMAIL_ENDPOINT, headers=headers)
            stack.push_async_callback(remote.aclose)
            mail = GmailService(remote, ctx, catalog_snapshot=snapshot, audit=audit)
        stack.push_async_callback(mail.aclose)
        # Fail Gmail capability checks before starting the browser or paid models.
        tools = await inspect_tools(mail, source="gmail")

        def search_audit(event):
            audit(
                {"event": "search_boundary", "call_id": current_tool_call_id(), **event}
            )

        if data_mode == "fixture":
            search = FixtureSearchService(ctx, policy=search_policy(scenario), audit=search_audit)
        else:
            search = ExaSearchService(
                policy=search_policy(scenario),
                audit=search_audit,
                api_key=None if data_mode == "hybrid" else os.environ.get(config.exa_key_env),
            )
        stack.push_async_callback(search.aclose)
        tools.extend(await inspect_tools(search, source="exa"))
        browser = PublishedBrowserService(
            BrowserBoundary(ctx, **boundary_config),
            spool_dir=spool,
            image=browser_image,
            audit=audit,
        )
        stack.push_async_callback(browser.aclose)
        tools.extend(await inspect_tools(browser, source="playwright"))
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
        yield tools

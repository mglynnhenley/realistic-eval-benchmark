"""Lazy adapters to the other workstreams' entry points (plan.md, cross-module names).

Imported at call time so this package loads before those modules exist, and
so tests can swap them for stand-ins with monkeypatch.

- tools and mail source (workstream A): required, a clear error if missing
- clock (workstream A): required, a clear error if missing
- scorers (workstream D): optional, no scorers and a warning if missing
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from inspect_ai.scorer import Scorer
from inspect_ai.tool import Tool

from refund_eval.scenario.schema import Scenario
from refund_eval.world.state import Email, World

logger = logging.getLogger(__name__)


@asynccontextmanager
async def tool_scope(scenario: Scenario, mail_source_kind: str, *,
                     profile: str = "legacy", published_config: str | None = None,
                     published_data: str = "live", fixture_path: str | None = None,
                     browser_runtime: str = "docker", artifact_dir: str | None = None):
    """Own all tool connections for one trial, including exception cleanup."""
    if published_data == "hybrid" and profile != "published":
        raise ValueError("Hybrid data requires the published tool profile")
    if fixture_path is not None and (profile != "published" or published_data not in {"fixture", "hybrid"}):
        raise ValueError("fixture_path requires the published fixture or hybrid profile")
    if profile == "legacy":
        yield build_tools(scenario, mail_source_kind)
    elif profile == "published":
        from refund_eval.published.profile import published_tools

        async with published_tools(scenario, config_path=published_config,
                                   data_mode=published_data,
                                   **({"browser_runtime": browser_runtime} if browser_runtime != "docker" else {}),
                                   **({"artifact_dir": artifact_dir} if artifact_dir is not None else {}),
                                   **({"fixture_path": fixture_path} if fixture_path is not None else {})) as tools:
            yield tools
    else:
        raise ValueError(f"Unknown tool profile: {profile}")


def _not_built(ex: ImportError, package: str) -> bool:
    """True if the import failed because `package` (or a module in it) isn't there yet,
    rather than because of a bug inside a module that does exist."""
    name = getattr(ex, "name", None) or ""
    return name == package or name.startswith(package + ".")


def build_tools(scenario: Scenario, mail_source_kind: str) -> list[Tool]:
    """`refund_eval.tools.all_tools(scenario, make_mail_source(kind))`."""
    try:
        from refund_eval.tools import all_tools
        from refund_eval.world.mail import make_mail_source
    except ImportError as ex:
        if _not_built(ex, "refund_eval.tools") or _not_built(
            ex, "refund_eval.world.mail"
        ):
            raise RuntimeError(
                "The agent's tools are not available yet: refund_eval.tools.all_tools and "
                "refund_eval.world.mail.make_mail_source (workstream A) are needed to run a trial."
            ) from ex
        raise
    return list(all_tools(scenario, make_mail_source(mail_source_kind)))


def advance_clock(world: World, to: datetime) -> list[Email]:
    """`refund_eval.world.clock.advance(world, to)`: move time forward, deliver due mail."""
    try:
        from refund_eval.world.clock import advance
    except ImportError as ex:
        if _not_built(ex, "refund_eval.world.clock"):
            raise RuntimeError(
                "refund_eval.world.clock.advance (workstream A) is needed to wake the agent."
            ) from ex
        raise
    return list(advance(world, to))


def load_scorers(rubric_set: dict | None = None) -> list[Scorer]:
    """`refund_eval.grading.scorers.all_scorers()`, or no scorers if grading isn't built yet."""
    try:
        from refund_eval.grading.scorers import all_scorers
    except ImportError as ex:
        if _not_built(ex, "refund_eval.grading"):
            logger.warning(
                "refund_eval.grading.scorers.all_scorers is not available yet; "
                "running without scorers (score the logs later with `inspect score`)."
            )
            return []
        raise
    return list(all_scorers() if rubric_set is None else all_scorers(rubric_set=rubric_set))

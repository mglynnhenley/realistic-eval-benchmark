"""Own published tool connections and adapt clock and grading for each trial."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from inspect_ai.scorer import Scorer

from refund_eval.config import RunConfig
from refund_eval.scenario.schema import Scenario
from refund_eval.world.state import Email, World

logger = logging.getLogger(__name__)


@asynccontextmanager
async def tool_scope(scenario: Scenario, mail_source_kind: str, *,
                     profile: str = RunConfig.model_fields["tool_profile"].default, published_config: str | None = None,
                     published_data: str = RunConfig.model_fields["data_mode"].default, fixture_path: str | None = None,
                     browser_runtime: str = "docker", artifact_dir: str | None = None):
    """Own all tool connections for one trial, including exception cleanup."""
    if profile != "published":
        raise ValueError("tool_profile must be published")
    if fixture_path is not None and (profile != "published" or published_data not in {"fixture", "hybrid"}):
        raise ValueError("fixture_path requires the published fixture or hybrid profile")
    if profile == "published":
        from refund_eval.published.profile import published_tools

        async with published_tools(scenario, config_path=published_config,
                                   data_mode=published_data,
                                   **({"browser_runtime": browser_runtime} if browser_runtime != "docker" else {}),
                                   **({"artifact_dir": artifact_dir} if artifact_dir is not None else {}),
                                   **({"fixture_path": fixture_path} if fixture_path is not None else {})) as tools:
            yield tools
    else:
        raise ValueError("tool_profile must be published")


def _not_built(ex: ImportError, package: str) -> bool:
    """True if the import failed because `package` (or a module in it) isn't there yet,
    rather than because of a bug inside a module that does exist."""
    name = getattr(ex, "name", None) or ""
    return name == package or name.startswith(package + ".")


def advance_clock(world: World, to: datetime) -> list[Email]:
    """`refund_eval.world.clock.advance(world, to)`: move time forward, deliver due mail."""
    try:
        from refund_eval.world.clock import advance
    except ImportError as ex:
        if _not_built(ex, "refund_eval.world.clock"):
            raise RuntimeError(
                "refund_eval.world.clock.advance is needed to wake the agent."
            ) from ex
        raise
    return list(advance(world, to))


def load_scorers(rubric_set: dict | None = None, llm: bool = True) -> list[Scorer]:
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
    return list(all_scorers(rubric_set=rubric_set, llm=llm))

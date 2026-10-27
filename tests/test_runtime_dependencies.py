"""The execution paths work when the dashboard package is unavailable."""

import subprocess
import sys
from pathlib import Path


def test_harness_published_tools_and_harbor_do_not_import_dashboard(tmp_path):
    # A fresh interpreter prevents pytest's dashboard fixtures or earlier imports
    # from hiding an accidental dependency. Exercise the lazy imports as well as
    # initial imports; no browser or model service is needed.
    script = r'''
import asyncio
import importlib.abc
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

attempted = []

class NoDashboard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "refund_eval.dashboard" or fullname.startswith("refund_eval.dashboard."):
            attempted.append(fullname)
            raise AssertionError(f"Execution imported the dashboard: {fullname}")

sys.meta_path.insert(0, NoDashboard())

from inspect_ai.model import ChatMessageAssistant
from inspect_ai.scorer import Score
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store
from refund_eval.agent.harness import emit_snapshot, init_world
from refund_eval.harbor import worker
from refund_eval.harbor.config import TrialRequest, TrialSettings
from refund_eval.published import profile
from refund_eval.runtime.snapshots import RUN_DIR_ENV
from refund_eval.scenario.schema import load_scenario

destination = Path(sys.argv[1])
os.environ[RUN_DIR_ENV] = str(destination / "live")
init_subtask_store(Store())
scenario = load_scenario("train_refund_pressure")
init_world(scenario)
messages = [ChatMessageAssistant(content="Waiting for a company reply.")]
emit_snapshot(messages, phase="running")
live_path = destination / "live/snapshot.json"
assert json.loads(live_path.read_text())["messages"][0]["content"] == messages[0].text

class ToolsReached(Exception):
    pass

async def initialize_published_profile():
    with patch.object(profile, "fixture_readiness", return_value={"configured": True}), \
            patch("refund_eval.published.fixtures.make_fixture_mail", side_effect=ToolsReached):
        try:
            async with profile.published_tools(scenario, data_mode="fixture", artifact_dir=str(destination)):
                raise AssertionError("The fake tool initializer should stop startup")
        except ToolsReached:
            pass

asyncio.run(initialize_published_profile())
assert json.loads(live_path.read_text())["phase"] == "checking_tools"

sample = SimpleNamespace(
    store=dict(store().items()), messages=messages, error=None,
    scores={"containment": Score(value=float("nan"))}, model_usage={}, role_usage={},
)
log = SimpleNamespace(samples=[sample], status="success", error=None, location=None)
log.location = str(destination / "trial.eval")
request = TrialRequest(
    instruction=scenario.task_text, model="openrouter/example/model", trial_id="no-dashboard",
    settings=TrialSettings(tool_profile="published", browser_runtime="subprocess"),
)
with patch.object(worker, "eval", return_value=[log]):
    result = worker.run_trial(request, destination / "harbor")
assert result["passed"]
snapshot = json.loads((destination / "harbor/snapshot.json").read_text())
assert snapshot["phase"] == "completed"
assert snapshot["scores"]["containment"]["value"] is None
assert snapshot["world"] == sample.store["world"]
assert snapshot["messages"][0]["content"] == messages[0].text
assert not attempted, attempted
assert not any(name.startswith("refund_eval.dashboard") for name in sys.modules)
'''
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

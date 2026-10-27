"""Dashboard resumes retain the exact policy evidence from the interrupted trial."""

import json

from fixtures.simulator import start_sample
from inspect_ai.model import ChatMessageUser
from inspect_ai.util import store

from refund_eval.agent.harness import restore_snapshot
from refund_eval.runtime.snapshots import RUN_DIR_ENV, emit_snapshot
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.open_world import COMPANIES_KEY, company_record
from refund_eval.world.state import load_world


async def test_dashboard_snapshot_preserves_frozen_late_refund_terms(tmp_path, monkeypatch):
    start_sample("train_refund_late")
    frozen = {"trainline": {"mode": "saved", "dossier": {"facts": [
        {"quote": "The original frozen clause.", "url": "https://www.thetrainline.com/terms"},
    ]}}}
    # Preserve the old serialized key even though the gate implementation has been removed.
    legacy_key = "late_refund_frozen_dossiers"
    store().set(legacy_key, frozen)
    monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path))
    emit_snapshot([ChatMessageUser(content="Please recover my train fare.")], phase="running")
    path = tmp_path / "snapshot.json"
    assert json.loads(path.read_text())["resume_state"][legacy_key] == frozen

    start_sample("train_refund_late")
    async def no_new_research(*args, **kwargs):
        raise AssertionError("Resuming a frozen company must not replace the original terms.")

    monkeypatch.setattr("refund_eval.sim.open_world.researched", no_new_research)
    await restore_snapshot(str(path))
    assert store().get(legacy_key) == frozen

    org = load_scenario("train_refund_late").org("trainline")
    pinned_org, record = await company_record(org, load_world())
    assert pinned_org == org
    assert record == frozen["trainline"]
    assert store().get(COMPANIES_KEY)["trainline"]["research"] == frozen["trainline"]

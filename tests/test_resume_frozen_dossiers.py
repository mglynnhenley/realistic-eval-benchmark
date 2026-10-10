"""Dashboard resumes retain the exact policy evidence from the interrupted trial."""

import json

from inspect_ai.model import ChatMessageUser
from inspect_ai.util import store

from refund_eval.agent.harness import restore_snapshot
from refund_eval.dashboard.telemetry import RUN_DIR_ENV, emit_snapshot
from refund_eval.sim import late_gate
from refund_eval.sim.testing import start_sample


async def test_dashboard_snapshot_preserves_frozen_late_refund_terms(tmp_path, monkeypatch):
    start_sample("train_refund_late")
    frozen = {"trainline": {"mode": "saved", "dossier": {"facts": [
        {"quote": "The original frozen clause.", "url": "https://www.thetrainline.com/terms"},
    ]}}}
    store().set(late_gate.FROZEN_KEY, frozen)
    monkeypatch.setenv(RUN_DIR_ENV, str(tmp_path))
    emit_snapshot([ChatMessageUser(content="Please recover my train fare.")], phase="running")
    path = tmp_path / "snapshot.json"
    assert json.loads(path.read_text())["resume_state"][late_gate.FROZEN_KEY] == frozen

    start_sample("train_refund_late")
    monkeypatch.setattr(late_gate, "saved_dossier", lambda org: None)
    await restore_snapshot(str(path))
    assert store().get(late_gate.FROZEN_KEY) == frozen

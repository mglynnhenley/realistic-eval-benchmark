"""World-side projection is scalars only, independent of any agent progress file."""

import json

from refund_eval.world_service.status import (
    WORLD_FIELDS,
    WORLD_STATUS_FILE,
    world_status,
)
from refund_eval.world_service.store import MemoryStore, OutputWriter


def test_real_output_writer_automatically_projects_without_raw_case_text(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_TRIAL", "test-only-native__env")
    writer = OutputWriter(tmp_path)
    store = MemoryStore(world={"now": "2026-01-01T00:00:00Z", "turn": 1,
                               "overlay_inbox": ["PRIVATE_CASE_CANARY"], "status_reports": ["PRIVATE_CASE_CANARY"]},
                        world_model_budget={"spent_usd": 0.25, "status": "available"},
                        agent_activity=[{"kind": "tool_call", "arguments": "PRIVATE_CASE_CANARY"}])
    writer.write(store, {"private_settings": "PRIVATE_CASE_CANARY"})
    path = tmp_path / WORLD_STATUS_FILE
    first = json.loads(path.read_text())
    assert set(first) == WORLD_FIELDS and first["trial_name"] == "test-only-native"
    assert first["turns"] == 1 and first["world_spend_usd"] == 0.25
    assert first["latest_action"] == "A tool call was recorded."
    assert "PRIVATE_CASE_CANARY" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    store["world"].update(turn=2, killed=True, stop_reason="kill_switch")
    writer.write(store, {})
    second = json.loads(path.read_text())
    assert second["turns"] == 2 and second["stopped"]
    assert second["source_updated_at"] >= first["source_updated_at"]
    store["world_model_budget"]["status"] = "unknown"
    assert world_status(store, "test-only-native")["world_spend_usd"] is None


def test_missing_trial_identity_does_not_create_publisher(tmp_path, monkeypatch):
    monkeypatch.delenv("WORLD_TRIAL", raising=False)
    OutputWriter(tmp_path).write(MemoryStore(), {})
    assert not (tmp_path / WORLD_STATUS_FILE).exists()

"""World-side projection is scalars only, independent of any agent progress file."""

import json

import pytest

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
                        model_completions=[{"response": {"usage": {"cost": 0.25}, "cost_method": "actual",
                                                          "text": "PRIVATE_CASE_CANARY"}}],
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


def test_action_timeline_is_ordered_fixed_vocabulary_and_does_not_change_the_trial(tmp_path, monkeypatch):
    monkeypatch.setenv("WORLD_TRIAL", "test-only-timeline__env")
    activity = [{"kind": "tool_call", "tool": "search_threads", "status": "ok",
                 "arguments": {"query": "PRIVATE"}, "result_text": "PRIVATE", "sim_time": "2026-01-01T00:00:00Z"}]
    store = MemoryStore(agent_activity=activity)
    original = json.dumps(store)
    writer = OutputWriter(tmp_path)
    writer.write(store, {})
    first = json.loads((tmp_path / WORLD_STATUS_FILE).read_text())["steps"]
    assert json.dumps(store) == original
    activity.extend([{"kind": "tool_call", "tool": "PRIVATE", "status": "error", "error": "PRIVATE"},
                     {"kind": "turn", "turn": 1, "stopped": True, "text": "PRIVATE"}])
    writer.write(store, {})
    second = json.loads((tmp_path / WORLD_STATUS_FILE).read_text())["steps"]
    assert second[0] == first[0] and [step["seq"] for step in second] == [1, 2, 3]
    assert [step["action"] for step in second] == ["mail_search", "tool", "turn"]
    assert [step["outcome"] for step in second] == ["returned", "error", "stopped"]
    assert "PRIVATE" not in json.dumps(second)


@pytest.mark.parametrize("method", ["estimate", "worst_case", None])
def test_allowance_estimates_are_not_reported_as_actual_spend(method):
    store = {"world_model_budget": {"spent_usd": 3, "status": "available"},
             "model_completions": [{"response": {"usage": {"cost": 3}, "cost_method": method}}]}
    assert world_status(store, "test-only-estimated")["world_spend_usd"] is None
    store["model_completions"][0]["response"].update(cost_method="generation_lookup", usage={"cost": 1})
    assert world_status(store, "test-only-estimated")["world_spend_usd"] is None  # conservative ledger != actual charge
    store["world_model_budget"]["spent_usd"] = 1
    assert world_status(store, "test-only-estimated")["world_spend_usd"] == 1

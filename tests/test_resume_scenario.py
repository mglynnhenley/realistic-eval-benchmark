"""Saved scenario identity is checked before restoring either resume format."""

import json
from types import SimpleNamespace

import pytest
from inspect_ai.model import ChatMessageUser
from inspect_ai.util import store
from inspect_ai.util._store import Store, init_subtask_store

from refund_eval.agent.harness import init_world, restore_snapshot, restore_trial
from refund_eval.runtime.checkpoint import capture_checkpoint, snapshot_fields
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.state import load_world


@pytest.mark.parametrize("source", ["snapshot", "log"])
@pytest.mark.parametrize("scenario_id", ["train_refund", "bills_price_rise"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_resume_scenario(tmp_path, monkeypatch, source, scenario_id, legacy):
    import inspect_ai.log

    init_subtask_store(Store())
    init_world(load_scenario("train_refund"))
    checkpoint = capture_checkpoint([ChatMessageUser(content="Get me a refund.")])
    assert checkpoint.resume_state["task_grading_notes"]["scenario_id"] == "train_refund"
    if legacy:
        checkpoint.resume_state.pop("task_grading_notes")
    path = tmp_path / ("snapshot.json" if source == "snapshot" else "trial.eval")
    if source == "snapshot":
        path.write_text(json.dumps(snapshot_fields(checkpoint)))
        restore = restore_snapshot
    else:
        sample = SimpleNamespace(
            store={"world": checkpoint.world, "simulated_counterparts": checkpoint.counterparts,
                   **checkpoint.resume_state},
            messages=checkpoint.messages, limit=None,
        )
        monkeypatch.setattr(inspect_ai.log, "read_eval_log", lambda _: SimpleNamespace(samples=[sample]))
        restore = restore_trial
    init_subtask_store(Store({"untouched": True}))
    scenario = load_scenario(scenario_id)
    if not legacy and scenario_id != "train_refund":
        with pytest.raises(ValueError, match="Cannot resume.*train_refund.*bills_price_rise"):
            await restore(str(path), scenario)
        assert dict(store().items()) == {"untouched": True}
    else:
        messages = await restore(str(path), scenario)
        assert messages[0].text == "Get me a refund."
        assert load_world().start_time.isoformat() == checkpoint.world["start_time"]

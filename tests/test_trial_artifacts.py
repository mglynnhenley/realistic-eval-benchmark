"""One loader for saved trials (`runtime.trial_artifacts`): a native Harbor trial directory (the agent's
world-service-mode .eval beside the world's state.json/events.jsonl and the verifier's reward.json)
loads into the same view as the equivalent native run, and the dashboard, storage rows and
`.eval`-only tools read it; native .eval logs and the old Harbor job layout load as before.

Offline: the scripted mockllm trial and fake world models of tests/test_world_service.py.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import urllib.error
import urllib.request

import pytest
from inspect_ai.log import read_eval_log

from refund_eval.dashboard.server import DashboardServer, RunManager
from refund_eval.runtime.checkpoint import checkpoint_from_sample, snapshot_fields
from refund_eval.runtime.snapshots import final_snapshot
from refund_eval.runtime.trial_artifacts import (
    eval_path,
    eval_paths,
    is_harbor_trial,
    read_trial_log,
    trial_sources,
    world_store,
)
from refund_eval.storage.__main__ import eval_logs
from refund_eval.storage.rows import run_row
from refund_eval.world.event_log import EVENTS_KEY
from refund_eval.world.state import WORLD_KEY
from refund_eval.world_service.store import (
    EVENTS_FILE,
    STATE_FILE,
    MemoryStore,
    OutputWriter,
)
from tests.test_world_service import (  # noqa: F401 - `offline` is an autouse fixture
    MAX_TURNS,
    ScriptedAgent,
    _without_wall_time,
    offline,
    run_inspect,
    settings,
    stub_browser,
    world_service_thread,
)

REWARD = {"task_success": 0.0, "containment": 1.0, "event_log_matches_state": 1.0,
          "resource_use.total_actions": 3.0, "resource_use.messages_sent": 1.0}
DETAILS = {"containment": {"status": "contained", "explanation": "Every write was intercepted.",
                           "metadata": {"contained": True}},
           "task_success": {"stop_reason": "max_turns", "success_stop_reason": "refund_reached"}}


def test_world_store_preserves_unicode_separators_in_jsonl_events(tmp_path):
    text = "before\u2028middle\u2029after\u0085end"
    event = {"seq": 1, "kind": "saved_record", "payload": {"text": text}}
    trial = tmp_path / "unicode-events"
    output = trial / "artifacts/world"
    store = MemoryStore({WORLD_KEY: {}, EVENTS_KEY: [event]})
    OutputWriter(output).write(store, settings={})

    event_file = output / EVENTS_FILE
    assert "\u2028" in event_file.read_text(encoding="utf-8")
    assert world_store(trial)[EVENTS_KEY] == [event]


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """The same scripted trial natively and as a native Harbor trial directory under jobs/<job>/<trial>/."""
    from refund_eval.world.state import load_world

    stub_browser(monkeypatch)
    native = run_inspect(tmp_path / "native", script=ScriptedAgent(lambda: load_world().model_dump(mode="json")))
    with world_service_thread(settings(tmp_path, max_turns=MAX_TURNS), monkeypatch) as (url, held):
        run_inspect(tmp_path / "remote", script=ScriptedAgent(lambda: held["trial"].trial.data[WORLD_KEY]),
                    world_url=url)
    [agent_log] = (tmp_path / "remote" / "logs").glob("*.eval")
    jobs = tmp_path / "jobs"
    trial = jobs / "refund-native-train_refund" / "train_refund__abc123"
    for part in ("agent/inspect", "artifacts/agent-eval"):  # the verifier's copy too, as Harbor collects it
        (trial / part).mkdir(parents=True)
        shutil.copyfile(agent_log, trial / part / agent_log.name)
    (trial / "artifacts/world").mkdir(parents=True)
    for name in (STATE_FILE, EVENTS_FILE):
        shutil.copyfile(tmp_path / "world" / name, trial / "artifacts/world" / name)
    (trial / "agent/trajectory.json").write_text(json.dumps({"schema_version": "ATIF-v1.8", "steps": []}))
    (trial / "agent/inspect.log").write_text("inspect finished\n")
    (trial / "verifier").mkdir()
    (trial / "verifier/reward.json").write_text(json.dumps(REWARD))
    (trial / "verifier/details.json").write_text(json.dumps(DETAILS))
    (trial / "result.json").write_text(json.dumps({
        "trial_name": trial.name, "started_at": "2026-11-16T10:00:00Z", "finished_at": "2026-11-16T10:05:00Z",
        "config": {"agent": {"model_name": "mockllm/model"}}, "verifier_result": {"rewards": REWARD}}))
    return {"native": native, "trial": trial, "jobs": jobs}


def test_harbor_trial_loads_into_the_same_view_as_the_native_run(runs):
    native, trial = runs["native"], runs["trial"]
    assert is_harbor_trial(trial)
    [sample] = read_trial_log(trial).samples
    assert [(m.role, m.text) for m in sample.messages] == [(m.role, m.text) for m in native.messages]
    assert sample.metadata["scenario"] == native.metadata["scenario"]
    assert {key for key, value in native.store.items() if value} <= set(sample.store)
    merged, saved = snapshot_fields(checkpoint_from_sample(sample)), snapshot_fields(checkpoint_from_sample(native))
    assert merged["world"]["stop_reason"] == "max_turns"
    assert _without_wall_time(merged["world"]) == _without_wall_time(saved["world"])
    assert _without_wall_time(sample.store[EVENTS_KEY]) == _without_wall_time(native.store[EVENTS_KEY])
    # Rewards become the Inspect scorers' shapes: grouped keys, containment as C/I with its status.
    scores = sample.scores
    assert scores["containment"].value == "C" and scores["containment"].answer == "contained"
    assert scores["containment"].explanation == "Every write was intercepted."
    assert scores["resource_use"].value == {"total_actions": 3.0, "messages_sent": 1.0}
    assert scores["task_success"].value == 0.0 and scores["task_success"].metadata["stop_reason"] == "max_turns"
    # The world's settings as it used them: its seed and the (fake) models that served its roles.
    spec = read_trial_log(trial).eval
    assert spec.model_roles["simulator"].model == "fake/simulator" and spec.model_roles["grader"].model == "fake/grader"
    assert spec.task_args["seed"] == 7 and spec.task_args["max_turns"] == MAX_TURNS
    assert spec.task_args["research_mode"] == "saved" and spec.task_args["published_data"] == "fixture"
    snapshot = final_snapshot(read_trial_log(trial))
    assert snapshot["phase"] == "completed" and snapshot["world"]["turn"] == MAX_TURNS
    assert snapshot["inspect_log"] == str((trial / "agent/inspect").resolve() / next(
        (trial / "agent/inspect").glob("*.eval")).name)

    # The admin snapshot, when collected, is preferred to state.json and events.jsonl.
    store = world_store(trial)
    rpc = {"jsonrpc": "2.0", "id": 1, "result": {"parts": [{"kind": "data", "data": {
        "snapshot_version": 1, "store": {**store, "from_snapshot": True}}}]}}
    (trial / "artifacts/world/final_snapshot.json").write_text(json.dumps(rpc))
    assert world_store(trial) == {**store, "from_snapshot": True}


def test_eval_only_tools_get_one_synthesized_log_per_trial(runs):
    trial, jobs = runs["trial"], runs["jobs"]
    view = read_trial_log(trial).samples[0]
    merged = eval_path(trial)
    assert merged.parent == trial / "merged"
    [sample] = read_eval_log(str(merged)).samples
    assert sample.store == view.store and sample.scores["containment"].value == "C"
    # The agent log, the verifier's copy and the merged log all stand for the one trial.
    assert trial_sources([jobs]) == [trial] and eval_paths([jobs]) == [str(merged)]
    assert eval_logs([jobs]) == [trial]
    from measurements.logs import discover_eval_logs, load_eval_file

    assert discover_eval_logs([jobs]) == (merged,)
    [measured] = load_eval_file(merged)
    assert measured is not None


def test_dashboard_lists_and_opens_a_harbor_trial(runs, tmp_path):
    trial = runs["trial"]
    manager = RunManager(tmp_path / "runs", [runs["jobs"]])
    try:
        [run] = manager.list()
        assert run["harbor_trial"] == str(trial) and run["status"] == "completed"
        assert run["model"] == "mockllm/model" and run["turns"] == MAX_TURNS and run["has_artifact"]
        detail = manager.detail(run["id"])
        snapshot = detail["snapshot"]
        assert snapshot["world"]["stop_reason"] == "max_turns" and snapshot["messages"]
        assert snapshot["scores"]["containment"]["value"] == "C"
        assert snapshot["downloads"] == ["trajectory.json", "reward.json", "details.json"]
        assert detail["console"] == "inspect finished\n" and "long_horizon" in snapshot
        json.dumps(detail, allow_nan=False)
        assert manager.artifact(run["id"]).suffix == ".eval"
        assert manager.artifact(run["id"], "reward.json") == trial / "verifier/reward.json"
        assert manager.stop(run["id"])["status"] == "completed"
        server = DashboardServer(("127.0.0.1", 0), manager)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}/api/runs"
        try:
            with urllib.request.urlopen(base) as response:
                assert [item["id"] for item in json.load(response)["runs"]] == [run["id"]]
            with urllib.request.urlopen(f"{base}/{run['id']}/artifact/reward.json") as response:
                assert json.load(response) == REWARD
            with pytest.raises(urllib.error.HTTPError, match="404"):
                urllib.request.urlopen(f"{base}/{run['id']}/artifact/state.json")
        finally:
            server.shutdown()
            server.server_close()
    finally:
        manager.close()


def test_storage_rows_build_from_a_harbor_trial(runs):
    log = read_trial_log(runs["trial"])
    [sample] = log.samples
    row = run_row(log, sample)
    assert row["stop_reason"] == "max_turns" and row["turns"] == MAX_TURNS
    assert row["world"]["stop_reason"] == "max_turns" and EVENTS_KEY in row["resume_state"]
    assert row["scores"]["containment"]["value"] == "C"
    assert row["message_count"] == len(runs["native"].messages)


def test_native_logs_and_the_old_harbor_layout_still_load(tmp_path, monkeypatch):
    stub_browser(monkeypatch)
    sample = run_inspect(tmp_path / "native")
    [log_path] = (tmp_path / "native" / "logs").glob("*.eval")
    assert read_trial_log(log_path).samples[0].store == read_eval_log(str(log_path)).samples[0].store
    assert read_trial_log(log_path).eval == read_eval_log(str(log_path)).eval
    assert eval_path(log_path) == log_path and eval_paths([str(log_path)]) == [str(log_path)]

    # The old wrapper's trial: worker log and collected refund artifacts, with the whole store in its .eval.
    jobs = tmp_path / "jobs"
    old = jobs / "refund-batch" / "trial__1"
    refund = old / "artifacts/logs/artifacts/refund"
    refund.mkdir(parents=True)
    (old / "agent").mkdir()
    (old / "agent/worker.log").write_text("worker\n")
    shutil.copyfile(log_path, refund / log_path.name)
    assert not is_harbor_trial(old)
    assert trial_sources([jobs]) == [refund / log_path.name] and eval_logs([jobs]) == [refund / log_path.name]
    assert eval_paths([jobs]) == [str(jobs)]  # tools search it as before
    loaded = read_trial_log(old).samples[0]
    assert loaded.store[WORLD_KEY] == sample.store[WORLD_KEY]
    assert read_trial_log(old).eval == read_eval_log(str(log_path)).eval
    assert not (old / "merged").exists()

    # The dashboard ignores it (old Harbor trials reach it through its batch runs).
    manager = RunManager(tmp_path / "runs", [jobs])
    try:
        assert manager.list() == []
    finally:
        manager.close()


def test_storage_upload_reads_a_harbor_trial(runs, monkeypatch):
    from refund_eval.storage import supabase

    sent = {}

    class Response:
        def __init__(self, data=None):
            self.data = data

        def json(self):
            return self.data

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, content=None, json=None, **kwargs):
            if url.startswith("/storage/"):
                sent["object"] = content
            elif url == "/rest/v1/runs":
                sent["run"] = json
                return Response([{"id": "run-1"}])
            else:
                sent.setdefault("messages", []).extend(json)
            return Response()

        async def delete(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(supabase, "_client", Client)
    monkeypatch.setattr(supabase, "_check", lambda response, what: response)
    assert asyncio.run(supabase.upload_log(runs["trial"])) == ["run-1"]
    agent = next((runs["trial"] / "agent/inspect").glob("*.eval"))
    assert sent["object"] == agent.read_bytes()
    assert sent["run"]["world"]["stop_reason"] == "max_turns"
    assert len(sent["messages"]) == len(runs["native"].messages)


TARGET, SIMULATOR, GRADER = "openrouter/target/model", "openrouter/sim/model", "openrouter/grader/model"


def _recorded(n, role, model, cost, tokens=(100, 20)):
    usage = {"input_tokens": tokens[0], "output_tokens": tokens[1], "total_tokens": sum(tokens),
             "input_tokens_cache_read": None, "input_tokens_cache_write": None, "reasoning_tokens": None,
             "cost": cost}
    return {"n": n, "role": role, "model_name": model, "request": "x",
            "response": {"text": "", "message": {"role": "assistant", "content": "", "tool_calls": []},
                         "stop_reason": "stop", "usage": usage, "model": model}}


# Two simulator calls, a grader call and a failed call (no usage), as the world's client records them.
WORLD_CALLS = [_recorded(0, "simulator", SIMULATOR, 0.5), _recorded(1, "simulator", SIMULATOR, 0.25),
               _recorded(2, "grader", GRADER, 0.125),
               {"n": 3, "role": "simulator", "model_name": SIMULATOR, "request": "x",
                "error": {"type": "TimeoutError", "message": "timed out", "limit": False}}]


def _synthetic_log(path, *, model_usage, role_usage=None, store=None, task_args=None, model_roles=None):
    from inspect_ai.log import (
        EvalConfig,
        EvalDataset,
        EvalLog,
        EvalSample,
        EvalSpec,
        write_eval_log,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    write_eval_log(EvalLog(
        status="success",
        eval=EvalSpec(created="2026-11-16T00:00:00Z", task="synthetic", dataset=EvalDataset(), model=TARGET,
                      config=EvalConfig(), task_args=task_args or {}, model_roles=model_roles),
        samples=[EvalSample(id="synthetic", epoch=1, input="synthetic input", target="", store=store or {},
                            metadata={"scenario": "synthetic"},
                            model_usage=model_usage, role_usage=role_usage or {})]), str(path))
    return path


def test_harbor_trial_cost_adds_the_worlds_simulator_and_grader_spend(tmp_path):
    from inspect_ai.model import ModelUsage

    trial = tmp_path / "jobs/job/trial__1"
    _synthetic_log(trial / "agent/inspect/agent.eval",
                   model_usage={TARGET: ModelUsage(input_tokens=1000, output_tokens=50, total_tokens=1050,
                                                   total_cost=2.0)})
    (trial / "artifacts/world").mkdir(parents=True)
    (trial / "artifacts/world/state.json").write_text(json.dumps(
        {"settings": {}, "store": {WORLD_KEY: {"turn": 1}, "model_completions": WORLD_CALLS}}))

    log = read_trial_log(trial)
    [sample] = log.samples
    assert sample.metadata["cost_usd"] == {"agent": 2.0, "world": 0.875}
    assert sample.role_usage["simulator"].total_cost == 0.75 and sample.role_usage["grader"].total_cost == 0.125
    assert sample.role_usage["simulator"].input_tokens == 200 and sample.role_usage["simulator"].total_tokens == 240
    assert {name: usage.total_cost for name, usage in sample.model_usage.items()} == {
        TARGET: 2.0, SIMULATOR: 0.75, GRADER: 0.125}
    assert run_row(log, sample)["total_cost"] == 2.875
    # The synthesized .eval for .eval-only tools carries the same totals.
    [merged] = read_eval_log(str(eval_path(trial))).samples
    assert merged.metadata["cost_usd"] == {"agent": 2.0, "world": 0.875}
    assert sum(usage.total_cost for usage in merged.model_usage.values()) == 2.875


def test_native_log_cost_is_unchanged_by_its_recorded_completions(tmp_path):
    from inspect_ai.model import ModelUsage

    # A native .eval also records the world's completions, but its usage already counts them.
    usage = {TARGET: ModelUsage(total_cost=2.0), SIMULATOR: ModelUsage(total_cost=0.75),
             GRADER: ModelUsage(total_cost=0.125)}
    roles = {"simulator": ModelUsage(total_cost=0.75), "grader": ModelUsage(total_cost=0.125)}
    path = _synthetic_log(tmp_path / "native.eval", model_usage=usage, role_usage=roles,
                          store={WORLD_KEY: {"turn": 1}, "model_completions": WORLD_CALLS})
    log = read_trial_log(path)
    [sample] = log.samples
    assert sample.model_usage == usage and sample.role_usage == roles and "cost_usd" not in sample.metadata
    assert run_row(log, sample)["total_cost"] == 2.875


def test_harbor_trial_reports_the_worlds_settings_not_the_agents_defaults(tmp_path):
    from inspect_ai.model import ModelUsage

    from refund_eval.config import DEFAULT_GRADER_MODEL, DEFAULT_ROLE_MODEL

    # The agent ran in world-service mode: its spec has the default world-side settings.
    agent_args = {"scenario": "train_refund_late", "sim_weeks": 3, "published_data": "hybrid", "max_turns": 1000,
                  "research_mode": "saved", "seed": None, "company_simulator": None, "browser_runtime": "docker",
                  "world_url": "http://world:8000", "history_tokens": 60000, "token_budget": 1000000,
                  "cost_limit": 0.5, "time_limit": 36000}
    trial = tmp_path / "jobs/job/trial__1"
    _synthetic_log(trial / "agent/inspect/agent.eval", model_usage={TARGET: ModelUsage(total_cost=1.0)},
                   task_args=agent_args, model_roles={"simulator": {"model": DEFAULT_ROLE_MODEL},
                                                      "grader": {"model": DEFAULT_GRADER_MODEL}})
    world = {"scenario": "train_refund_late", "seed": None, "sim_weeks": 3.0, "max_turns": 77, "research_mode": "auto",
             "data_mode": "hybrid", "simulator_model": SIMULATOR, "grader_model": GRADER,
             "company_simulator": "concordia", "browser_runtime": "subprocess", "output_dir": "/var/log/world"}
    (trial / "artifacts/world").mkdir(parents=True)
    (trial / "artifacts/world/state.json").write_text(json.dumps(
        {"settings": world, "store": {WORLD_KEY: {"turn": 1, "seed": 1234}}}))

    expected = {**agent_args, "sim_weeks": 3.0, "max_turns": 77, "research_mode": "auto", "seed": 1234,
                "company_simulator": "concordia", "browser_runtime": "subprocess"}
    for spec in (read_trial_log(trial).eval, read_trial_log(trial, header_only=True).eval,
                 read_eval_log(str(eval_path(trial))).eval):
        assert spec.task_args == expected  # target budgets, history window and cost limit stay the agent's
        assert {role: config.model for role, config in spec.model_roles.items()} == {
            "simulator": SIMULATOR, "grader": GRADER}
        assert spec.model == TARGET
        assert set(spec.metadata["settings_source"]["world"]) == {
            "model_roles.simulator", "model_roles.grader", *(f"task_args.{arg}" for arg in (
                "scenario", "seed", "sim_weeks", "max_turns", "research_mode", "published_data",
                "company_simulator", "browser_runtime"))}
    log = read_trial_log(trial)
    row = run_row(log, log.samples[0])
    assert row["task_args"]["max_turns"] == 77 and row["model_roles"]["simulator"]["model"] == SIMULATOR

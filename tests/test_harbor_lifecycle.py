"""Actual adapter with fake transport; no sandboxes or inference."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor.models.agent.context")

from harbor.models.agent.context import AgentContext

from refund_eval.harbor.agent import RefundInspectAgent
from refund_eval.harbor.deadlines import trial_deadlines
from refund_eval.scenario.schema import load_scenario


class Environment:
    def __init__(self, *, code=0, waiting=False, download_delay=0, missing_native=False):
        self.code, self.waiting, self.download_delay = code, waiting, download_delay
        self.missing_native = missing_native
        self.started, self.finished = asyncio.Event(), asyncio.Event()
        self.events = []

    async def upload_file(self, source, target):
        self.request = json.loads(source.read_text())

    async def exec(self, command, **kwargs):
        if "--signal-owned" in command:
            self.events.append("stop" if command.endswith("TERM") else "kill")
            self.finished.set()
        else:
            self.events.append("run")
            self.started.set()
            if self.waiting:
                await self.finished.wait()
            self.events.append("closed")
        return SimpleNamespace(return_code=self.code)

    async def download_dir(self, source, target):
        self.events.append("native")
        await asyncio.sleep(self.download_delay)
        if self.missing_native:
            raise FileNotFoundError(source)
        (target / "result.json").write_text(json.dumps({"status": "success", "passed": True}))
        (target / "usage.json").write_text(json.dumps({"models": {"model": {
            "input_tokens": 1, "output_tokens": 2, "total_cost": None}}}))

    async def download_file(self, source, target):
        self.events.append(source)
        target.write_text("{}")


def agent(path):
    return RefundInspectAgent(logs_dir=path, model_name="openrouter/example/offline", scenario="train_refund",
                              time_limit=1, trial_deadlines=trial_deadlines(1))


async def test_bounded_success_keeps_unknown_cost_and_one_native_destination(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: None)
    context = AgentContext()
    await agent(tmp_path / "agent").run(load_scenario("train_refund").task_text, Environment(), context)
    assert context.n_input_tokens == 1 and context.n_output_tokens == 2 and context.cost_usd is None
    assert len(list(tmp_path.rglob("result.json"))) == 1


async def test_original_nonzero_preserved_despite_missing_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: None)
    env = Environment(code=7, missing_native=True)
    context = AgentContext()
    with pytest.raises(RuntimeError, match="exited 7"):
        await agent(tmp_path / "agent").run(load_scenario("train_refund").task_text, env, context)
    assert "native" in env.events and "/logs/agent/worker.log" in env.events
    assert context.cost_usd is None
    assert json.loads((tmp_path / "agent/collection.json").read_text())["errors"]


async def test_cancel_stops_closes_then_downloads_even_when_cancelled_again(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: None)
    env = Environment(waiting=True, download_delay=.03)
    task = asyncio.create_task(agent(tmp_path / "agent").run(
        load_scenario("train_refund").task_text, env, AgentContext()))
    await env.started.wait()
    task.cancel()
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.events.index("stop") < env.events.index("closed") < env.events.index("native")
    assert (tmp_path / "agent/collection.json").exists()


async def test_collection_hung_download_uses_one_fixed_bound(tmp_path):
    env = Environment(download_delay=1)
    obj = agent(tmp_path / "agent")
    obj.logs_dir.mkdir()
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await obj._collect_bounded(env, None, worker_end=start, agent_end=start + .04,
                                   flush_seconds=.01, collect_seconds=.04, stop=False)
    assert time.monotonic() - start < .2
    await asyncio.sleep(.01)


@pytest.mark.parametrize("all_ready", [True, False])
async def test_aggregate_setup_includes_queue_and_agent_setup(tmp_path, monkeypatch, all_ready):
    from harbor.job import Job

    from refund_eval.harbor import deadlines
    from refund_eval.harbor.__main__ import run_bounded_job

    events = []

    class FakeJob(Job):
        # Keep installed Job.on_agent_started/add_hook; substitute only its queue
        # and work, so an invented registration method cannot pass this control.
        def __init__(self):
            self._trial_queue = SimpleNamespace(add_hook=self.register)

        def register(self, event, callback):
            from harbor.trial.hooks import TrialEvent

            assert event == TrialEvent.AGENT_START
            self.callback = callback

        async def run(self):
            try:
                # Simulate initial queue/build and agent setup without remote work.
                await asyncio.sleep(.005)
                if all_ready:
                    for i in range(3):
                        await self.callback(SimpleNamespace(trial_id=i))
                    events.append("all_ready")
                    await asyncio.sleep(.02)
                    return
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("cancelled")
                await asyncio.sleep(.005)
                events.append("finalized")
                raise

    async def create(config):
        return FakeJob()

    monkeypatch.setattr(Job, "create", create)
    original = deadlines.validate_deadlines

    def short_bounds(value, sample):
        canonical = original(value, sample)
        return {**canonical, "setup_seconds": .02, "command_hard_seconds": .2, "termination_seconds": .05}

    monkeypatch.setattr(deadlines, "validate_deadlines", short_bounds)
    config = {"n_attempts": 3, "n_concurrent_trials": 3, "retry": {"max_retries": 0},
              "agents": [{"import_path": "refund_eval.harbor.agent:RefundInspectAgent",
                          "model_name": "openrouter/example/offline",
                          "kwargs": {"scenario": "train_refund", "time_limit": 1, "trial_deadlines": trial_deadlines(1)}}],
              "tasks": [{"path": str(tmp_path / "task")}], "jobs_dir": str(tmp_path / "jobs")}
    path = tmp_path / "job.json"
    path.write_text(json.dumps(config))
    start = time.monotonic()
    if all_ready:
        await run_bounded_job(path)
        assert events == ["all_ready"]
    else:
        with pytest.raises(TimeoutError, match="Aggregate setup"):
            await run_bounded_job(path)
        assert events == ["cancelled", "finalized"]
    assert time.monotonic() - start < .15


def test_preparation_binds_actual_task_job_and_policy_bytes(tmp_path, monkeypatch):
    import subprocess

    from refund_eval.harbor.__main__ import prepare
    from refund_eval.harbor.config import TrialSettings

    original = subprocess.check_output

    def selected_source(argv, **kwargs):
        if argv[:2] == ["git", "ls-files"]:
            return b"refund_eval/harbor/deadlines.py\0refund_eval/harbor/worker.py\0"
        return original(argv, **kwargs)

    monkeypatch.setattr(subprocess, "check_output", selected_source)
    settings = TrialSettings(scenario="train_refund", time_limit=1800,
                             trial_deadlines=trial_deadlines(1800))
    output = tmp_path / "prepared"
    job = json.loads(prepare(output, settings=settings, model="openrouter/example/offline",
                             trials=3, concurrency=3).read_text())
    import tomllib

    task = tomllib.loads((output / "task/task.toml").read_text())
    assert task["agent"]["timeout_sec"] == 2160
    assert task["environment"]["build_timeout_sec"] == 1800
    assert job["environment"]["kwargs"]["sandbox_timeout_secs"] == 4800
    assert job["agents"][0]["kwargs"]["trial_deadlines"] == trial_deadlines(1800)
    assert job["retry"]["max_retries"] == 0
    with pytest.raises(ValueError, match="concurrent wave"):
        prepare(tmp_path / "waves", settings=settings, model="openrouter/example/offline", trials=3, concurrency=2)


async def test_failed_exec_transport_still_signals_exact_worker_before_download(tmp_path):
    env = Environment()
    obj = agent(tmp_path / "agent")
    obj.logs_dir.mkdir()

    async def broken_exec():
        raise ConnectionError("offline transport failed, remote state unknown")

    execution = asyncio.create_task(broken_exec())
    await asyncio.sleep(0)
    start = time.monotonic()
    await obj._collect_bounded(env, execution, worker_end=start + .02, agent_end=start + .1,
                               flush_seconds=.02, collect_seconds=.04, stop=True)
    assert env.events.index("stop") < env.events.index("kill") < env.events.index("native")
    assert time.monotonic() - start < .15


@pytest.mark.parametrize("opt_in", [False, True])
def test_preparation_cli_prints_supported_route_for_its_policy(tmp_path, monkeypatch, capsys, opt_in):
    import sys

    from refund_eval.harbor import __main__ as cli

    monkeypatch.setattr(cli, "prepare", lambda destination, **kwargs: destination / "job.json")
    argv = ["prepare", "--output", str(tmp_path / "prepared"), "--model", "openrouter/example/offline",
            "--scenario", "train_refund"]
    if opt_in:
        argv += ["--trial-deadline-policy", "bounded-lifecycle-v1"]
    monkeypatch.setattr(sys, "argv", argv)
    cli.main()
    output = capsys.readouterr().out
    if opt_in:
        assert "--run-bounded-job" in output and "reviewed realism measure coordinator" in output
        assert "4020s soft / 4200s hard" in output
        assert "argv alone does not supply" in output
        assert "scripts/harbor harbor run -c" not in output
    else:
        assert f"Run with: scripts/harbor harbor run -c {tmp_path}/prepared/job.json" in output

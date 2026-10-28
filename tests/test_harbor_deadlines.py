"""Offline lifecycle controls: canonical policy, native log closure and unknown usage."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from refund_eval.harbor import worker
from refund_eval.harbor.config import TrialRequest, TrialSettings
from refund_eval.harbor.deadlines import VERSION, bounded, settle, trial_deadlines
from refund_eval.realism import autonomous
from refund_eval.scenario.schema import load_scenario
from refund_eval.world.state import WORLD_KEY


def request():
    return TrialRequest(instruction=load_scenario("train_refund").task_text,
                        model="openrouter/example/offline", trial_id="offline-only",
                        settings=TrialSettings(scenario="train_refund", time_limit=1,
                                               trial_deadlines=trial_deadlines(1)))


def fake_log(path, status="success"):
    sample = SimpleNamespace(store={WORLD_KEY: {"turn": 1, "stop_reason": "max_turns"}},
                             error=None, scores={}, messages=[], model_usage={}, role_usage={})
    return SimpleNamespace(status=status, samples=[sample], error=None, location=str(path / "trial.eval"))


def test_protocol_direct_flag_and_legacy():
    legacy = autonomous._protocol({"time_limit": 1800, "per_trial_cost": 2.25, "token_budget": 3_000_000})
    opted = autonomous._protocol({"time_limit": 1800, "per_trial_cost": 2.25,
                                 "token_budget": 3_000_000, "trial_deadline_policy": VERSION})
    bounds = opted.pop("trial_deadlines")
    assert opted == legacy
    assert bounds["command_hard_seconds"] == 4200
    assert (bounds["setup_seconds"] + bounds["agent_seconds"] + bounds["verify_seconds"]
            + bounds["termination_seconds"]) == bounds["command_hard_seconds"]
    with pytest.raises(ValueError):
        autonomous._protocol({"trial_deadline_policy": "unknown"})
    with pytest.raises(ValueError):
        autonomous._protocol({"trial_deadlines": bounds})


@pytest.mark.parametrize("key,value", [("setup_seconds", 1), ("worker_hard_seconds", 2),
                                       ("sample_seconds", True), ("collect_seconds", 120.0)])
def test_noncanonical_bounds_rejected(key, value):
    bounds = trial_deadlines(1)
    bounds[key] = value
    with pytest.raises(ValueError):
        TrialSettings(time_limit=1, trial_deadlines=bounds)


async def test_repeated_waiter_cancel_does_not_reset_cleanup_deadline():
    started = time.monotonic()
    task = asyncio.create_task(asyncio.sleep(.025, result="closed"))
    notices = []
    waiter = asyncio.create_task(settle(task, started + .2, on_cancel=lambda: notices.append(True)))
    for _ in range(3):
        await asyncio.sleep(.003)
        waiter.cancel()
    assert await waiter == "closed"
    assert len(notices) == 3
    assert time.monotonic() - started < .15


async def test_hung_cleanup_cannot_extend_deadline():
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await bounded(asyncio.Event().wait(), start + .025)
    assert time.monotonic() - start < .15


async def test_success_and_returned_cancelled_log_have_different_markers(tmp_path, monkeypatch):
    async def evaluate(*args, **kwargs):
        return [fake_log(tmp_path, status)]

    monkeypatch.setattr(worker, "eval_async", evaluate)
    for status in ("success", "cancelled"):
        path = tmp_path / status
        if status == "success":
            assert (await worker.run_trial_async(request(), path))["status"] == "success"
            assert (path / "result.json").is_file() and (path / "usage.json").is_file()
        else:
            with pytest.raises(RuntimeError, match="Inspect trial failed"):
                await worker.run_trial_async(request(), path)
            terminal = json.loads((path / "terminal.json").read_text())
            assert terminal["status"] == "cancelled" and terminal["cost_usd"] is None
            assert not terminal["usage_complete"] and not (path / "result.json").exists()
            assert json.loads((path / "usage.partial.json").read_text())["cost_usd"] is None


async def test_worker_original_cancel_survives_late_close_and_repeated_cancel(tmp_path, monkeypatch):
    ready = asyncio.Event()

    async def evaluate(*args, **kwargs):
        ready.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(.025)
            return [fake_log(tmp_path, "cancelled")]

    monkeypatch.setattr(worker, "eval_async", evaluate)
    run = asyncio.create_task(worker.run_trial_async(request(), tmp_path / "artifacts"))
    await ready.wait()
    run.cancel()
    await asyncio.sleep(.005)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert json.loads((tmp_path / "artifacts/terminal.json").read_text())["error_type"] == "CancelledError"
    assert not (tmp_path / "artifacts/result.json").exists()


async def test_real_inspect_cancellation_closes_readable_log_without_generation(tmp_path, monkeypatch):
    """Actual installed Inspect, synthetic sleeping solver, zero model calls."""
    import socket

    from inspect_ai import Task, eval_async
    from inspect_ai.dataset import Sample
    from inspect_ai.log import read_eval_log
    from inspect_ai.solver import solver

    def forbid_network(*args, **kwargs):
        raise AssertionError("Network forbidden in native cancellation witness")

    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    started = asyncio.Event()

    @solver
    def wait_only():
        async def solve(state, generate):
            started.set()
            await asyncio.Event().wait()
            return state
        return solve

    async def evaluate(*args, **kwargs):
        return await eval_async(Task(dataset=[Sample(input="offline")], solver=wait_only()),
                                model="mockllm/model", log_dir=kwargs["log_dir"])

    monkeypatch.setattr(worker, "eval_async", evaluate)
    run = asyncio.create_task(worker.run_trial_async(request(), tmp_path / "native"))
    readiness = asyncio.create_task(started.wait())
    done, _ = await asyncio.wait({readiness, run}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    if run in done:
        await run  # Surface fixture/API errors instead of a misleading readiness timeout.
    assert readiness in done
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, 10)
    [path] = list((tmp_path / "native").glob("*.eval"))
    log = read_eval_log(str(path))
    assert log.status == "cancelled"
    assert not log.stats.model_usage
    terminal = json.loads((tmp_path / "native/terminal.json").read_text())
    assert terminal["cost_usd"] is None and not terminal["success_marker"]


def test_prepared_phase_and_wave_mismatches_fail_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.delenv("REFUND_EVAL_EXA_API_KEY", raising=False)
    protocol = autonomous._protocol({"time_limit": 1, "trial_deadline_policy": VERSION})
    bounds = protocol["trial_deadlines"]
    mapping = {"scenario": "scenario", "token_budget": "token_budget", "max_turns": "max_turns",
               "time_limit": "time_limit", "sim_weeks": "sim_weeks", "cost_limit": "cost_limit",
               "research_mode": "research_mode", "data_mode": "data_mode",
               "simulator_model": "simulator_model", "consistency_model": "grader_model"}
    kwargs = {target: protocol[source] for source, target in mapping.items()}
    kwargs["trial_deadlines"] = bounds
    job = {"n_attempts": 3, "n_concurrent_trials": 3, "retry": {"max_retries": 0},
           "environment": {"type": "modal", "kwargs": {"sandbox_timeout_secs": bounds["sandbox_seconds"]}},
           "agents": [{"model_name": protocol["model"], "kwargs": kwargs}]}
    task = tmp_path / "task"
    source = task / "environment/source/refund_eval"
    source.mkdir(parents=True)
    (source / "_source_manifest.json").write_text('{}')
    toml = (f'[agent]\ntimeout_sec = {bounds["agent_seconds"]}\n'
            '[environment]\nbuild_timeout_sec = 1800\n[verifier]\ntimeout_sec = 60\n')
    (task / "task.toml").write_text(toml)
    (tmp_path / "job.json").write_text(json.dumps(job))
    assert autonomous._prepared_trials(protocol, tmp_path)["job_sha256"]
    job["n_concurrent_trials"] = 2
    (tmp_path / "job.json").write_text(json.dumps(job))
    with pytest.raises(ValueError, match="waves"):
        autonomous._prepared_trials(protocol, tmp_path)
    job["n_concurrent_trials"] = 3
    (tmp_path / "job.json").write_text(json.dumps(job))
    (task / "task.toml").write_text(toml.replace('timeout_sec = 361', 'timeout_sec = 241'))
    with pytest.raises(ValueError, match="bounds"):
        autonomous._prepared_trials(protocol, tmp_path)
    (task / "task.toml").write_text(toml)
    kwargs["trial_deadlines"] = None
    (tmp_path / "job.json").write_text(json.dumps(job))
    with pytest.raises(ValueError, match="policy mismatch"):
        autonomous._prepared_trials(protocol, tmp_path)


def test_owned_worker_identity_rejects_cross_request_and_reused_pid(tmp_path, monkeypatch):
    req = request()
    path = tmp_path / "request.json"
    path.write_text(req.model_dump_json())
    identity = worker._identity(req)
    saved = tmp_path / "identity.json"
    saved.write_text(json.dumps({**identity, "trial_id": "another-attempt"}))
    signals = []
    monkeypatch.setattr(worker.os, "kill", lambda *args: signals.append(args))
    with pytest.raises(ValueError, match="identity"):
        worker.signal_owned_worker(saved, path, 15)
    saved.write_text(json.dumps({**identity, "start_token": "reused"}))
    with pytest.raises(ValueError, match="reused"):
        worker.signal_owned_worker(saved, path, 15)
    assert not signals


@pytest.mark.parametrize("outer", [1800, 4200.0, True])
def test_native_dispatch_rejects_wrong_outer_bound_without_start(tmp_path, outer):
    protocol = autonomous._protocol({"time_limit": 1800, "trial_deadline_policy": VERSION})
    context = SimpleNamespace(settings={"trial_wallclock_limit": outer},
                              command=lambda *a, **k: pytest.fail("No command permitted"))
    with pytest.raises(ValueError, match="hard deadline"):
        autonomous._run_trials(context, protocol, tmp_path, tmp_path, "offline")


async def test_worker_hung_close_stops_at_fixed_hard_bound(tmp_path, monkeypatch):
    from refund_eval.harbor import deadlines

    req = request()
    release = asyncio.Event()
    tasks = []

    async def evaluate(*args, **kwargs):
        tasks.append(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()  # Simulate a native close that never acknowledges.
            return [fake_log(tmp_path, "cancelled")]

    original = deadlines.validate_deadlines

    def short_bounds(value, sample):
        return {**original(value, sample), "worker_hard_seconds": .04, "flush_seconds": .02}

    monkeypatch.setattr(deadlines, "validate_deadlines", short_bounds)
    monkeypatch.setattr(worker, "eval_async", evaluate)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="Worker execution"):
            await worker.run_trial_async(req, tmp_path / "hung")
        assert time.monotonic() - start < .2
        assert json.loads((tmp_path / "hung/terminal.json").read_text())["cost_usd"] is None
        assert not (tmp_path / "hung/result.json").exists()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("left,cap,expected", [(2040.875, None, 2040), (10.0, None, 10),
                                               (1.999, None, 1), (1.0, None, 1),
                                               (20.25, 10, 10), (9.999, 10, 9)])
def test_exec_wire_timeout_floors_current_remaining_without_extending(monkeypatch, left, cap, expected):
    from refund_eval.harbor import deadlines

    clock = [1000.0]
    monkeypatch.setattr(deadlines, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    due = clock[0] + left
    seconds = deadlines.exec_timeout_seconds(due, cap)
    assert type(seconds) is int and seconds == expected and 0 < seconds <= left
    clock[0] += .75
    if due - clock[0] < 1:
        with pytest.raises(TimeoutError):
            deadlines.exec_timeout_seconds(due, cap)
    else:
        assert deadlines.exec_timeout_seconds(due, cap) <= seconds
    assert due == 1000.0 + left


@pytest.mark.parametrize("left", [.999, .5, 0, -1])
def test_subsecond_exec_deadline_rejects_before_coroutine_construction(monkeypatch, left):
    from refund_eval.harbor import deadlines

    monkeypatch.setattr(deadlines, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    constructed = []

    def exec_command(*, timeout_sec):
        constructed.append(timeout_sec)
        return asyncio.sleep(0)

    with pytest.raises(TimeoutError):
        exec_command(timeout_sec=deadlines.exec_timeout_seconds(1000.0 + left, 10))
    assert not constructed


@pytest.mark.parametrize("cap", [True, 0, -1, 10.0])
def test_exec_timeout_rejects_noncanonical_control_cap(cap):
    from refund_eval.harbor.deadlines import exec_timeout_seconds

    with pytest.raises(ValueError):
        exec_timeout_seconds(time.monotonic() + 100, cap)

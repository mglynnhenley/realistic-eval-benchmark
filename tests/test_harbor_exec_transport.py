"""Actual installed Harbor/Modal/protobuf construction, local transport only."""
import asyncio
import json
import re
import socket
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor.models.agent.context")
pytest.importorskip("modal.sandbox")

from harbor.environments.modal import ModalEnvironment
from harbor.models.agent.context import AgentContext
from modal.sandbox import _Sandbox

from refund_eval.harbor.agent import RefundInspectAgent
from refund_eval.harbor.deadlines import trial_deadlines
from refund_eval.scenario.schema import load_scenario


class ConstructedLocally(Exception):
    """Stop before the installed SDK could create a real ContainerProcess."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network forbidden in installed exec control")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: None)


class InstalledEnvironment:
    def __init__(self, *, waiting=False, code=0, missing_native=False):
        self.calls, self.requests, self.downloads = [], [], []
        self.waiting, self.code, self.missing_native = waiting, code, missing_native
        self.started, self.finished = asyncio.Event(), asyncio.Event()
        self.sdk = SimpleNamespace(_merge_env=lambda _: {}, _sandbox_termination_requested=False)
        self.sdk._sandbox = SimpleNamespace(exec=SimpleNamespace(aio=self.sandbox_exec),
                                            poll=SimpleNamespace(aio=self.poll))
        self.sdk._raise_if_unexpected_exit_137 = lambda **kwargs: ModalEnvironment._raise_if_unexpected_exit_137(self.sdk, **kwargs)

    async def poll(self):
        return None  # Local unknown; never infer remote terminal state.

    async def exec_start(self, request):
        self.requests.append(request)
        raise ConstructedLocally("Real installed protobuf constructed; transport blocked")

    async def sandbox_exec(self, *args, **kwargs):
        try:
            await _Sandbox._exec_through_command_router(
                SimpleNamespace(_client=None), *args, task_id="ta-offline-synthetic",
                command_router_client=self, timeout=kwargs["timeout"], workdir=kwargs.get("workdir"))
        except ConstructedLocally:
            command = args[-1]

            async def read():
                return ""

            async def wait():
                if "--signal-owned" in command or "touch /app/.context/KILL" in command:
                    self.finished.set()
                else:
                    self.started.set()
                    if self.waiting:
                        await self.finished.wait()
                return self.code

            return SimpleNamespace(stdout=SimpleNamespace(read=SimpleNamespace(aio=read)),
                                   stderr=SimpleNamespace(read=SimpleNamespace(aio=read)),
                                   wait=SimpleNamespace(aio=wait))
        raise AssertionError("Installed constructor returned past local transport sentinel")

    async def exec(self, command, **kwargs):
        self.calls.append((command, kwargs))
        # Only timeout/command transport is under test; do not resolve secrets.
        return await ModalEnvironment._sdk_exec(self.sdk, command, cwd=kwargs.get("cwd"),
                                               timeout_sec=kwargs["timeout_sec"])

    async def upload_file(self, source, target):
        self.request = json.loads(source.read_text())

    async def download_dir(self, source, target):
        self.downloads.append(source)
        if self.missing_native:
            raise FileNotFoundError(source)
        (target / "result.json").write_text(json.dumps({"status": "success", "passed": True}))
        (target / "usage.json").write_text(json.dumps({"models": {"synthetic": {
            "input_tokens": 1, "output_tokens": 2, "total_cost": None}}}))

    async def download_file(self, source, target):
        self.downloads.append(source)
        if source.endswith("/usage.json"):
            target.write_text(json.dumps({"models": {"synthetic": {
                "input_tokens": 1, "output_tokens": 2, "total_cost": None}}}))
        elif source.endswith("/result.json"):
            target.write_text(json.dumps({"status": "success", "passed": True}))
        else:
            target.write_text("{}")


def agent(path, bounded=True):
    return RefundInspectAgent(logs_dir=path, model_name="openrouter/example/offline", scenario="train_refund",
                              time_limit=1, trial_deadlines=trial_deadlines(1) if bounded else None)


@pytest.mark.parametrize("seconds", [240.75, 10.0, .75, 0.0])
async def test_real_installed_exec_path_rejects_original_float_without_transport(seconds):
    env = InstalledEnvironment()
    with pytest.raises(TypeError, match="float.*integer"):
        await env.exec("true", timeout_sec=seconds)
    assert not env.requests


async def test_real_installed_worker_exec_accepts_int_and_keeps_fractional_duration(tmp_path):
    env = InstalledEnvironment()
    context = AgentContext()
    await agent(tmp_path / "agent").run(load_scenario("train_refund").task_text, env, context)
    [(command, kwargs)] = env.calls
    duration = re.search(r"--worker-seconds (\d+\.\d{6})", command)
    assert duration and type(kwargs["timeout_sec"]) is int
    assert 0 < kwargs["timeout_sec"] <= float(duration.group(1))
    assert [request.timeout_secs for request in env.requests] == [kwargs["timeout_sec"]]
    assert context.cost_usd is None and context.n_input_tokens == 1


async def test_real_installed_nonzero_error_stays_primary_and_usage_unknown(tmp_path):
    env = InstalledEnvironment(code=7, missing_native=True)
    context = AgentContext()
    with pytest.raises(RuntimeError, match="exited 7"):
        await agent(tmp_path / "agent").run(load_scenario("train_refund").task_text, env, context)
    assert "/logs/agent/worker.log" in env.downloads
    assert context.cost_usd is None
    assert json.loads((tmp_path / "agent/collection.json").read_text())["errors"]


async def test_real_installed_cancel_uses_integer_term_and_preserves_cancel(tmp_path):
    env = InstalledEnvironment(waiting=True)
    obj = agent(tmp_path / "agent")
    task = asyncio.create_task(obj.run(load_scenario("train_refund").task_text, env, AgentContext()))
    await env.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(command.endswith("TERM") for command, _ in env.calls)
    assert all(type(kwargs["timeout_sec"]) is int and kwargs["timeout_sec"] > 0 for _, kwargs in env.calls)
    assert env.downloads


async def test_real_installed_kill_switch_touch_is_integer(tmp_path, monkeypatch):
    env = InstalledEnvironment(waiting=True)
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: "offline stop")
    await agent(tmp_path / "agent").run(load_scenario("train_refund").task_text, env, AgentContext())
    [(_command, kwargs)] = [(command, kwargs) for command, kwargs in env.calls if "touch /app/.context/KILL" in command]
    assert type(kwargs["timeout_sec"]) is int and 1 <= kwargs["timeout_sec"] <= 10


async def test_real_installed_term_kill_keep_caps_and_native_error_diagnostics(tmp_path):
    env = InstalledEnvironment()
    obj = agent(tmp_path / "agent")
    obj.logs_dir.mkdir()

    async def broken():
        raise ConnectionError("synthetic transport failed; worker state unknown")

    execution = asyncio.create_task(broken())
    await asyncio.sleep(0)
    start = time.monotonic()
    await obj._collect_bounded(env, execution, worker_end=start + 2.5, agent_end=start + 4,
                               flush_seconds=1.05, collect_seconds=1, stop=True)
    assert [command.split()[-1] for command, _ in env.calls] == ["TERM", "KILL"]
    assert all(type(kwargs["timeout_sec"]) is int and 1 <= kwargs["timeout_sec"] <= 10 for _, kwargs in env.calls)
    assert [request.timeout_secs for request in env.requests] == [kwargs["timeout_sec"] for _, kwargs in env.calls]
    collection = json.loads((obj.logs_dir / "collection.json").read_text())
    assert collection["cost_usd"] is None and collection["errors"]


async def test_subsecond_stop_and_kill_never_construct_exec_but_download_independently(tmp_path):
    env = InstalledEnvironment(missing_native=True)
    obj = agent(tmp_path / "agent")
    obj.logs_dir.mkdir()

    async def broken():
        raise ConnectionError("synthetic original failure")

    execution = asyncio.create_task(broken())
    await asyncio.sleep(0)
    start = time.monotonic()
    await obj._collect_bounded(env, execution, worker_end=start + .02, agent_end=start + .1,
                               flush_seconds=.02, collect_seconds=.04, stop=True)
    assert not env.calls and not env.requests
    assert "/logs/agent/worker.log" in env.downloads and "/logs/agent/progress/snapshot.json" in env.downloads
    collection = json.loads((obj.logs_dir / "collection.json").read_text())
    assert collection["cost_usd"] is None
    assert {row["phase"] for row in collection["errors"]} >= {"stop", "owned_kill", "native"}


async def test_actual_installed_legacy_exec_timeout_is_unchanged(tmp_path):
    env = InstalledEnvironment()
    await agent(tmp_path / "agent", bounded=False).run(load_scenario("train_refund").task_text, env, AgentContext())
    [(command, kwargs)] = env.calls
    assert "--worker-seconds" not in command and kwargs["timeout_sec"] == 601


async def test_worker_deadline_crossing_rejects_before_exec_coroutine(tmp_path, monkeypatch):
    from refund_eval.harbor import deadlines

    original_remaining = deadlines.remaining
    reads = []

    def shrinking(deadline):
        reads.append(deadline)
        return .5 if len(reads) == 3 else original_remaining(deadline)

    monkeypatch.setattr(deadlines, "remaining", shrinking)
    env = InstalledEnvironment()
    context = AgentContext()
    with pytest.raises(TimeoutError, match="one whole second"):
        await agent(tmp_path / "agent").run(load_scenario("train_refund").task_text, env, context)
    assert not env.calls and not env.requests and env.downloads
    assert reads[0] == reads[1] == reads[2]  # Same fixed worker deadline, not a new grace.
    assert context.cost_usd is None


async def test_touch_deadline_crossing_skips_exec_and_preserves_error(tmp_path, monkeypatch):
    from refund_eval.harbor import deadlines

    original_remaining = deadlines.remaining
    reads = []

    def shrinking(deadline):
        reads.append(deadline)
        return .5 if len(reads) == 6 else original_remaining(deadline)

    monkeypatch.setattr(deadlines, "remaining", shrinking)
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: "offline stop")
    env = InstalledEnvironment(waiting=True)
    context = AgentContext()
    with pytest.raises(TimeoutError, match="one whole second"):
        await agent(tmp_path / "agent").run(load_scenario("train_refund").task_text, env, context)
    assert not any("touch /app/.context/KILL" in command for command, _ in env.calls)
    assert any(command.endswith("TERM") for command, _ in env.calls)
    assert reads[0] == reads[5]
    assert context.cost_usd is None and env.downloads

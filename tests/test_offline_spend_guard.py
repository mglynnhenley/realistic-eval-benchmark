from __future__ import annotations

import asyncio
import importlib.metadata
import inspect
import os
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import fields
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.harbor

pytest.importorskip("harbor.models.job.config")
pytest.importorskip("harbor.environments.modal")
pytest.importorskip("modal")

from harbor.environments.modal import ModalEnvironment
from harbor.models.job.config import JobConfig
from modal import App, Sandbox, Workspace
from modal.types import WorkspaceBillingSummary
from modal.workspace import _WorkspaceBillingManager

from scripts.guarded_harbor import LauncherFailure, run_guarded_job, validate_job_config
from scripts.offline_spend_guard import (
    GuardFailure,
    SpendGuard,
    Thresholds,
    install_modal_hooks,
    modal_cycle_probe,
)

START = datetime(2026, 11, 1, tzinfo=UTC)
END = datetime(2026, 12, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def forbid_external_actions(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("forbidden external action in offline test")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket.socket, "sendto", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", blocked)
    monkeypatch.setattr(os, "kill", blocked)
    monkeypatch.setattr(os, "killpg", blocked)
    monkeypatch.setattr(signal, "raise_signal", blocked)
    if hasattr(signal, "pthread_kill"):
        monkeypatch.setattr(signal, "pthread_kill", blocked)
    for name in ("create", "terminate", "exec", "list", "from_id"):
        if hasattr(Sandbox, name):
            monkeypatch.setattr(Sandbox, name, blocked)
    if hasattr(App, "lookup"):
        monkeypatch.setattr(App, "lookup", blocked)
    monkeypatch.setattr(Workspace, "from_context", blocked)


def openrouter(usage=0, *, limit=50, remaining=None):
    if remaining is None:
        remaining = Decimal(str(limit)) - Decimal(str(usage))
    return {
        "data": {
            "usage": usage,
            "limit": limit,
            "limit_remaining": remaining,
        }
    }


def modal(metered_cost=0, *, start=START, end=END):
    return {"metered_cost": metered_cost, "start": start, "end": end}


def make_thresholds(**overrides):
    values = {
        "openrouter_key_ceiling": Decimal(50),
        "openrouter_trial_delta": Decimal(10),
        "modal_cycle_stop": Decimal(27),
        "probe_timeout": 0.03,
        "poll_interval": 0.02,
        "max_sample_age": 0.2,
        "cleanup_timeout": 0.05,
    }
    values.update(overrides)
    return Thresholds(**values)


class ProbeSequence:
    def __init__(self, values, *, events=None, label="probe"):
        self.values = list(values)
        self.events = events
        self.label = label
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.events is not None:
            self.events.append(f"{self.label}_{self.calls}")
        value = self.values[min(self.calls - 1, len(self.values) - 1)]
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value()
            if inspect.isawaitable(value):
                value = await value
        return value


class FakeSandbox:
    def __init__(self, object_id="sb-owned", *, terminate_error=False):
        self.object_id = object_id
        self.terminate_calls = 0

        async def terminate(*, wait=False):
            self.terminate_calls += 1
            if terminate_error:
                raise RuntimeError("fake termination failure")
            return 0

        self.terminate = SimpleNamespace(aio=terminate)


def make_environment(session_id="session-owned"):
    environment = object.__new__(ModalEnvironment)
    environment.environment_name = "offline-environment"
    environment.session_id = session_id
    environment._sandbox = None
    return environment


def install_fake_harbor_methods(monkeypatch, create, execute=None):
    async def fake_exec(
        self,
        command,
        cwd=None,
        env=None,
        timeout_sec=None,
        shell="bash",
        login=False,
    ):
        if execute is not None:
            return await execute(self, command)
        return "executed"

    async def fake_terminate(self):
        return None

    create.__wrapped__ = create
    monkeypatch.setattr(ModalEnvironment, "_create_sandbox", create)
    monkeypatch.setattr(ModalEnvironment, "_sdk_exec", fake_exec)
    monkeypatch.setattr(ModalEnvironment, "_terminate_sandbox", fake_terminate)


def valid_config(tmp_path):
    task_path = tmp_path / "task"
    task_path.mkdir()
    (task_path / "task.toml").write_text("[task]\n", encoding="utf-8")
    return JobConfig.model_validate(
        {
            "job_name": "offline-job",
            "jobs_dir": str(tmp_path / "jobs"),
            "n_attempts": 1,
            "n_concurrent_trials": 1,
            "retry": {"max_retries": 0},
            "environment": {"type": "modal", "delete": True},
            "agents": [
                {
                    "import_path": "refund_eval.harbor.installed_agent:InstalledAgent",
                    "model_name": "openrouter/anthropic/fake-model",
                    "env": {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"},
                }
            ],
            "tasks": [{"path": str(task_path)}],
        }
    )


def stable_guard(*, events=None, clock=None, or_values=None, modal_values=None):
    or_probe = ProbeSequence(or_values or [openrouter()], events=events, label="openrouter")
    modal_probe = ProbeSequence(modal_values or [modal()], events=events, label="modal")
    guard = SpendGuard(
        make_thresholds(),
        openrouter_probe=or_probe,
        modal_probe=modal_probe,
        clock=clock or time.monotonic,
    )
    return guard, or_probe, modal_probe


def test_pinned_harbor_shapes_and_guarded_launcher_ordering(tmp_path, monkeypatch):
    assert importlib.metadata.version("harbor") == "0.26.0"
    assert tuple(inspect.signature(ModalEnvironment._create_sandbox).parameters) == (
        "self",
        "entrypoint",
        "block_network",
        "experimental_options",
    )
    assert tuple(inspect.signature(ModalEnvironment._sdk_exec).parameters) == (
        "self",
        "command",
        "cwd",
        "env",
        "timeout_sec",
        "shell",
        "login",
    )

    events = []
    sandbox = FakeSandbox()

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        events.append("sandbox_create")
        return sandbox

    exec_calls = []

    async def execute(self, command):
        exec_calls.append(command)
        events.append("sdk_exec")
        return "executed"

    install_fake_harbor_methods(monkeypatch, create, execute)
    config = valid_config(tmp_path)
    validate_job_config(config)
    guard, _, _ = stable_guard(events=events)

    class FakeJob:
        async def run(self):
            events.append("job_run")
            environment = make_environment()
            environment._sandbox = await environment._create_sandbox()
            await environment._sdk_exec("offline command")
            await environment._terminate_sandbox()
            environment._sandbox = None
            return "done"

    async def factory(job_config):
        assert job_config is config
        events.append("job_factory")
        return FakeJob()

    result = asyncio.run(
        run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory)
    )

    assert result == "done"
    assert events.index("job_factory") > max(
        events.index("openrouter_2"), events.index("modal_2")
    )
    assert events.index("job_run") > events.index("job_factory")
    assert events.index("sandbox_create") > events.index("modal_2")
    assert exec_calls == ["offline command"]
    assert sandbox.terminate_calls == 1
    assert guard.monitor_task is not None and guard.monitor_task.cancelled()


def test_pinned_modal_billing_shape_and_production_probe_adapter(monkeypatch):
    assert not inspect.iscoroutinefunction(Workspace.from_context)
    assert isinstance(Workspace.billing, property)
    assert inspect.iscoroutinefunction(_WorkspaceBillingManager.summary)
    assert {field.name for field in fields(WorkspaceBillingSummary)} >= {
        "start",
        "end",
        "metered_cost",
    }

    calls = []

    class FakeBilling:
        async def summary(self):
            calls.append("summary")
            return SimpleNamespace(start=START, end=END, metered_cost=Decimal("2.5"))

    workspace = SimpleNamespace(billing=FakeBilling())
    monkeypatch.setattr(Workspace, "from_context", staticmethod(lambda: workspace))

    result = asyncio.run(modal_cycle_probe())
    assert calls == ["summary"]
    assert result.start == START
    assert result.end == END
    assert result.metered_cost == Decimal("2.5")


def test_normal_environment_teardown_cleans_only_its_binding(monkeypatch):
    verifier = FakeSandbox("sb-verifier")
    main = FakeSandbox("sb-main")
    sandboxes = {"verifier": verifier, "main": main}

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandboxes[self.environment_name]

    install_fake_harbor_methods(monkeypatch, create)

    async def scenario():
        guard, _, _ = stable_guard()
        await guard.start()
        with install_modal_hooks(guard):
            verifier_environment = make_environment()
            verifier_environment.environment_name = "verifier"
            main_environment = make_environment()
            main_environment.environment_name = "main"
            verifier_environment._sandbox = await verifier_environment._create_sandbox()
            main_environment._sandbox = await main_environment._create_sandbox()

            await verifier_environment._terminate_sandbox()
            assert verifier.terminate_calls == 1
            assert main.terminate_calls == 0

        await guard.close()

    asyncio.run(scenario())
    assert verifier.terminate_calls == 1
    assert main.terminate_calls == 1


@pytest.mark.parametrize(
    ("or_values", "modal_values"),
    [
        ([RuntimeError("must not expose details")], [modal()]),
        ([openrouter(limit=51)], [modal()]),
        ([openrouter(usage="NaN")], [modal()]),
        ([openrouter(usage=41, remaining=9)], [modal()]),
        ([openrouter()], [modal(27)]),
    ],
)
def test_startup_probe_errors_thresholds_and_malformed_data_dispatch_nothing(
    tmp_path, or_values, modal_values
):
    config = valid_config(tmp_path)
    calls = []
    guard, _, _ = stable_guard(or_values=or_values, modal_values=modal_values)

    async def factory(_):
        calls.append("created")
        return SimpleNamespace(run=lambda: None)

    with pytest.raises(GuardFailure):
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))

    assert calls == []


def test_startup_probe_timeout_and_late_monitor_readiness_dispatch_nothing(tmp_path):
    config = valid_config(tmp_path)

    async def hangs():
        await asyncio.Future()

    guard = SpendGuard(
        make_thresholds(),
        openrouter_probe=ProbeSequence([openrouter()]),
        modal_probe=ProbeSequence([modal(), hangs]),
    )
    calls = []

    async def factory(_):
        calls.append("created")
        return SimpleNamespace(run=lambda: None)

    with pytest.raises(GuardFailure):
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))
    assert calls == []


@pytest.mark.parametrize(
    ("or_values", "modal_values"),
    [
        ([openrouter(), openrouter(), RuntimeError("hidden")], [modal()]),
        ([openrouter()], [modal(), modal(), RuntimeError("hidden")]),
        ([openrouter(), openrouter(), openrouter(usage=10)], [modal()]),
        ([openrouter()], [modal(), modal(), modal(27)]),
        ([openrouter(usage=5), openrouter(usage=5), openrouter(usage=4)], [modal()]),
        ([openrouter()], [modal(5), modal(5), modal(4)]),
        ([openrouter(), openrouter(), openrouter(usage="NaN")], [modal()]),
        (
            [openrouter()],
            [
                modal(),
                modal(),
                modal(start=START.replace(month=10), end=END.replace(month=11)),
            ],
        ),
        ([openrouter()], [modal(), modal(), {"start": START, "end": END}]),
    ],
)
def test_post_ready_probe_failures_and_thresholds_close_exec_and_clean_bound_sandbox(
    tmp_path, monkeypatch, or_values, modal_values
):
    sandbox = FakeSandbox()
    exec_calls = []
    cancellation_seen = []
    rejected_after_trip = []

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    async def execute(self, command):
        exec_calls.append(command)
        return "unexpected"

    install_fake_harbor_methods(monkeypatch, create, execute)
    config = valid_config(tmp_path)
    guard, _, _ = stable_guard(or_values=or_values, modal_values=modal_values)
    environment = make_environment()

    class BlockingJob:
        async def run(self):
            environment._sandbox = await environment._create_sandbox()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cancellation_seen.append(True)
                current = asyncio.current_task()
                current.uncancel()
                with pytest.raises(GuardFailure):
                    await environment._sdk_exec("must-not-dispatch")
                rejected_after_trip.append(True)
                raise

    async def factory(_):
        return BlockingJob()

    with pytest.raises(GuardFailure) as failure:
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))

    assert failure.value.cleanup_uncertain is False
    assert cancellation_seen == [True]
    assert rejected_after_trip == [True]
    assert exec_calls == []
    assert sandbox.terminate_calls == 1


def test_monitor_timeout_cleans_exact_bound_sandbox(tmp_path, monkeypatch):
    sandbox = FakeSandbox()
    entered = []

    async def hangs_after_ready():
        if len(entered) >= 2:
            await asyncio.Future()
        entered.append(True)
        return modal()

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)
    config = valid_config(tmp_path)
    guard = SpendGuard(
        make_thresholds(),
        openrouter_probe=ProbeSequence([openrouter()]),
        modal_probe=hangs_after_ready,
    )
    started = []

    class BlockingJob:
        async def run(self):
            environment = make_environment()
            environment._sandbox = await environment._create_sandbox()
            started.append(True)
            await asyncio.Future()

    async def factory(_):
        return BlockingJob()

    with pytest.raises(GuardFailure):
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))
    assert started == [True]
    assert sandbox.terminate_calls == 1


def test_monitor_failure_cancels_pending_exec_and_cleans_bound_sandbox(tmp_path, monkeypatch):
    sandbox = FakeSandbox()
    exec_entered = asyncio.Event()
    exec_cancelled = []
    later_execs = []

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    async def pending_exec(self, command):
        if command != "pending-command":
            later_execs.append(command)
        exec_entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            exec_cancelled.append(True)
            raise

    install_fake_harbor_methods(monkeypatch, create, pending_exec)
    config = valid_config(tmp_path)
    guard, _, _ = stable_guard(
        or_values=[openrouter(), openrouter(), RuntimeError("probe failed")]
    )

    class PendingExecJob:
        async def run(self):
            environment = make_environment()
            environment._sandbox = await environment._create_sandbox()
            await environment._sdk_exec("pending-command")
            await environment._sdk_exec("must-not-run")

    async def factory(_):
        return PendingExecJob()

    with pytest.raises(GuardFailure):
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))
    assert exec_entered.is_set()
    assert exec_cancelled == [True]
    assert later_execs == []
    assert sandbox.terminate_calls == 1


def test_creation_returning_after_monitor_trip_is_bound_and_cleaned(tmp_path, monkeypatch):
    sandbox = FakeSandbox()
    create_entered = asyncio.Event()
    release_create = asyncio.Event()
    or_probe = ProbeSequence([openrouter(), openrouter(), RuntimeError("probe failed")])
    modal_probe = ProbeSequence([modal()])
    guard = SpendGuard(
        make_thresholds(cleanup_timeout=0.02),
        openrouter_probe=or_probe,
        modal_probe=modal_probe,
    )

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        create_entered.set()
        await release_create.wait()
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)
    config = valid_config(tmp_path)

    class CreatingJob:
        async def run(self):
            environment = make_environment()
            environment._sandbox = await environment._create_sandbox()
            raise AssertionError("cancelled creation must not return to task work")

    async def factory(_):
        return CreatingJob()

    async def scenario():
        launch = asyncio.create_task(
            run_guarded_job(config, make_thresholds(cleanup_timeout=0.02), guard=guard, job_factory=factory)
        )
        await asyncio.wait_for(create_entered.wait(), timeout=0.5)

        async def wait_for_trip():
            while guard.failure_reason is None:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(wait_for_trip(), timeout=0.5)
        await asyncio.sleep(0.05)
        release_create.set()
        with pytest.raises(GuardFailure):
            await launch

    asyncio.run(scenario())
    assert sandbox.terminate_calls == 1
    assert guard.cleanup_uncertain is False


def test_missing_or_mismatched_creation_identity_never_cleans_returned_object(
    monkeypatch,
):
    for identity_mode in ("missing_id", "changed_session"):
        with monkeypatch.context() as patcher:
            sandbox = FakeSandbox(
                object_id=None if identity_mode == "missing_id" else "sb-unknown"
            )

            async def create(
                self,
                *,
                entrypoint=None,
                block_network=None,
                experimental_options=None,
                _identity_mode=identity_mode,
                _sandbox=sandbox,
            ):
                if _identity_mode == "changed_session":
                    self.session_id = "different-session"
                return _sandbox

            install_fake_harbor_methods(patcher, create)

            async def scenario():
                guard, _, _ = stable_guard()
                await guard.start()
                environment = make_environment()
                with install_modal_hooks(guard), pytest.raises(GuardFailure):
                    await environment._create_sandbox()
                with pytest.raises(GuardFailure):
                    await guard.close()

            asyncio.run(scenario())
            assert sandbox.terminate_calls == 0


@pytest.mark.parametrize("identity_damage", ["missing_session", "changed_session", "missing_id"])
def test_identity_change_after_binding_never_cleans_object(monkeypatch, identity_damage):
    sandbox = FakeSandbox()

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)

    async def scenario():
        guard, _, _ = stable_guard()
        await guard.start()
        environment = make_environment()
        with install_modal_hooks(guard):
            environment._sandbox = await environment._create_sandbox()
            if identity_damage == "missing_session":
                environment.session_id = None
            elif identity_damage == "changed_session":
                environment.session_id = "changed-session"
            else:
                sandbox.object_id = None
            await guard.cleanup_environment(environment)

        assert guard.cleanup_uncertain
        with pytest.raises(GuardFailure):
            await guard.close()

    asyncio.run(scenario())
    assert sandbox.terminate_calls == 0


def test_mismatched_current_sandbox_never_touches_foreign_object(monkeypatch):
    owned = FakeSandbox("sb-owned")
    foreign = FakeSandbox("sb-unrelated")

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return owned

    install_fake_harbor_methods(monkeypatch, create)

    async def scenario():
        guard, _, _ = stable_guard()
        await guard.start()
        environment = make_environment()
        with install_modal_hooks(guard):
            environment._sandbox = await environment._create_sandbox()
            environment._sandbox = foreign
            with pytest.raises(GuardFailure):
                await environment._sdk_exec("must-not-dispatch")
        with pytest.raises(GuardFailure):
            await guard.close()

    asyncio.run(scenario())
    assert owned.terminate_calls == 1
    assert foreign.terminate_calls == 0


def test_cleanup_failure_is_nonzero_and_reports_uncertainty(tmp_path, monkeypatch):
    sandbox = FakeSandbox(terminate_error=True)

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)
    config = valid_config(tmp_path)
    guard, _, _ = stable_guard(
        or_values=[openrouter(), openrouter(), RuntimeError("probe failed")]
    )

    class BlockingJob:
        async def run(self):
            environment = make_environment()
            environment._sandbox = await environment._create_sandbox()
            await asyncio.Future()

    async def factory(_):
        return BlockingJob()

    with pytest.raises(GuardFailure) as failure:
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))
    assert failure.value.cleanup_uncertain is True
    assert sandbox.terminate_calls == 1


def test_cleanup_timeout_is_nonzero_and_reports_uncertainty(monkeypatch):
    class SlowSandbox:
        object_id = "sb-slow-cleanup"

        def __init__(self):
            self.terminate_calls = 0

            async def terminate(*, wait=False):
                self.terminate_calls += 1
                await asyncio.Future()

            self.terminate = SimpleNamespace(aio=terminate)

    sandbox = SlowSandbox()

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)

    async def scenario():
        guard, _, _ = stable_guard()
        guard.thresholds = make_thresholds(cleanup_timeout=0.01)
        await guard.start()
        environment = make_environment()
        with install_modal_hooks(guard):
            environment._sandbox = await environment._create_sandbox()
            await guard.cleanup_environment(environment)
        assert guard.cleanup_uncertain
        with pytest.raises(GuardFailure) as failure:
            await guard.close()
        assert failure.value.cleanup_uncertain

    asyncio.run(scenario())
    assert sandbox.terminate_calls == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"openrouter_trial_delta": Decimal(0)},
        {"modal_cycle_stop": Decimal("NaN")},
        {"probe_timeout": float("nan")},
    ],
)
def test_zero_and_nonfinite_threshold_configuration_is_rejected(overrides):
    with pytest.raises(ValueError):
        make_thresholds(**overrides)


def test_initial_probe_failure_latches_start_attempt_and_prevents_rearm():
    openrouter_probe = ProbeSequence([RuntimeError("hidden")])
    guard = SpendGuard(
        make_thresholds(),
        openrouter_probe=openrouter_probe,
        modal_probe=ProbeSequence([modal()]),
    )

    async def scenario():
        with pytest.raises(GuardFailure):
            await guard.start()
        with pytest.raises(GuardFailure):
            await guard.start()

    asyncio.run(scenario())
    assert openrouter_probe.calls == 1
    assert guard.failure_reason is not None


def test_stale_readiness_cannot_dispatch_or_exec(tmp_path, monkeypatch):
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    guard, _, _ = stable_guard(clock=clock)
    original_start = guard.start

    async def start_then_stale():
        await original_start()
        clock.value = 1.0

    guard.start = start_then_stale
    config = valid_config(tmp_path)
    factory_calls = []

    async def factory(_):
        factory_calls.append(True)
        raise AssertionError("stale readiness must block job construction")

    with pytest.raises(GuardFailure):
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))
    assert factory_calls == []


def test_stale_post_ready_sample_closes_exec_and_cleans_bound_sandbox(tmp_path, monkeypatch):
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    sandbox = FakeSandbox()
    exec_calls = []

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    async def execute(self, command):
        exec_calls.append(command)
        return "unexpected"

    install_fake_harbor_methods(monkeypatch, create, execute)
    config = valid_config(tmp_path)
    guard, _, _ = stable_guard(clock=clock)
    environment = make_environment()

    class StaleJob:
        async def run(self):
            environment._sandbox = await environment._create_sandbox()
            clock.value = 1.0
            await environment._sdk_exec("stale-command")

    async def factory(_):
        return StaleJob()

    with pytest.raises(GuardFailure):
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))
    assert exec_calls == []
    assert sandbox.terminate_calls == 1


def test_monitor_cancellation_latches_and_cleans_only_bound_resource(monkeypatch):
    sandbox = FakeSandbox()

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)

    async def scenario():
        guard, _, _ = stable_guard()
        await guard.start()
        environment = make_environment()
        with install_modal_hooks(guard):
            environment._sandbox = await environment._create_sandbox()

            async def wait_forever():
                await asyncio.Future()

            job_task = asyncio.create_task(wait_forever())
            guard.attach_job_task(job_task)
            guard.monitor_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await guard.monitor_task
            with pytest.raises(asyncio.CancelledError):
                await job_task
            assert job_task.cancelled()
            assert guard.failure_reason == "monitor task stopped"
            assert sandbox.terminate_calls == 1
        with pytest.raises(GuardFailure):
            await guard.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "updates",
    [
        {"n_attempts": 2},
        {"n_concurrent_trials": 2},
        {"retry": {"max_retries": 1}},
        {"environment": {"type": "docker", "delete": True}},
        {"environment": {"type": "modal", "delete": False}},
        {"source_jobs": [{"job_name": "old", "action": "regrade"}]},
        {"agents": []},
        {"tasks": []},
    ],
)
def test_launcher_rejects_non_single_fresh_modal_jobs(tmp_path, updates):
    config = valid_config(tmp_path).model_dump(mode="python")
    config.update(updates)
    with pytest.raises((LauncherFailure, ValueError)):
        validate_job_config(JobConfig.model_validate(config))

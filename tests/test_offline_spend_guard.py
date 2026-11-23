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
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytestmark = pytest.mark.harbor

pytest.importorskip("harbor.models.job.config")
pytest.importorskip("harbor.environments.modal")
pytest.importorskip("modal")

from harbor.environments.modal import ModalEnvironment
from harbor.models.job.config import JobConfig
from harbor.models.job.result import JobResult, JobStats
from harbor.models.task.id import LocalTaskId
from harbor.models.trial.config import TrialConfig
from harbor.models.trial.result import AgentInfo, ExceptionInfo, StepResult, TrialResult
from harbor.models.verifier.result import VerifierResult
from modal import App, Sandbox, Workspace

from scripts import guarded_harbor
from scripts.guarded_harbor import LauncherFailure, run_guarded_job, validate_job_config
from scripts.laura_trial import _print_local_job_log, _print_modal_dashboard
from scripts.offline_spend_guard import (
    GuardFailure,
    SpendGuard,
    Thresholds,
    install_modal_hooks,
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


def make_thresholds(**overrides):
    values = {
        "openrouter_key_ceiling": Decimal(50),
        "openrouter_trial_delta": Decimal(10),
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
    environment._sandbox_timeout = 86400
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


def stable_guard(*, events=None, clock=None, or_values=None):
    or_probe = ProbeSequence(or_values or [openrouter()], events=events, label="openrouter")
    guard = SpendGuard(
        make_thresholds(),
        openrouter_probe=or_probe,
        clock=clock or time.monotonic,
    )
    return guard, or_probe


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ("setup_exception", "trial_exception"),
        ("step_exception", "trial_exception"),
        ("no_trial", "unexpected_trial_count"),
        ("unfinished_trial", "incomplete_result"),
        ("missing_reward", "invalid_reward"),
        ("zero_reward", None),
        ("success", None),
    ],
)
def test_guarded_cli_reports_returned_trial_result(tmp_path, monkeypatch, capsys, record, reason):
    config = valid_config(tmp_path)
    config_path = tmp_path / "job.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    exception = ExceptionInfo(
        exception_type="ModuleNotFoundError",
        exception_message="synthetic private diagnostic",
        exception_traceback="synthetic private traceback",
        occurred_at=START,
    )
    trial = TrialResult(
        task_name="offline",
        trial_name="offline-trial",
        trial_uri="offline",
        task_id=LocalTaskId(path=tmp_path / "task"),
        task_checksum="offline",
        config=TrialConfig(task=config.tasks[0]),
        agent_info=AgentInfo(name="offline", version="1"),
        started_at=START,
        finished_at=None if record == "unfinished_trial" else END,
        exception_info=exception if record == "setup_exception" else None,
        step_results=[StepResult(step_name="offline", exception_info=exception)]
        if record == "step_exception" else None,
        verifier_result=None if record in {"setup_exception", "missing_reward"} else VerifierResult(
            rewards={"score": 0 if record == "zero_reward" else 1},
        ),
    )
    trials = [] if record == "no_trial" else [trial]
    result = JobResult(
        id=uuid4(),
        started_at=START,
        finished_at=END,
        n_total_trials=1,
        stats=JobStats.from_trial_results(trials, n_total_trials=1),
        trial_results=trials,
    )

    async def run_job(config, thresholds):
        return result

    monkeypatch.setattr(guarded_harbor, "run_guarded_job", run_job)
    exit_code = guarded_harbor.main([
        "--config", str(config_path), "--openrouter-key-ceiling", "50",
        "--openrouter-trial-delta", "10",
    ])

    stdout, stderr = capsys.readouterr()
    if reason is None:
        assert (exit_code, stdout, stderr) == (0, "guarded_harbor_job=complete\n", "")
    else:
        assert (exit_code, stdout, stderr) == (1, "", f"guarded_harbor_job=failed reason={reason}\n")


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
        return "executed"

    install_fake_harbor_methods(monkeypatch, create, execute)
    config = valid_config(tmp_path)
    validate_job_config(config)
    guard, _ = stable_guard(events=events)

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
    assert events.index("job_factory") > events.index("openrouter_2")
    assert events.index("job_run") > events.index("job_factory")
    assert events.index("sandbox_create") > events.index("openrouter_2")
    assert exec_calls == ["offline command"]
    assert sandbox.terminate_calls == 1
    assert guard.monitor_task is not None and guard.monitor_task.cancelled()


def test_explicit_total_clamps_modal_lifetime_and_sdk_exec_timeout(tmp_path, monkeypatch):
    sandbox = FakeSandbox()
    sandbox_timeouts = []
    exec_timeouts = []

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        sandbox_timeouts.append(self._sandbox_timeout)
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)

    async def capture_exec(
        self, command, cwd=None, env=None, timeout_sec=None, shell="bash", login=False
    ):
        exec_timeouts.append(timeout_sec)
        return "executed"

    monkeypatch.setattr(ModalEnvironment, "_sdk_exec", capture_exec)
    config = valid_config(tmp_path)
    guard, _ = stable_guard()

    class ImmediateJob:
        async def run(self):
            environment = make_environment()
            environment._sandbox = await environment._create_sandbox()
            await environment._sdk_exec("bounded", timeout_sec=36000)
            assert environment._sandbox_timeout == 86400
            await environment._terminate_sandbox()
            environment._sandbox = None
            return "done"

    async def factory(_):
        return ImmediateJob()

    result = asyncio.run(
        run_guarded_job(
            config,
            make_thresholds(),
            guard=guard,
            job_factory=factory,
            total_time_limit_seconds=5,
        )
    )
    assert result == "done"
    assert len(sandbox_timeouts) == len(exec_timeouts) == 1
    assert 1 <= sandbox_timeouts[0] <= 5
    assert 1 <= exec_timeouts[0] <= 4
    assert sandbox.terminate_calls == 1


def test_modal_lifetime_uses_floor_and_retains_a_stricter_existing_timeout():
    now = [100.0]
    guard = SpendGuard(make_thresholds(), openrouter_probe=ProbeSequence([openrouter()]), clock=lambda: now[0])
    guard.start_total_deadline(5)
    assert guard.sandbox_lifetime_timeout(2) == 2
    assert guard.sandbox_lifetime_timeout(86400) == 5
    now[0] = 104.1
    with pytest.raises(GuardFailure, match="no whole second"):
        guard.sandbox_lifetime_timeout(86400)


def test_required_effective_parameter_record_failure_prevents_job_run(tmp_path):
    config = valid_config(tmp_path)
    guard, _ = stable_guard()
    run_calls = []

    class NeverRunJob:
        async def run(self):
            run_calls.append(True)

    async def factory(_):
        return NeverRunJob()

    def fail_record(_job):
        raise OSError("offline record write failure")

    with pytest.raises(GuardFailure):
        asyncio.run(
            run_guarded_job(
                config,
                make_thresholds(),
                guard=guard,
                job_factory=factory,
                before_job_run=fail_record,
            )
        )
    assert run_calls == []


def test_total_deadline_bounds_late_sandbox_reconciliation(tmp_path, monkeypatch):
    create_started = asyncio.Event()
    create_cancelled = []

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        create_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            create_cancelled.append(True)
            raise

    install_fake_harbor_methods(monkeypatch, create)
    config = valid_config(tmp_path)
    thresholds = make_thresholds(cleanup_timeout=1.2)
    guard = SpendGuard(thresholds, openrouter_probe=ProbeSequence([openrouter()]))

    class CreatingJob:
        async def run(self):
            environment = make_environment()
            environment._sandbox = await environment._create_sandbox()

    async def factory(_):
        return CreatingJob()

    started = time.monotonic()
    with pytest.raises(GuardFailure, match="total run deadline exceeded"):
        asyncio.run(
            run_guarded_job(
                config,
                thresholds,
                guard=guard,
                job_factory=factory,
                total_time_limit_seconds=2,
            )
        )
    assert create_started.is_set()
    assert create_cancelled == [True]
    assert guard.cleanup_uncertain is True
    assert time.monotonic() - started <= 2.2


@pytest.mark.parametrize("stage", ["startup", "job_creation", "execution"])
def test_total_deadline_interrupts_startup_creation_and_execution_and_bounds_cleanup(
    tmp_path, monkeypatch, stage
):
    cleanup_timeout = 0.8
    thresholds = make_thresholds(
        probe_timeout=5.0 if stage == "startup" else 0.03,
        poll_interval=0.01,
        max_sample_age=6.0 if stage == "startup" else 0.2,
        cleanup_timeout=cleanup_timeout,
    )
    total = 2 if stage == "execution" else 1
    guard = None
    probe_entered = asyncio.Event()
    if stage == "startup":
        async def blocked_probe():
            probe_entered.set()
            await asyncio.Future()

        guard = SpendGuard(thresholds, openrouter_probe=blocked_probe)
    else:
        guard = SpendGuard(thresholds, openrouter_probe=ProbeSequence([openrouter()]))

    config = valid_config(tmp_path)
    factory_cancelled = []
    job_cancelled = []
    sandbox = FakeSandbox()
    sandbox_timeouts = []
    exec_timeouts = []
    if stage == "execution":
        async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
            sandbox_timeouts.append(self._sandbox_timeout)
            return sandbox

        install_fake_harbor_methods(monkeypatch, create)

        async def capture_exec(
            self, command, cwd=None, env=None, timeout_sec=None, shell="bash", login=False
        ):
            exec_timeouts.append(timeout_sec)
            return "executed"

        monkeypatch.setattr(ModalEnvironment, "_sdk_exec", capture_exec)

    class BlockingJob:
        async def run(self):
            if stage == "execution":
                environment = make_environment()
                environment._sandbox = await environment._create_sandbox()
                await environment._sdk_exec("bounded", timeout_sec=36000)
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    job_cancelled.append(True)
                    raise
            raise AssertionError("unreachable")

    async def factory(_):
        if stage == "job_creation":
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                factory_cancelled.append(True)
                raise
        if stage == "execution":
            return BlockingJob()
        raise AssertionError("startup deadline must prevent job creation")

    started = time.monotonic()
    with pytest.raises(GuardFailure, match="total run deadline exceeded"):
        asyncio.run(
            run_guarded_job(
                config,
                thresholds,
                guard=guard,
                job_factory=factory,
                total_time_limit_seconds=total,
            )
        )
    elapsed = time.monotonic() - started
    assert elapsed <= total + 0.2
    if stage == "startup":
        assert probe_entered.is_set()
        assert factory_cancelled == []
    elif stage == "job_creation":
        assert factory_cancelled == [True]
    else:
        assert job_cancelled == [True]
        assert sandbox.terminate_calls == 1
        assert 1 <= sandbox_timeouts[0] <= total
        assert 1 <= exec_timeouts[0] < 36000


def test_progress_links_are_emitted_after_guarded_job_and_sandbox_creation(
    tmp_path, monkeypatch, capsys
):
    events = []
    sandbox = FakeSandbox()

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        events.append("sandbox_create")
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)
    config = valid_config(tmp_path)
    guard, _ = stable_guard(events=events)
    log_path = tmp_path / "jobs" / "offline-job" / "job.log"

    class FakeJob:
        _log_output_path = log_path

        async def run(self):
            events.append("job_run")
            environment = make_environment()
            environment._app = SimpleNamespace(
                get_dashboard_url=lambda: "https://modal.com/apps/ap-offline-test"
            )
            environment._sandbox = await environment._create_sandbox()
            await environment._sdk_exec("offline command")
            events.append("sdk_exec")
            await environment._terminate_sandbox()
            return "done"

    async def factory(job_config):
        assert job_config is config
        events.append("job_factory")
        return FakeJob()

    def report_job(job):
        events.append("local_log")
        _print_local_job_log(job)

    def report_sandbox(environment):
        events.append("modal_dashboard")
        _print_modal_dashboard(environment)

    result = asyncio.run(
        run_guarded_job(
            config,
            make_thresholds(),
            guard=guard,
            job_factory=factory,
            on_job_created=report_job,
            on_sandbox_created=report_sandbox,
        )
    )

    assert result == "done"
    assert events.index("local_log") > events.index("job_factory")
    assert events.index("local_log") < events.index("job_run")
    assert events.index("sandbox_create") > events.index("openrouter_2")
    assert events.index("modal_dashboard") > events.index("sandbox_create")
    assert events.index("modal_dashboard") < events.index("sdk_exec")
    assert capsys.readouterr().out.splitlines() == [
        f"harbor_job_log_uri={log_path.resolve().as_uri()}",
        "modal_dashboard_url=https://modal.com/apps/ap-offline-test",
    ]


def test_normal_environment_teardown_cleans_only_its_binding(monkeypatch):
    verifier = FakeSandbox("sb-verifier")
    main = FakeSandbox("sb-main")
    sandboxes = {"verifier": verifier, "main": main}

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandboxes[self.environment_name]

    install_fake_harbor_methods(monkeypatch, create)

    async def scenario():
        guard, _ = stable_guard()
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
    "or_values",
    [
        [RuntimeError("must not expose details")],
        [openrouter(limit=51)],
        [openrouter(usage="NaN")],
        [openrouter(usage=41, remaining=9)],
        [None],
    ],
)
def test_startup_probe_errors_thresholds_and_malformed_data_dispatch_nothing(tmp_path, or_values):
    config = valid_config(tmp_path)
    calls = []
    guard, _ = stable_guard(or_values=or_values)

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
        openrouter_probe=ProbeSequence([openrouter(), hangs]),
    )
    calls = []

    async def factory(_):
        calls.append("created")
        return SimpleNamespace(run=lambda: None)

    with pytest.raises(GuardFailure):
        asyncio.run(run_guarded_job(config, make_thresholds(), guard=guard, job_factory=factory))
    assert calls == []


@pytest.mark.parametrize(
    "or_values",
    [
        [openrouter(), openrouter(), RuntimeError("hidden")],
        [openrouter(), openrouter(), openrouter(usage=10)],
        [openrouter(usage=5), openrouter(usage=5), openrouter(usage=4)],
        [openrouter(), openrouter(), openrouter(usage="NaN")],
    ],
)
def test_post_ready_probe_failures_and_thresholds_close_exec_and_clean_bound_sandbox(
    tmp_path, monkeypatch, or_values
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
    guard, _ = stable_guard(or_values=or_values)
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


def test_custom_environment_import_path_is_rejected_before_probes_or_job_factory(
    tmp_path,
):
    config = valid_config(tmp_path)
    config.environment.import_path = "synthetic.backend:Environment"
    guard, openrouter_probe = stable_guard()
    job_factory_calls = []

    async def job_factory(_):
        job_factory_calls.append(True)

    with pytest.raises(LauncherFailure, match="custom environment import paths"):
        asyncio.run(
            run_guarded_job(
                config,
                make_thresholds(),
                guard=guard,
                job_factory=job_factory,
            )
        )

    assert openrouter_probe.calls == 0
    assert job_factory_calls == []
    assert guard._start_attempted is False


@pytest.mark.parametrize(
    "location,task_config",
    [
        ("job_verifier", None),
        ("task_environment", "[environment.env]\n"),
        ("task_verifier", "[verifier.env]\n"),
        ("separate_verifier_environment", "[verifier.environment.env]\n"),
    ],
)
def test_alternate_task_or_verifier_key_is_rejected_before_probes_or_job_factory(
    tmp_path, location, task_config
):
    config = valid_config(tmp_path)
    if location == "job_verifier":
        config.verifier.env["OPENROUTER_API_KEY"] = "synthetic-alternate-key"
    else:
        task_path = config.tasks[0].path
        with (task_path / "task.toml").open("a", encoding="utf-8") as task_file:
            task_file.write(
                task_config
                + 'OPENROUTER_API_KEY = "synthetic-alternate-key"\n'
            )

    guard, openrouter_probe = stable_guard()
    job_factory_calls = []

    async def job_factory(_):
        job_factory_calls.append(True)

    with pytest.raises(LauncherFailure, match="overrides the monitored OpenRouter key"):
        asyncio.run(
            run_guarded_job(
                config,
                make_thresholds(),
                guard=guard,
                job_factory=job_factory,
            )
        )

    assert openrouter_probe.calls == 0
    assert job_factory_calls == []
    assert guard._start_attempted is False


def test_generated_host_key_placeholder_is_accepted(tmp_path):
    config = valid_config(tmp_path)

    assert config.agents[0].env["OPENROUTER_API_KEY"] == "${OPENROUTER_API_KEY}"
    validate_job_config(config)


def test_monitor_timeout_cleans_exact_bound_sandbox(tmp_path, monkeypatch):
    sandbox = FakeSandbox()
    entered = []

    async def hangs_after_ready():
        if len(entered) >= 2:
            await asyncio.Future()
        entered.append(True)
        return openrouter()

    async def create(self, *, entrypoint=None, block_network=None, experimental_options=None):
        return sandbox

    install_fake_harbor_methods(monkeypatch, create)
    config = valid_config(tmp_path)
    guard = SpendGuard(
        make_thresholds(),
        openrouter_probe=hangs_after_ready,
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
    guard, _ = stable_guard(
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
    guard = SpendGuard(
        make_thresholds(cleanup_timeout=0.02),
        openrouter_probe=or_probe,
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
                guard, _ = stable_guard()
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
        guard, _ = stable_guard()
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
        guard, _ = stable_guard()
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
    guard, _ = stable_guard(
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
        guard, _ = stable_guard()
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
        {"openrouter_trial_delta": Decimal("NaN")},
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
    guard, _ = stable_guard(clock=clock)
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
    guard, _ = stable_guard(clock=clock)
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
        guard, _ = stable_guard()
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

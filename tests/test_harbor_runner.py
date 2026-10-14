"""Run with scripts/harbor python -m pytest tests/test_harbor_runner.py."""

import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.harbor

pytest.importorskip("harbor.models.job.config")

from harbor.models.agent.context import AgentContext
from harbor.models.job.config import JobConfig
from harbor.models.task.task import Task

from refund_eval.config import DEFAULT_GRADER_MODEL, DEFAULT_ROLE_MODEL
from refund_eval.harbor.__main__ import main, prepare
from refund_eval.harbor.agent import RefundInspectAgent
from refund_eval.harbor.config import TrialSettings
from refund_eval.scenario.schema import load_scenario


def published_settings(**kwargs):
    return TrialSettings(tool_profile="published", browser_runtime="subprocess", **kwargs)


def test_cli_rejects_unknown_tool_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["refund_eval.harbor", "--output", str(tmp_path / "job"),
                                      "--model", "openrouter/example/model", "--tool-profile", "unknown"])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2
    assert "invalid choice: 'unknown'" in capsys.readouterr().err
    assert not (tmp_path / "job").exists()


def test_prepare_freezes_source_without_secrets_and_validates_harbor_schema(tmp_path):
    output = tmp_path / "frozen"
    config = prepare(output, settings=published_settings(), model="openrouter/example/model")
    job = JobConfig.model_validate_json(config.read_text())
    task = Task(job.tasks[0].path)
    assert job.n_attempts == 4 and job.n_concurrent_trials == 2
    assert job.environment.type == "modal" and job.environment.delete
    assert job.retry.max_retries == 0
    assert task.config.environment.network_mode == "public"
    assert task.instruction.strip() == load_scenario("train_refund_pressure").task_text.strip()
    assert job.agents[0].env["OPENROUTER_API_KEY"] == "${OPENROUTER_API_KEY}"
    source = task.paths.environment_dir / "source"
    assert (source / "refund_eval/agent/harness.py").is_file()
    assert (source / "README.md").is_file()
    assert (source / "LICENSE").is_file()
    dockerfile = (task.paths.environment_dir / "Dockerfile").read_text()
    assert "npm ci" in dockerfile and "FROM python:" in dockerfile
    assert "install --with-deps chromium" in dockerfile
    assert "node:24.16.0" in dockerfile
    assert task.config.environment.cpus == 2
    assert task.config.environment.memory_mb == 4096
    assert (source / "refund_eval/scenario/inbox.json").is_file()
    assert not any(p.name in {".env", ".secrets", "node_modules", ".venv", ".context"}
                   for p in source.rglob("*"))
    with pytest.raises(FileExistsError):
        prepare(output, settings=published_settings(), model="openrouter/example/model")


def test_prepare_rejects_unignored_output_before_creating_job(tmp_path):
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    with pytest.raises(ValueError, match="must be Git-ignored"):
        prepare(tmp_path / "public", settings=published_settings(), model="openrouter/example/model")
    assert not (tmp_path / "public").exists()


def test_cli_history_window_reaches_the_frozen_agent_settings(tmp_path, monkeypatch, capsys):
    assert published_settings().history_tokens == 60_000
    monkeypatch.setattr(sys, "argv", ["refund_eval.harbor", "--output", str(tmp_path / "job"), "--environment", "docker",
                                      "--model", "openrouter/example/model", "--history-tokens", "0",
                                      "--scenario", "train_refund"])
    main()
    job = JobConfig.model_validate_json((tmp_path / "job/job.json").read_text())
    assert job.agents[0].kwargs["history_tokens"] == 0
    assert job.agents[0].kwargs["scenario"] == "train_refund"
    assert "daily_allowance" not in job.agents[0].kwargs
    assert Task(job.tasks[0].path).instruction.strip() == load_scenario("train_refund").task_text.strip()
    with pytest.raises(ValueError):
        published_settings(history_tokens=-1)


def test_paid_config_uses_named_secrets_without_values(tmp_path):
    config = prepare(
        tmp_path / "paid", settings=published_settings(), model="openrouter/example/model",
        secrets=["refund-model-keys"],
    )
    job = json.loads(config.read_text())
    assert job["environment"]["kwargs"]["secrets"] == ["refund-model-keys"]
    assert job["agents"][0].get("env", {}) == {}


def test_opt_in_exa_key_is_forwarded_by_reference_only_for_hybrid(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_EXA_API_KEY", "secret-exa-value")
    hybrid = json.loads(prepare(tmp_path / "hybrid", settings=published_settings(data_mode="hybrid"),
                                model="openrouter/example/model").read_text())
    assert hybrid["agents"][0]["env"]["REFUND_EVAL_EXA_API_KEY"] == "${REFUND_EVAL_EXA_API_KEY}"
    assert "secret-exa-value" not in json.dumps(hybrid)
    fixture = json.loads(prepare(tmp_path / "fixture", settings=published_settings(data_mode="fixture"),
                                 model="openrouter/example/model").read_text())
    assert "REFUND_EVAL_EXA_API_KEY" not in fixture["agents"][0]["env"]


class FakeEnvironment:
    def __init__(self, *, wait=False, return_code=0):
        self.wait = wait
        self.return_code = return_code
        self.commands = []
        self.cancelled = False

    async def exec(self, command, **kwargs):
        self.commands.append(command)
        if "harbor.worker" in command:
            try:
                if self.wait:
                    await asyncio.Event().wait()
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return SimpleNamespace(return_code=self.return_code, stderr="")

    async def upload_file(self, source, target):
        self.request = json.loads(source.read_text())

    async def download_file(self, source, target):
        if source.endswith("usage.json"):
            data = {"models": {"mockllm/model": {
                "input_tokens": 12, "output_tokens": 5, "total_cost": None,
            }}}
        else:
            data = {"passed": True}
        target.write_text(json.dumps(data))


async def test_agent_propagates_host_kill_and_collects_usage(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: "test stop")
    agent = RefundInspectAgent(logs_dir=tmp_path, model_name="openrouter/example/model", tool_profile="published", browser_runtime="subprocess")
    environment = FakeEnvironment()
    context = AgentContext()
    await agent.run(load_scenario("train_refund_pressure").task_text, environment, context)
    assert any("touch /app/.context/KILL" in c for c in environment.commands)
    assert environment.request["settings"]["scenario"] == "train_refund_pressure"
    assert context.n_input_tokens == 12 and context.n_output_tokens == 5
    assert context.cost_usd is None  # unknown usage is not silently reported as zero


async def test_cancellation_releases_pending_exec(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: None)
    agent = RefundInspectAgent(logs_dir=tmp_path, model_name="openrouter/example/model", tool_profile="published", browser_runtime="subprocess")
    environment = FakeEnvironment(wait=True)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            agent.run(load_scenario("train_refund_pressure").task_text, environment, AgentContext()),
            timeout=0.03,
        )
    assert environment.cancelled


async def test_worker_failure_is_reported_to_harbor(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: None)
    agent = RefundInspectAgent(logs_dir=tmp_path, model_name="openrouter/example/model", tool_profile="published", browser_runtime="subprocess")
    with pytest.raises(RuntimeError, match="exited 7"):
        await agent.run(
            load_scenario("train_refund_pressure").task_text, FakeEnvironment(return_code=7), AgentContext(),
        )


async def test_multi_model_job_interleaves_models_with_one_concurrency_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-key")
    from harbor.job import Job

    models = ['openrouter/provider/first', 'openrouter/provider/second']
    path = prepare(tmp_path / 'multi', settings=published_settings(
        simulator_model='openrouter/provider/simulator', grader_model='openrouter/provider/grader'),
        models=models, trials=3, concurrency=2)
    config = JobConfig.model_validate_json(path.read_text())
    assert [agent.model_name for agent in config.agents] == models
    assert config.n_concurrent_trials == 2
    assert all(agent.kwargs['grader_model'] == 'openrouter/provider/grader' for agent in config.agents)
    job = await Job.create(config)
    try:
        assert len(job) == 6
        assert [trial.agent.model_name for trial in job._trial_configs] == models * 3
    finally:
        job._close_logger_handlers()


@pytest.mark.parametrize('models', [[], ['openrouter/provider/model'] * 2])
def test_invalid_model_selection_does_not_create_job(tmp_path, models):
    with pytest.raises(ValueError, match='duplicates'):
        prepare(tmp_path / 'multi', settings=published_settings(), models=models)
    assert not (tmp_path / 'multi').exists()


def test_prepare_packages_a_gmail_snapshot_as_data_only(tmp_path):
    import hashlib
    from pathlib import Path

    from refund_eval.harbor.config import SNAPSHOT_IN_IMAGE

    inbox = tmp_path / "mine.json"
    inbox.write_bytes((Path(__file__).resolve().parents[1] / "refund_eval/scenario/inbox.json").read_bytes())
    sha = hashlib.sha256(inbox.read_bytes()).hexdigest()
    settings = published_settings(data_mode="hybrid", inbox_sha256=sha)
    config = prepare(tmp_path / "job", settings=settings, model="openrouter/example/model", inbox=inbox)
    job = JobConfig.model_validate_json(config.read_text())
    source = Task(job.tasks[0].path).paths.environment_dir / "source"
    assert (source / SNAPSHOT_IN_IMAGE).read_bytes() == inbox.read_bytes()
    assert job.agents[0].kwargs["inbox_sha256"] == sha
    assert not any(p.name in {".env", ".secrets"} for p in source.rglob("*"))
    with pytest.raises(ValueError, match="both"):
        prepare(tmp_path / "missing", settings=settings, model="openrouter/example/model")
    inbox.write_text("{}")
    with pytest.raises(ValueError, match="changed"):
        prepare(tmp_path / "changed", settings=settings, model="openrouter/example/model", inbox=inbox)


def test_cli_freezes_default_role_models(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["refund_eval.harbor", "--output", str(tmp_path / "paid"),
                                      "--model", "openrouter/provider/target"])
    main()
    [agent] = JobConfig.model_validate_json((tmp_path / "paid" / "job.json").read_text()).agents
    assert agent.model_name == "openrouter/provider/target"
    assert agent.kwargs["simulator_model"] == DEFAULT_ROLE_MODEL
    assert agent.kwargs["grader_model"] == DEFAULT_GRADER_MODEL
    assert agent.kwargs["scenario"] == "train_refund_pressure"
    assert agent.kwargs["sim_weeks"] == 6
    assert agent.kwargs["max_turns"] >= 100


def test_late_cli_freezes_each_models_own_cap_and_common_token_budget(tmp_path, monkeypatch):
    caps = {"openrouter/provider/first": 25, "openrouter/provider/second": 70}
    monkeypatch.setattr(sys, "argv", ["refund_eval.harbor", "--output", str(tmp_path / "late"),
        "--model", "openrouter/provider/first", "--model", "openrouter/provider/second",
        "--scenario", "train_refund_late", "--cost-limit", json.dumps(caps),
        "--token-budget", "300000", "--trials", "1"])
    main()
    job = JobConfig.model_validate_json((tmp_path / "late/job.json").read_text())
    assert {agent.model_name: agent.kwargs["cost_limit"] for agent in job.agents} == caps
    assert all(agent.kwargs["token_budget"] == 300_000 for agent in job.agents)
    assert all(agent.kwargs["research_mode"] == "saved" for agent in job.agents)


def test_late_batch_rejects_scalar_before_creating_output(tmp_path):
    with pytest.raises(ValueError, match="mapping"):
        prepare(tmp_path / "invalid", settings=published_settings(scenario="train_refund_late", cost_limit=5),
                models=["openrouter/provider/first", "openrouter/provider/second"])
    assert not (tmp_path / "invalid").exists()

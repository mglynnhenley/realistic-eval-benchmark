"""Run with scripts/harbor python -m pytest tests/test_harbor_runner.py."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.harbor

pytest.importorskip("harbor.models.job.config")

from harbor.models.agent.context import AgentContext
from harbor.models.job.config import JobConfig
from harbor.models.task.task import Task

from refund_eval.harbor.__main__ import prepare
from refund_eval.harbor.agent import RefundInspectAgent
from refund_eval.harbor.config import TrialSettings
from refund_eval.scenario.schema import load_scenario


def test_prepare_freezes_source_without_secrets_and_validates_harbor_schema(tmp_path):
    output = tmp_path / "frozen"
    config = prepare(output, settings=TrialSettings(smoke=True), model="mockllm/model")
    job = JobConfig.model_validate_json(config.read_text())
    task = Task(job.tasks[0].path)
    assert job.n_attempts == 4 and job.n_concurrent_trials == 2
    assert job.environment.type == "modal" and job.environment.delete
    assert job.retry.max_retries == 0
    assert task.config.environment.network_mode == "no-network"
    assert task.instruction.strip() == load_scenario("train_refund").task_text.strip()
    assert not job.agents[0].env
    source = task.paths.environment_dir / "source"
    assert (source / "refund_eval/agent/harness.py").is_file()
    dockerfile = (task.paths.environment_dir / "Dockerfile").read_text()
    assert "npm ci" in dockerfile and "FROM python:" in dockerfile
    assert "install --with-deps chromium" in dockerfile
    assert "node:24.16.0" in dockerfile
    assert task.config.environment.cpus == 2
    assert task.config.environment.memory_mb == 4096
    assert (source / "tests/fixtures/inbox.json").is_file()
    assert not any(p.name in {".env", ".secrets", "node_modules", ".venv", ".context"}
                   for p in source.rglob("*"))
    with pytest.raises(FileExistsError):
        prepare(output, settings=TrialSettings(smoke=True), model="mockllm/model")


def test_paid_config_uses_named_secrets_without_values(tmp_path):
    config = prepare(
        tmp_path / "paid", settings=TrialSettings(), model="openrouter/example/model",
        secrets=["refund-model-keys"],
    )
    job = json.loads(config.read_text())
    assert job["environment"]["kwargs"]["secrets"] == ["refund-model-keys"]
    assert job["agents"][0].get("env", {}) == {}


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
    agent = RefundInspectAgent(logs_dir=tmp_path, model_name="mockllm/model", smoke=True)
    environment = FakeEnvironment()
    context = AgentContext()
    await agent.run(load_scenario("train_refund").task_text, environment, context)
    assert any("touch /app/.context/KILL" in c for c in environment.commands)
    assert environment.request["settings"]["smoke"]
    assert context.n_input_tokens == 12 and context.n_output_tokens == 5
    assert context.cost_usd is None  # unknown usage is not silently reported as zero


async def test_cancellation_releases_pending_exec(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: None)
    agent = RefundInspectAgent(logs_dir=tmp_path, model_name="mockllm/model", smoke=True)
    environment = FakeEnvironment(wait=True)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            agent.run(load_scenario("train_refund").task_text, environment, AgentContext()),
            timeout=0.03,
        )
    assert environment.cancelled


async def test_worker_failure_is_reported_to_harbor(tmp_path, monkeypatch):
    monkeypatch.setattr("refund_eval.harbor.agent.kill_reason", lambda: None)
    agent = RefundInspectAgent(logs_dir=tmp_path, model_name="mockllm/model", smoke=True)
    with pytest.raises(RuntimeError, match="exited 7"):
        await agent.run(
            load_scenario("train_refund").task_text, FakeEnvironment(return_code=7), AgentContext(),
        )


async def test_multi_model_job_interleaves_models_with_one_concurrency_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-key")
    from harbor.job import Job

    models = ['openrouter/provider/first', 'openrouter/provider/second']
    path = prepare(tmp_path / 'multi', settings=TrialSettings(
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
        prepare(tmp_path / 'multi', settings=TrialSettings(), models=models)
    assert not (tmp_path / 'multi').exists()

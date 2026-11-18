"""The native Harbor task: adapter output loaded with Harbor's own models, the installed agent against a
fake environment, and converter output against Harbor's ATIF model. No sandboxes or inference.

Run with scripts/harbor python -m pytest tests/test_harbor_native_task.py.
"""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.harbor

pytest.importorskip("harbor.models.job.config")

from harbor.models.agent.context import AgentContext
from harbor.models.dataset.manifest import DatasetManifest
from harbor.models.job.config import JobConfig
from harbor.models.task.config import MCPServerConfig
from harbor.models.task.task import Task
from harbor.models.trajectories import Trajectory
from harbor.publisher.packager import Packager
from harbor.utils.trajectory_validator import TrajectoryValidator

from refund_eval.agent.prompt import task_message
from refund_eval.harbor import adapter, atif, batch
from refund_eval.harbor.installed_agent import RefundAgentOptions, RefundInstalledAgent
from refund_eval.scenario.schema import load_scenario


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("harbor_tasks")
    assert adapter.main(["--scenario", "train_refund_late", "--out", str(out), "--sim-weeks", "12"]) == 0
    return out


def test_cost_limit_schema_documents_target_only_scope():
    field = RefundAgentOptions.model_json_schema()["properties"]["cost_limit"]
    assert "target-only" in field["description"]
    assert "excludes world-service model calls" in field["description"]
    assert field["default"] is None


def test_task_loads_with_harbors_task_model(generated):
    task_dir = generated / "train_refund_late"
    config = Task(task_dir).config
    assert config.schema_version == "1.4"
    assert config.task.name == "refund-eval/train_refund_late"
    meta = config.metadata
    assert meta["task_version"] == adapter.TASK_VERSION and meta["scenario"] == "train_refund_late"
    assert meta["rules_sha256"] == adapter.rules_sha256("train_refund_late") != "none"
    assert len(meta["source_sha256"]) == 64 and "trial name" in meta["seed_policy"]
    assert (meta["sim_weeks"], meta["research_mode"], meta["data_mode"]) == (12.0, "auto", "hybrid")
    assert config.verifier.environment_mode.value == "separate"
    [collect] = config.verifier.collect
    assert collect.service == "world" and "127.0.0.1:9000/agentenv" in collect.command
    assert "$WORLD_ADMIN_TOKEN" in collect.command and "final_snapshot.json" in collect.command
    sources = {(a.source, a.service, a.destination) for a in config.artifacts}
    assert sources == {("/var/log/world", "world", "world"), ("/logs/agent/inspect", None, "agent-eval")}
    [server] = config.environment.mcp_servers
    assert (server.name, server.url) == ("world", "http://world:8000/mcp")
    env = config.environment.env
    assert set(env) == {"WORLD_SCENARIO", "WORLD_SIM_WEEKS", "WORLD_DATA_MODE", "WORLD_TURN_TOKEN"}
    assert env["WORLD_TURN_TOKEN"].startswith("${WORLD_TURN_TOKEN:-") and len(env["WORLD_TURN_TOKEN"]) > 40
    assert config.agent.timeout_sec == 39600 and config.environment.network_mode.value == "public"
    instruction = (task_dir / "instruction.md").read_text()
    assert instruction == task_message(load_scenario("train_refund_late"), 12.0) + "\n"


def test_compose_world_sidecar(generated):
    environment = generated / "train_refund_late" / "environment"
    compose = yaml.safe_load((environment / "docker-compose.yaml").read_text())
    assert compose["services"]["main"] == {"depends_on": {"world": {"condition": "service_healthy"}}}
    world = compose["services"]["world"]
    assert world["build"] == {"context": "${CONTEXT_DIR}", "dockerfile": "world/Dockerfile"}
    assert world["volumes"] == ["world-logs:/var/log/world"] and "world-logs" in compose["volumes"]
    assert world["security_opt"] == ["seccomp=unconfined"]
    assert world["environment"]["OPENROUTER_API_KEY"] == "${OPENROUTER_API_KEY:-}"
    assert world["environment"]["WORLD_TRIAL"] == "${COMPOSE_PROJECT_NAME}"
    assert world["environment"]["WORLD_RESEARCH_MODE"] == "auto"
    assert world["environment"]["WORLD_ADMIN_TOKEN"].startswith("${WORLD_ADMIN_TOKEN:-")
    # The admin token is the world's only; `main` gets the turn token through the task env.
    assert "WORLD_ADMIN_TOKEN" not in Task(generated / "train_refund_late").config.environment.env
    assert "WORLD_ADMIN_TOKEN" not in compose["services"]["main"]
    dockerfile = (environment / "world" / "Dockerfile").read_text()
    assert "HEALTHCHECK" in dockerfile and "8000" in dockerfile and "refund_eval.harbor.world_entrypoint" in dockerfile
    for text in (dockerfile, (environment / "docker-compose.yaml").read_text()):
        assert "__" not in text.replace("__env", "")


@pytest.mark.parametrize("mode", ["auto", "saved", "live"])
def test_adapter_preserves_explicit_research_mode(tmp_path, mode):
    assert adapter.main(["--scenario", "train_refund_late", "--out", str(tmp_path),
                         "--research-mode", mode]) == 0
    task_dir = tmp_path / "train_refund_late"
    assert Task(task_dir).config.metadata["research_mode"] == mode
    compose = yaml.safe_load((task_dir / "environment" / "docker-compose.yaml").read_text())
    assert compose["services"]["world"]["environment"]["WORLD_RESEARCH_MODE"] == mode


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_merges_with_harbors_base_file(generated):
    import harbor.environments.docker as harbor_docker

    environment = generated / "train_refund_late" / "environment"
    base = Path(harbor_docker.__file__).with_name("docker-compose-build.yaml")
    env = {**os.environ, "CONTEXT_DIR": str(environment), "WORLD_SCENARIO": "train_refund_late",
           "WORLD_SIM_WEEKS": "12", "WORLD_DATA_MODE": "hybrid", "WORLD_TURN_TOKEN": "t"}
    result = subprocess.run(["docker", "compose", "-p", "trial__abc__env", "--project-directory", str(environment),
                             "-f", str(base), "-f", str(environment / "docker-compose.yaml"), "config", "--format",
                             "json"], env=env, capture_output=True, text=True, check=False)
    if result.returncode and "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin not installed")
    assert result.returncode == 0, result.stderr
    merged = json.loads(result.stdout)
    assert merged["services"]["world"]["environment"]["WORLD_TRIAL"] == "trial__abc__env"
    assert merged["services"]["main"]["depends_on"]["world"]["condition"] == "service_healthy"


def test_verifier_image_and_test_script(generated):
    tests = generated / "train_refund_late" / "tests"
    test_sh = tests / "test.sh"
    assert test_sh.stat().st_mode & stat.S_IXUSR
    script = test_sh.read_text()
    assert "python -m refund_eval.grading.verifier" in script and "--out /logs/verifier/reward.json" in script
    assert "/var/log/world/state.json" in script and "/logs/agent/inspect/*.eval" in script
    assert "COPY test.sh /tests/test.sh" in (tests / "Dockerfile").read_text()
    for source in (tests / "source", generated / "train_refund_late" / "environment" / "source"):
        assert (source / "refund_eval" / "grading" / "verifier.py").is_file()
        assert (source / "uv.lock").is_file() and not (source / ".env").exists()


def test_main_image_builds_from_only_the_agent_payload(generated):
    task_dir = generated / "train_refund_late"
    environment = task_dir / "environment"
    payload = environment / "agent"
    copy_inputs = [line.split()[1:-1] for line in (environment / "Dockerfile").read_text().splitlines()
                   if line.startswith("COPY ") and not line.startswith("COPY --from=")]
    assert copy_inputs == [["agent/pyproject.toml", "agent/uv.lock"], ["agent/"]]
    assert (environment / "Dockerfile.dockerignore").read_text().splitlines() == ["*", "!agent/", "!agent/**"]
    for forbidden in ("scenario/rules", "scenario/dossiers", "scenario/realism", "grading", "claims",
                      "sim/rules.py", "tasks/train_refund/checklist.json", "tasks/household_bills/checklist.json"):
        assert not (payload / "refund_eval" / forbidden).exists()
    assert not list(payload.rglob("test.sh"))
    assert (task_dir / "tests" / "test.sh").is_file()
    assert "COPY source/ /app/" in (environment / "world" / "Dockerfile").read_text()
    for source in (environment / "source", task_dir / "tests" / "source"):
        assert (source / "refund_eval" / "scenario" / "rules" / "train_refund_late").is_dir()
        assert (source / "refund_eval" / "grading" / "verifier.py").is_file()

    original = load_scenario()
    path = payload / "refund_eval" / "scenario" / "train_refund_late.yaml"
    raw = yaml.safe_load(path.read_text())
    assert not {"intent", "grading_notes", "seed_emails", "scheduled_emails"} & raw.keys()
    assert all(not {"policy", "terms", "persona"} & org.keys() for org in raw["orgs"])
    assert "true_circumstances" not in raw["facts"]
    assert task_message(load_scenario(str(path)), 12.0) == task_message(original, 12.0)
    manifest = Path("refund_eval/_source_manifest.json")
    agent_manifest = json.loads((payload / manifest).read_text())
    world_manifest = json.loads((environment / "source" / manifest).read_text())
    memory = environment / "source" / "refund_eval" / "scenario" / "realism" / "train_refund_late.json"
    assert agent_manifest.pop("realism_memory") == {
        "train_refund_late": hashlib.sha256(memory.read_bytes()).hexdigest()[:8],
    }
    assert agent_manifest == world_manifest
    assert agent_manifest["source_sha256"] == Task(task_dir).config.metadata["source_sha256"]


def test_dataset_metric_and_job(generated):
    manifest = DatasetManifest.from_toml_file(generated / "dataset.toml")
    assert [t.name for t in manifest.tasks] == ["refund-eval/train_refund_late"]
    assert [f.path for f in manifest.files] == ["metric.py"]
    assert (generated / "metric.py").read_text() == adapter.METRIC.read_text()
    job = JobConfig.model_validate(yaml.safe_load((generated / "job.yaml").read_text()))
    [agent] = job.agents
    assert agent.import_path == adapter.AGENT_IMPORT_PATH
    assert agent.kwargs["cost_limit"] == 150.0 and agent.kwargs["token_budget"] == 40_000_000
    assert agent.env == {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"}
    assert job.retry.max_retries == 0


def test_adapter_refuses_to_overwrite(generated):
    with pytest.raises(FileExistsError):
        adapter.main(["--scenario", "train_refund_late", "--out", str(generated)])


@pytest.mark.parametrize(("execution", "seed"), [("docker", None), ("modal", 0)])
def test_batch_freezes_one_task_and_the_job_for_all_targets(tmp_path, capsys, monkeypatch, execution, seed):
    monkeypatch.setenv("WORLD_SEED", "17")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-packaged")
    config = tmp_path / "config.json"
    out = tmp_path / "private run"
    config.write_text(json.dumps({
        "execution": execution, "scenario": "train_refund_late", "data_mode": "fixture",
        "research_mode": "saved", "models": ["openrouter/a", "openrouter/b"],
        "cost_limit": {"openrouter/a": 1.0, "openrouter/b": 2.0},
        "max_turns": 50, "sim_weeks": 3, "time_limit": 120, "history_tokens": 0,
        "budget_notice": "daily", "trials": 3, "concurrency": 2, "seed": seed,
    }))
    assert batch.main(["--config", str(config), "--out", str(out)]) == 0
    assert f"scripts/harbor harbor run -c '{out / 'job.yaml'}'" in capsys.readouterr().out
    job_bytes = (out / "job.yaml").read_bytes()
    job = JobConfig.model_validate(yaml.safe_load(job_bytes))
    task = out / "train_refund_late"
    [job_task] = job.tasks
    assert job_task.path == task
    assert job.environment.type.value == execution and job.retry.max_retries == 0
    assert (job.n_attempts, job.n_concurrent_trials) == (3, 2)
    assert [(a.model_name, a.kwargs["cost_limit"]) for a in job.agents] == [
        ("openrouter/a", 1.0), ("openrouter/b", 2.0),
    ]
    for agent in job.agents:
        assert agent.kwargs == {"cost_limit": agent.kwargs["cost_limit"], "token_budget": 40_000_000,
                                "time_limit": 120, "history_tokens": 0, "budget_notice": "daily"}
    compose = (task / "environment" / "docker-compose.yaml").read_text()
    world = yaml.safe_load(compose)["services"]["world"]["environment"]
    assert world["WORLD_SEED"] == ("" if seed is None else "0")
    assert world["WORLD_MAX_TURNS"] == "50" and world["WORLD_RESEARCH_MODE"] == "saved"
    meta = Task(task).config.metadata
    assert (meta["sim_weeks"], meta["data_mode"]) == (3.0, "fixture")
    record_text = (out / "run.json").read_text()
    record = json.loads(record_text)
    assert record["total_trials"] == 6 and record["config"]["seed"] == seed
    assert record["seed_policy"] == meta["seed_policy"]
    assert record["task"]["digest"] == f"sha256:{Packager.compute_content_hash(task)[0]}"
    assert record["task"]["rules_sha256"] == meta["rules_sha256"]
    assert record["task"]["source_sha256"] == meta["source_sha256"]
    assert record["job"]["sha256"] == hashlib.sha256(job_bytes).hexdigest()
    assert all("test-key-never-packaged" not in artifact for artifact in (record_text, job_bytes.decode(), compose))
    assert not (out / "jobs").exists()
    with pytest.raises(FileExistsError, match="empty output directory"):
        batch.prepare(out, batch.RunConfig.model_validate_json(config.read_bytes()))


def test_batch_rejects_untrackable_inputs_before_writing(tmp_path):
    with pytest.raises(ValueError, match="concurrency cannot exceed"):
        batch.prepare(tmp_path / "private", batch.RunConfig(scenario="bills_price_rise", execution="docker", concurrency=2))
    assert not (tmp_path / "private").exists()
    with pytest.raises(ValueError, match="Git-ignored"):
        batch.prepare(adapter.ROOT / "trackable-native-batch",
                      batch.RunConfig(scenario="bills_price_rise", execution="docker"))


# --------------------------------------------------------------------------- installed agent


WORLD = [MCPServerConfig(name="world", transport="streamable-http", url="http://world:8000/mcp")]


def make_agent(tmp_path, **kwargs):
    return RefundInstalledAgent(logs_dir=tmp_path / "train_refund__abc" / "agent", model_name=kwargs.pop(
        "model_name", "openrouter/anthropic/claude-opus-5.5"), mcp_servers=WORLD, **kwargs)


def test_inspect_command(tmp_path):
    command = make_agent(tmp_path, cost_limit=150, token_budget=40_000_000, history_tokens=60000, time_limit=36000,
                         reasoning=True).inspect_command()
    run, convert = command.split("; status=$?; ")
    assert run.startswith("mkdir -p /logs/agent/inspect; /app/.venv/bin/inspect eval refund_eval/task.py ")
    for part in ("--model openrouter/anthropic/claude-opus-5.5", "-M strict_tools=false", "-M reasoning_enabled=true",
                 "-T world_url=http://world:8000", '-T scenario="$WORLD_SCENARIO"', '-T sim_weeks="$WORLD_SIM_WEEKS"',
                 '-T published_data="$WORLD_DATA_MODE"', "-T history_tokens=60000", "-T token_budget=40000000",
                 "-T cost_limit=150.0", "-T time_limit=36000", "--log-dir /logs/agent/inspect",
                 "--log-format eval", "--metadata harbor_trial=train_refund__abc", "> /logs/agent/inspect.log 2>&1"):
        assert part in run
    assert convert.startswith("/app/.venv/bin/python -m refund_eval.harbor.atif /logs/agent/inspect "
                              "--out /logs/agent/trajectory.json")
    assert convert.endswith("exit $status")
    mock = make_agent(tmp_path, model_name="mockllm/model").inspect_command()
    assert "-M " not in mock and "-T cost_limit" not in mock and "-T token_budget" not in mock


def test_options_are_validated(tmp_path):
    with pytest.raises(ValueError):
        make_agent(tmp_path, cost_limit=0)
    with pytest.raises(ValueError):
        make_agent(tmp_path, max_turns=10)  # a world setting, fixed in the task


class Environment:
    def __init__(self, on_run=None):
        self.commands, self.on_run, self.default_user = [], on_run, None

    async def exec(self, command, **kwargs):
        self.commands.append((command, kwargs.get("cwd")))
        if "inspect eval" in command and self.on_run is not None:
            await self.on_run(self)
        return SimpleNamespace(return_code=0, stdout="", stderr="")


async def test_run_executes_inspect_in_app_and_forwards_the_kill_switch(tmp_path, monkeypatch):
    kill_file = tmp_path / "KILL"
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(kill_file))

    async def run(environment):
        kill_file.touch()  # the operator stops the batch mid-trial
        while not any("touch /app/.context/KILL" in c for c, _ in environment.commands):
            await asyncio.sleep(0.05)

    environment = Environment(on_run=run)
    agent = make_agent(tmp_path, model_name="mockllm/model")
    await asyncio.wait_for(agent.run("ignored", environment, AgentContext()), timeout=10)
    [(command, cwd)] = [c for c in environment.commands if "inspect eval" in c[0]]
    assert cwd == "/app" and "world_url=http://world:8000" in command
    assert [c for c, _ in environment.commands if "KILL" in c] == ["mkdir -p /app/.context && touch /app/.context/KILL"]


async def test_kill_switch_blocks_a_new_trial(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_KILL", "1")
    environment = Environment()
    with pytest.raises(RuntimeError, match="kill switch"):
        await make_agent(tmp_path).run("ignored", environment, AgentContext())
    assert environment.commands == []


def test_world_url_comes_from_the_tasks_mcp_server(tmp_path):
    agent = RefundInstalledAgent(logs_dir=tmp_path, model_name="mockllm/model", mcp_servers=[
        MCPServerConfig(name="world", transport="streamable-http", url="http://world-b:9100/mcp")])
    assert agent.world_url() == "http://world-b:9100"
    assert RefundInstalledAgent(logs_dir=tmp_path, model_name="mockllm/model").world_url() == "http://world:8000"


def test_scripted_parity_agent_runs_inspect_through_its_script(tmp_path):
    from refund_eval.parity.harbor_agent import ScriptedRefundAgent

    command = ScriptedRefundAgent(logs_dir=tmp_path / "t__abc" / "agent", model_name="mockllm/model",
                                  mcp_servers=WORLD, script="turn_cap", cost_limit=1).inspect_command()
    assert "/app/.venv/bin/python -m refund_eval.parity.inspect_cli turn_cap eval refund_eval/task.py " in command
    assert "/app/.venv/bin/inspect eval" not in command and "-T cost_limit=1.0" in command
    with pytest.raises(ValueError, match="mockllm"):
        ScriptedRefundAgent(logs_dir=tmp_path, model_name="openrouter/a/b", mcp_servers=WORLD,
                            script="turn_cap").inspect_command()


# --------------------------------------------------------------------------- ATIF


def fake_log():
    """An Inspect log's shape, as `atif.convert` reads it, without Inspect."""
    now = datetime(2026, 11, 16, tzinfo=UTC)
    usage = SimpleNamespace(input_tokens=100, input_tokens_cache_read=700, input_tokens_cache_write=None,
                            output_tokens=50, reasoning_tokens=None, total_cost=0.25,
                            model_dump=lambda mode=None: {"input_tokens": 100})
    call = SimpleNamespace(id="call_1", function="search_threads", arguments={"query": "is:unread"})
    reasoning = SimpleNamespace(type="reasoning", reasoning="Check the inbox.", redacted=False)
    text = SimpleNamespace(type="text", text="Checking.")
    messages = [
        SimpleNamespace(role="system", content="You are an assistant."),
        SimpleNamespace(role="user", content="Get me a refund."),
        SimpleNamespace(role="assistant", id="m1", content=[reasoning, text], tool_calls=[call]),
        SimpleNamespace(role="tool", tool_call_id="call_1", content="No new mail.", error=None),
        SimpleNamespace(role="assistant", id="m2", content="Waiting.", tool_calls=None),
    ]

    def model_event(message_id):
        output = SimpleNamespace(choices=[1], message=SimpleNamespace(id=message_id), model="openrouter/x/y",
                                 usage=usage)
        return SimpleNamespace(event="model", output=output, timestamp=now, model="openrouter/x/y")

    clock = SimpleNamespace(event="info", source=atif.SIM_CLOCK_SOURCE,
                            data={"sim_time": "2026-09-25T09:00:00+01:00", "turn": 0, "stop_reason": None})
    sample = SimpleNamespace(messages=messages, events=[clock, model_event("m1"), model_event("m2")],
                             model_usage={"openrouter/x/y": usage}, uuid="sample-1", error=None, limit=None)
    return SimpleNamespace(samples=[sample], status="success", error=None,
                           eval=SimpleNamespace(eval_id="e1", task="impossible_refund", model="openrouter/x/y"))


def test_atif_output_is_valid_for_harbor(tmp_path):
    trajectory = atif.convert(fake_log(), agent_version="1.0.0", log_name="trial.eval")
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(trajectory))
    validator = TrajectoryValidator()
    assert validator.validate(str(path)), validator.get_errors()
    model = Trajectory.model_validate(trajectory)
    agent_step = model.steps[2]
    assert agent_step.reasoning_content == "Check the inbox." and agent_step.message == "Checking."
    assert agent_step.observation.results[0].source_call_id == "call_1"
    assert agent_step.extra == {"sim_time": "2026-09-25T09:00:00+01:00", "sim_turn": 0}
    assert model.final_metrics.total_cost_usd == 0.25 and model.final_metrics.total_prompt_tokens == 800

    agent = make_agent(tmp_path)
    agent.logs_dir.mkdir(parents=True)
    shutil.copy(path, agent.logs_dir / "trajectory.json")
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens, context.cost_usd) == (
        800, 700, 50, 0.25)
    assert context.metadata["inspect_log"] == "trial.eval" and "target model only" in context.metadata["cost_scope"]

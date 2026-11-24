"""The native Harbor task's pieces that run in the benchmark environment: the `.eval` -> ATIF converter
on a mockllm world-service trial, and the world image's entrypoint (settings, seed, fake models).

Offline, as tests/test_world_service.py: fixture data, fake world-side models and a stub browser.
The Harbor-side pieces (adapter, installed agent) are in tests/test_harbor_native_task.py.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, ModelUsage

from refund_eval.agent.prompt import task_message
from refund_eval.agent.world_mode import TURN_TOKEN_ENV
from refund_eval.harbor import adapter, atif, world_entrypoint
from refund_eval.harbor.target_charges import (
    TARGET_CHARGES_DIRECTORY,
    TARGET_CHARGES_ENV,
)
from refund_eval.harbor.world_entrypoint import (
    FakeWorldModels,
    choose_seed,
    credentials_set,
    trial_seed,
    world_argv,
)
from refund_eval.models.client import Message, OutputSchema
from refund_eval.scenario.schema import SeedEmail, load_scenario
from refund_eval.world_service.store import ACTIVITY_KEY
from tests.test_world_service import (  # noqa: F401 - `offline` is an autouse fixture
    MAX_TURNS,
    WORLD_KEY,
    ScriptedAgent,
    _write_agent_scenario,
    offline,
    run_inspect,
    settings,
    stub_browser,
    world_service_thread,
)


class WithUsage(ScriptedAgent):
    """The scripted agent, with provider usage and cost on every generation."""

    def __call__(self, input, tools, tool_choice, config):
        output: ModelOutput = super().__call__(input, tools, tool_choice, config)
        output.usage = ModelUsage(input_tokens=100, input_tokens_cache_read=700, output_tokens=50,
                                  total_tokens=850, total_cost=0.01)
        return output


# Run this in the installed interpreter, not in pytest's checkout environment.
INSTALLED_AGENT_SMOKE = """
import sys
import sysconfig
from pathlib import Path

def local_only(event, args):
    if event == "socket.getaddrinfo" and args[0] not in (None, "127.0.0.1"):
        raise AssertionError(f"Non-loopback DNS: {args[0]}")
    if event == "socket.connect" and isinstance(args[1], tuple) and args[1][0] != "127.0.0.1":
        raise AssertionError(f"Non-loopback connection: {args[1]}")

sys.addaudithook(local_only)

import inspect_ai
import refund_eval.task
from inspect_ai.model import ModelOutput, ModelUsage, get_model
from refund_eval.harbor import atif

site = Path(sysconfig.get_path("purelib")).resolve()
for module in (inspect_ai, refund_eval.task, atif):
    assert Path(module.__file__).resolve().is_relative_to(site), module.__file__

model = "mockllm/model"
output = ModelOutput.for_tool_call(model, "list_labels", {}, tool_call_id="installed_smoke_call")
output.usage = ModelUsage(input_tokens=10, output_tokens=2, total_tokens=12, total_cost=0)
[log] = inspect_ai.eval(
    refund_eval.task.impossible_refund, model=get_model(model, custom_outputs=[output]),
    task_args={"scenario": "train_refund_late", "world_url": sys.argv[1], "sim_weeks": 1,
               "published_data": "fixture", "research_mode": "saved", "history_tokens": 0, "cost_limit": 1},
    log_dir="inspect", log_format="eval", display="none",
)
assert log.status == "success", log.error
assert atif.main(["inspect", "--out", "trajectory.json"]) == 0
"""


# Host runs Harbor's real installed-agent method; only container filesystem and
# sidecar copy operations are translated to local paths. Inspect runs from the
# non-editable public installation, against the real fixture world service.
INSTALLED_STATUS_HOST = r'''
import asyncio, json, os, shutil, sys
from pathlib import Path
from types import SimpleNamespace
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import MCPServerConfig
from refund_eval.harbor.installed_agent import RefundInstalledAgent
from refund_eval.dashboard.luna_publisher import publish_once
from refund_eval.dashboard.luna import Feed
from refund_eval.world_service.status import WORLD_STATUS_FILE

python, app, world, url, trial = sys.argv[1:]
python, app, world, trial = map(Path, (python, app, world, trial))
logs = trial / "agent"
logs.mkdir(parents=True)

class LocalEnvironment:
    default_user = None
    async def exec(self, command, **kwargs):
        assert "luna-status" not in command and "luna-source" not in command
        command = command.replace("/app/.venv/bin/python", str(python))
        command = command.replace("/app/.venv/bin/inspect", str(python.parent / "inspect"))
        command = command.replace("/logs/agent", str(logs)).replace("/app", str(app))
        env = {key: os.environ[key] for key in ("PATH", "WORLD_TURN_TOKEN")}
        env.update(HOME=str(app), PYTHON_DOTENV_DISABLED="1", REFUND_EVAL_COMPANY_STORE="off",
                   WORLD_SCENARIO="train_refund_late", WORLD_SIM_WEEKS="1", WORLD_DATA_MODE="fixture")
        process = await asyncio.create_subprocess_shell(command, cwd=app, env=env,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, error = await process.communicate()
        assert process.returncode == 0, (out + error).decode()
        return SimpleNamespace(return_code=process.returncode, stdout=out.decode(), stderr=error.decode())
    async def service_download_file(self, source, destination, *, service):
        assert service == "world" and source == f"/var/log/world/{WORLD_STATUS_FILE}"
        shutil.copyfile(world / WORLD_STATUS_FILE, destination)

async def main():
    agent = RefundInstalledAgent(logs_dir=logs, model_name="mockllm/model", cost_limit=1,
        time_limit=30, history_tokens=0, mcp_servers=[MCPServerConfig(name="world",
        transport="streamable-http", url=url + "/mcp")])
    context = AgentContext()
    await agent.run("Ignored by the installed agent", LocalEnvironment(), context)
    assert not context.metadata  # no telemetry fed back into agent context
    source = trial / "luna-source.json"
    status = json.loads(source.read_text())
    assert status["turns"] == 1 and status["run_id"] == trial.name
    assert status["model"] == "mockllm/model" and status["agent_spend_usd"] is None
    assert status["schema_version"] == 2 and status["steps"]
    assert all(set(step) == {"seq", "at", "simulated_date", "turn", "action", "outcome"} for step in status["steps"])
    assert any(step["action"] == "turn" for step in status["steps"])
    output = trial / "luna-status.json"
    publish_once(source, output, run_id=trial.name, result_path=trial / "result.json")
    consumed = Feed(output, run_id=trial.name).read()
    assert consumed["turns"] == 1 and len(consumed["steps"]) == len(status["steps"])
    assert not list(logs.rglob("*luna*"))  # status is outside the agent log mount
    print("Installed-agent native status: PASS")

asyncio.run(main())
'''


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_HARBOR_PACKAGE_SMOKE") != "1",
                    reason="needs a local Python 3.14 and cached uv dependencies")
def test_native_agent_wheel_starts_and_records_first_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", "off")
    wheel_path_value = os.environ.get("REFUND_EVAL_HARBOR_AGENT_WHEEL")
    expected_sha256 = os.environ.get("REFUND_EVAL_HARBOR_AGENT_WHEEL_SHA256")
    if bool(wheel_path_value) != bool(expected_sha256):
        pytest.fail("Set both REFUND_EVAL_HARBOR_AGENT_WHEEL and its SHA-256", pytrace=False)
    wheel_path = Path(wheel_path_value).expanduser().resolve() if wheel_path_value else None
    if wheel_path is not None:
        if not wheel_path.is_file():
            pytest.fail("The exact agent wheel does not exist", pytrace=False)
        actual_sha256 = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256.lower():
            pytest.fail("The exact agent wheel SHA-256 does not match", pytrace=False)
    else:
        # Build an explicitly TEST ONLY public wheel; no task launch or approval
        # of these bytes for a paid run is implied by this offline smoke.
        source, staged = tmp_path / "source", tmp_path / "public-agent"
        adapter.copy_source(source)
        adapter.stage_agent(source, staged, adapter.load_scenario("train_refund_late"),
                            scenario_name="train_refund_late")
        subprocess.run(["uv", "build", "--offline", "--wheel", str(staged), "--out-dir", str(tmp_path / "wheels")],
                       check=True, capture_output=True, text=True, timeout=60)
        [wheel_path] = (tmp_path / "wheels").glob("*.whl")
        expected_sha256 = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    task_dir = adapter.write_task(tmp_path / "prepared", scenario="train_refund_late",
                                  data_mode="fixture", research_mode="saved", sim_weeks=1, max_turns=1,
                                  simulator_model="mockllm/model", grader_model="mockllm/model",
                                  agent_timeout=60, verifier_timeout=60, cpus=1, memory_mb=1024,
                                  agent_wheel=wheel_path, agent_wheel_sha256=expected_sha256)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    venv = tmp_path / "venv"
    env = {"PATH": os.environ["PATH"], "HOME": str(run_dir), "PYTHON_DOTENV_DISABLED": "1"}
    uv_env = {key: os.environ[key] for key in ("HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR") if key in os.environ}
    sync_args = ["uv", "sync", "--offline", "--no-config", "--frozen", "--no-dev", "--python", "3.14",
                 "--project", str(task_dir / "environment" / "agent")]
    sync_args.append("--no-install-project" if wheel_path is not None else "--no-editable")
    subprocess.run(
        sync_args,
        cwd=run_dir, check=True, timeout=120,
        env={**env, **uv_env, "UV_PYTHON_DOWNLOADS": "never", "UV_PROJECT_ENVIRONMENT": str(venv)},
    )
    if wheel_path is not None:
        subprocess.run(
            ["uv", "pip", "install", "--offline", "--no-deps", "--python", str(venv / "bin" / "python"),
             str(wheel_path)],
            cwd=run_dir, check=True, timeout=120, env={**env, **uv_env, "UV_PYTHON_DOWNLOADS": "never"},
        )
    _check_installed_agent_first_turn(venv / "bin" / "python", run_dir, tmp_path, env, monkeypatch)


def test_native_agent_stage_does_not_depend_on_private_scenario_values(tmp_path):
    name = "train_refund_late"
    scenario = adapter.load_scenario(name)
    change = {field: f"PRIVATE_HARBOR_CANARY_{field.upper()}" for field in (
        "origin", "destination", "departure_time", "operator", "retailer", "ticket_type",
        "booking_ref", "paid_by", "cancellation_reason", "currency",
    )}
    circumstances = "PRIVATE_HARBOR_CANARY_CIRCUMSTANCES"
    journey = scenario.journey.model_copy(update={
        **change, "date": date(2041, 7, 9), "fare": 8127.94, "cancelled": not scenario.journey.cancelled,
    })
    email = SeedEmail(id="canary", sender="fixture@example.test", to=["recipient@example.test"],
                      subject="PRIVATE_HARBOR_CANARY_EMAIL", body="Synthetic mail", sent_at=scenario.start_time)
    variant = scenario.model_copy(update={
        "journey": journey, "true_circumstances": [circumstances],
        "intent": "PRIVATE_HARBOR_CANARY_INTENT", "grading_notes": "PRIVATE_HARBOR_CANARY_GRADING",
        "seed_emails": [email], "scheduled_emails": [email],
        "user": scenario.user.model_copy(update={
            "home_town": "PRIVATE_HARBOR_CANARY_TOWN", "card_last4": "4732",
        }),
        "orgs": [org.model_copy(update={"policy": "PRIVATE_HARBOR_CANARY_POLICY"}) for org in scenario.orgs],
    })
    source = tmp_path / "source"
    adapter.copy_source(source)

    def snapshot(agent, sc):
        adapter.stage_agent(source, agent, sc, scenario_name=name)
        fingerprints = {}
        for file in sorted(agent.rglob("*")):
            if not file.is_file():
                continue
            relative = file.relative_to(agent).as_posix()
            contents = file.read_bytes()
            if b"PRIVATE_HARBOR_CANARY_" in contents or "PRIVATE_HARBOR_CANARY_" in relative:
                pytest.fail("A synthetic private scenario value reached the staged agent", pytrace=False)
            fingerprints[relative] = hashlib.sha256(contents).hexdigest()
        _check_public_scenario(agent / "refund_eval" / "scenario" / f"{name}.yaml", scenario)
        return fingerprints

    assert snapshot(tmp_path / "variant", variant) == snapshot(tmp_path / "original", scenario)


def _check_public_scenario(path, scenario):
    try:
        public = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        pytest.fail("Missing or invalid agent-visible scenario", pytrace=False)
    if not isinstance(public, dict) or set(public) != {
        "id", "task_text", "start_time", "requested_deadline", "sim_weeks", "user",
    }:
        pytest.fail("Agent scenario fields exceed or differ from the public contract", pytrace=False)
    expected_user = scenario.user.model_dump(include={"name", "email_addresses", "phone"}, exclude_none=True)
    user = public["user"]
    if not isinstance(user, dict) or {k: v for k, v in user.items() if v is not None or k != "phone"} != expected_user:
        pytest.fail("Agent profile differs from the public contract", pytrace=False)
    try:
        start_time = datetime.fromisoformat(str(public["start_time"]))
    except ValueError:
        pytest.fail("Agent scenario has invalid public timing", pytrace=False)
    requested_deadline = (scenario.requested_deadline.isoformat() if scenario.requested_deadline else None)
    if (public["id"], public["task_text"], start_time, public["requested_deadline"], public["sim_weeks"]) != (
        scenario.id, scenario.task_text, scenario.start_time, requested_deadline, scenario.sim_weeks,
    ):
        pytest.fail("Agent scenario does not match the resolved public values", pytrace=False)


def test_public_scenario_contract_accepts_explicit_requested_deadline(tmp_path):
    scenario = load_scenario().model_copy(update={"requested_deadline": date(2027, 2, 4)})
    public = {
        "id": scenario.id,
        "task_text": scenario.task_text,
        "start_time": scenario.start_time.isoformat(),
        "requested_deadline": scenario.requested_deadline.isoformat(),
        "sim_weeks": scenario.sim_weeks,
        "user": scenario.user.model_dump(include={"name", "email_addresses", "phone"}, exclude_none=True),
    }
    path = tmp_path / "agent-scenario.yaml"
    path.write_text(yaml.safe_dump(public))

    _check_public_scenario(path, scenario)


def _check_installed_agent_first_turn(python, run_dir, tmp_path, env, monkeypatch):
    name = "train_refund_late"
    installed = subprocess.check_output(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        cwd=run_dir, timeout=10, text=True, env=env,
    )
    scenario = adapter.load_scenario(name)
    _check_public_scenario(Path(installed.strip()) / "refund_eval" / "scenario" / f"{name}.yaml", scenario)
    stub_browser(monkeypatch)
    with world_service_thread(settings(tmp_path, scenario=name, sim_weeks=1, max_turns=1),
                              monkeypatch) as (url, held):
        run = subprocess.run(
            [str(python), "-I", "-c", INSTALLED_AGENT_SMOKE, url],
            cwd=run_dir, timeout=60, check=False, capture_output=True, text=True,
            env={**env, TURN_TOKEN_ENV: os.environ[TURN_TOKEN_ENV]},
        )
    assert run.returncode == 0, run.stdout + run.stderr
    [log_path] = (run_dir / "inspect").glob("*.eval")
    assert read_eval_log(log_path).status == "success"
    trajectory = json.loads((run_dir / "trajectory.json").read_text())
    assert trajectory["schema_version"] == "ATIF-v1.8"
    request = next(step["message"] for step in trajectory["steps"] if step["source"] == "user")
    if request != task_message(scenario, 1):
        pytest.fail("The installed agent did not receive the task's public request", pytrace=False)
    [step] = [step for step in trajectory["steps"] if step["source"] == "agent"]
    [call] = step["tool_calls"]
    assert (call["tool_call_id"], call["function_name"]) == ("installed_smoke_call", "list_labels")
    [observation] = step["observation"]["results"]
    assert observation["source_call_id"] == call["tool_call_id"]
    assert "INBOX" in observation["content"]
    assert trajectory["extra"]["world_turns"] == 1
    assert trajectory["extra"]["world_stop_reason"] == "max_turns"
    data = held["data"]
    assert data["world"]["turn"] == 1
    assert data["world"]["stop_reason"] == "max_turns"
    [tool, turn] = data["store"][ACTIVITY_KEY]
    assert (tool["kind"], tool["tool"], tool["call_id"], tool["status"]) == (
        "tool_call", call["function_name"], call["tool_call_id"], "ok")
    assert turn["kind"] == "turn" and turn["turn"] == 1 and turn["stopped"]
    # Exercise RefundInstalledAgent.run itself, not a fixture progress.json.
    host_python = Path(__file__).resolve().parents[1] / "integrations/harbor/.venv/bin/python"
    assert host_python.is_file(), "uv sync --frozen --project integrations/harbor is required for the host observer smoke"
    monkeypatch.setenv("WORLD_TRIAL", "test-only-installed__env")
    trial = tmp_path / "host" / "test-only-installed"
    with world_service_thread(settings(tmp_path / "native-status", scenario=name, sim_weeks=1, max_turns=1),
                              monkeypatch) as (url, _):
        run = subprocess.run([str(host_python), "-c", INSTALLED_STATUS_HOST, str(python), str(run_dir),
                              str(tmp_path / "native-status/world"), url, str(trial)],
                             cwd=Path(__file__).resolve().parents[1], timeout=60, check=False,
                             env={**env, TURN_TOKEN_ENV: os.environ[TURN_TOKEN_ENV], "HARBOR_TELEMETRY": "0"},
                             capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    # The public installation has no world telemetry implementation or files.
    assert not (Path(installed.strip()) / "refund_eval/world_service/status.py").exists()


def test_atif_from_a_world_service_trial(tmp_path, monkeypatch):
    stub_browser(monkeypatch)
    with world_service_thread(settings(tmp_path, max_turns=MAX_TURNS), monkeypatch) as (url, held):
        script = WithUsage(lambda: held["trial"].trial.data[WORLD_KEY])
        run_inspect(tmp_path / "remote", script=script, world_url=url)
    [path] = (tmp_path / "remote" / "logs").glob("*.eval")
    out = tmp_path / "agent" / "trajectory.json"
    assert atif.main([str(path.parent), "--out", str(out), "--agent-version", "1.0.0"]) == 0
    trajectory = json.loads(out.read_text())
    log = read_eval_log(str(path))

    assert trajectory["schema_version"] == "ATIF-v1.8"
    assert trajectory["agent"] == {"name": "refund-inspect", "version": "1.0.0", "model_name": "mockllm/model"}
    steps = trajectory["steps"]
    assert [s["step_id"] for s in steps] == list(range(1, len(steps) + 1))
    assert [s["source"] for s in steps[:3]] == ["system", "user", "agent"]
    agent_steps = [s for s in steps if s["source"] == "agent"]
    assert len(agent_steps) == MAX_TURNS  # one step per generation

    # Tool calls keep Inspect's ids and carry their results.
    send = agent_steps[0]
    assert send["tool_calls"][0]["tool_call_id"] == "call_send"
    assert send["tool_calls"][0]["function_name"] == "send_message"
    assert send["observation"]["results"][0]["source_call_id"] == "call_send"
    assert "SENT" in send["observation"]["results"][0]["content"]

    # Simulated time at each generation: the scenario start, then the world's clock after each turn.
    times = [s["extra"]["sim_time"] for s in agent_steps]
    assert times[0] == "2026-09-25T09:00:00+01:00" and times == sorted(times) and times[-1] > times[0]
    assert [s["extra"]["sim_turn"] for s in agent_steps][:3] == [0, 1, 2]
    assert trajectory["extra"]["world_stop_reason"] == "max_turns"
    assert trajectory["extra"]["world_turns"] == MAX_TURNS

    # Per-step metrics include cache reads in prompt tokens; totals are the sample's model usage.
    assert send["metrics"] == {"prompt_tokens": 800, "completion_tokens": 50, "cached_tokens": 700,
                               "cost_usd": 0.01}
    usage = log.samples[0].model_usage["mockllm/model"]
    totals = trajectory["final_metrics"]
    assert totals["total_prompt_tokens"] == usage.input_tokens + usage.input_tokens_cache_read == 800 * len(agent_steps)
    assert totals["total_completion_tokens"] == usage.output_tokens
    assert totals["total_cached_tokens"] == 700 * len(agent_steps)
    assert totals["total_cost_usd"] == pytest.approx(0.01 * len(agent_steps))
    assert totals["total_steps"] == len(steps)
    assert trajectory["extra"]["status"] == "success" and trajectory["extra"]["inspect_log"] == path.name


def test_latest_log_needs_one(tmp_path):
    with pytest.raises(FileNotFoundError):
        atif.latest_log(tmp_path)


@pytest.mark.parametrize("second_usage, second_cost, total", [
    ({"cost": 0.005, "is_byok": False}, "0.005", 0.017),
    ({"cost": 0, "is_byok": False}, "0", 0.012),
    ({}, None, None),
    ({"cost": 0.005, "is_byok": True}, None, None),
    ({"cost": "NaN"}, None, None),
    (None, None, None),
], ids=["all-reported", "reported-zero", "missing", "byok", "invalid", "provider-error"])
def test_native_openrouter_target_receipts(tmp_path, monkeypatch, second_usage, second_cost, total):
    import httpx2
    from inspect_ai import eval
    from inspect_ai.model import get_model

    from refund_eval import task
    from refund_eval.published.provider import published_model_args

    logs = tmp_path / "agent"
    receipt_dir = logs / TARGET_CHARGES_DIRECTORY
    monkeypatch.setenv(TARGET_CHARGES_ENV, str(receipt_dir))
    monkeypatch.setattr(task, "register_openrouter_costs", lambda *_: None)
    monkeypatch.setattr(task, "default_model_roles", lambda *_: {})
    model_name = "openrouter/openai/gpt-6-astra"
    scenario = _write_agent_scenario(tmp_path / "agent-scenario.yaml", load_scenario("train_refund_late"))
    requests = []

    def respond(request):
        assert request.method == "POST" and request.url.path == "/api/v1/chat/completions"
        requests.append(request)
        n = len(requests)
        assert n <= 2
        if n == 2 and second_usage is None:
            return httpx2.Response(402, json={"id": "gen-offline-2", "error": {"message": "Offline error."}})
        return httpx2.Response(200, json={
            "id": f"gen-offline-{n}", "object": "chat.completion", "created": 1,
            "model": model_name.removeprefix("openrouter/"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Offline reply."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
                      **({"cost": 0.012} if n == 1 else second_usage)},
        })

    stub_browser(monkeypatch)
    with world_service_thread(settings(tmp_path, scenario="train_refund_late", sim_weeks=1, max_turns=2),
                              monkeypatch) as (url, _):
        model = get_model(model_name, api_key="offline-placeholder", memoize=False, max_retries=0,
                          http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
                          **published_model_args(model_name))
        [log] = eval(task.impossible_refund, model=model, max_retries=0, display="none", log_format="eval",
                     task_args={"scenario": scenario, "world_url": url, "sim_weeks": 1,
                                "published_data": "fixture", "research_mode": "saved", "history_tokens": 0,
                                "token_budget": 10000, "cost_limit": 1},
                     log_dir=str(logs / "inspect"))
    assert log.status == ("error" if second_usage is None else "success"), log.error
    assert len(requests) == 2
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in receipt_dir.glob("*.json")]
    receipts = {row["generation_id"]: row for row in rows}
    assert set(receipts) == {"gen-offline-1", "gen-offline-2"}
    first, second = (receipts[f"gen-offline-{n}"] for n in (1, 2))
    assert set(first) == {"schema_version", "attempt_id", "status", "generation_id", "actual_cost_usd", "source"}
    assert first["actual_cost_usd"] == "0.012" and first["source"] == "openrouter_response_usage"
    assert second["actual_cost_usd"] == second_cost
    assert second["status"] == ("failed" if second_usage is None else "response")

    out = logs / "trajectory.json"
    assert atif.main([str(logs / "inspect"), "--out", str(out)]) == 0
    trajectory = json.loads(out.read_text(encoding="utf-8"))
    summary = trajectory["extra"]["target_charges"]
    assert summary["attempt_count"] == 2 and summary["unknown_attempt_count"] == int(total is None)
    assert summary["actual_cost_usd"] == total
    totals = trajectory["final_metrics"]
    assert totals.get("total_cost_usd") == summary["actual_cost_usd"]
    assert totals["extra"]["inspect_estimated_cost_usd"] == pytest.approx(0.00125 if second_usage is None else 0.0025)
    for step in (step for step in trajectory["steps"] if step["source"] == "agent"):
        assert "cost_usd" not in step["metrics"]
        assert step["metrics"]["extra"]["inspect_estimated_cost_usd"] == pytest.approx(0.00125)


# --------------------------------------------------------------------------- world entrypoint


def test_seed_is_derived_from_task_version_and_trial():
    env = {"WORLD_TASK_VERSION": "1.0", "WORLD_TRIAL": "train_refund__abc1234__env"}
    chosen = choose_seed(env)
    assert chosen == {"seed": trial_seed("1.0", "train_refund__abc1234"), "source": "derived",
                      "task_version": "1.0", "trial": "train_refund__abc1234"}
    assert choose_seed(env) == chosen  # deterministic
    assert choose_seed({**env, "WORLD_TRIAL": "train_refund__other__env"})["seed"] != chosen["seed"]
    assert choose_seed({**env, "WORLD_TASK_VERSION": "1.1"})["seed"] != chosen["seed"]
    assert 0 <= chosen["seed"] < 2**32
    assert choose_seed({**env, "WORLD_SEED": "7"}) == {"seed": 7, "source": "explicit"}
    assert choose_seed({})["seed"] is None


def test_world_argv_from_environment():
    env = {"WORLD_SCENARIO": "train_refund_late", "WORLD_SIM_WEEKS": "12", "WORLD_MAX_TURNS": "1000",
           "WORLD_DATA_MODE": "hybrid", "WORLD_RESEARCH_MODE": "saved", "WORLD_SIMULATOR_MODEL": "openrouter/a/b",
           "WORLD_GRADER_MODEL": "openrouter/c/d", "WORLD_COMPANY_SIMULATOR": ""}
    argv = world_argv(env, 42)
    assert argv[:4] == ["--browser-runtime", "subprocess", "--output-dir", "/var/log/world"]
    assert argv[-1] == "--require-verified-browser-isolation"
    pairs = dict(zip(argv[4:-1:2], argv[5:-1:2], strict=True))
    assert pairs == {"--scenario": "train_refund_late", "--sim-weeks": "12", "--max-turns": "1000",
                     "--data-mode": "hybrid", "--research-mode": "saved", "--simulator-model": "openrouter/a/b",
                     "--grader-model": "openrouter/c/d", "--seed": "42"}
    from refund_eval.world_service.__main__ import parser

    parser().parse_args(argv)


def test_fake_world_models_refuse_to_start_with_any_key(monkeypatch, tmp_path, capsys):
    assert credentials_set({"OPENROUTER_API_KEY": "", "WORLD_TURN_TOKEN": "t", "WORLD_ADMIN_TOKEN": "a"}) == []
    assert credentials_set({"OPENROUTER_API_KEY": "x", "OPEN_ROUTER_KEY": "y", "REFUND_EVAL_EXA_API_KEY": "z"}) == [
        "OPENROUTER_API_KEY", "OPEN_ROUTER_KEY", "REFUND_EVAL_EXA_API_KEY"]
    monkeypatch.setenv(world_entrypoint.FAKE_MODELS_ENV, "1")
    monkeypatch.setenv("WORLD_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "set")
    served = []
    monkeypatch.setattr("refund_eval.world_service.__main__.main", lambda *a, **kw: served.append(a) or 0)
    assert world_entrypoint.main() == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err and not served and not (tmp_path / "seed.json").exists()


def test_fake_world_models_answer_company_and_consistency_calls():
    from refund_eval.companies.llm import CompanyAction

    fake = FakeWorldModels()
    reply = asyncio.run(fake.complete("simulator", [Message("user", "hi")],
                                      schema=OutputSchema("open_world_action", CompanyAction)))
    CompanyAction.model_validate(reply.json())
    with pytest.raises(RuntimeError, match="no reply"):
        asyncio.run(fake.complete("simulator", [], schema=OutputSchema("other", CompanyAction)))


def test_fake_world_models_can_promise_a_follow_up(monkeypatch, tmp_path):
    from refund_eval.companies.llm import CompanyAction

    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", "off")
    schema = OutputSchema("open_world_action", CompanyAction)
    reply = asyncio.run(FakeWorldModels(30).complete("simulator", [], schema=schema)).json()
    assert CompanyAction.model_validate(reply).follow_up_in_hours == 30 and reply["follow_up_plan"]
    assert asyncio.run(FakeWorldModels().complete("simulator", [], schema=schema)).json().get("follow_up_in_hours") is None

    # An unrecognized fake mode cannot launch uncapped OpenRouter models.
    served = []
    monkeypatch.setattr("refund_eval.world_service.__main__.main", lambda argv, model_client: served.append(
        model_client) or 0)
    monkeypatch.setenv("WORLD_OUTPUT_DIR", str(tmp_path))
    for name in credentials_set(dict(os.environ)):
        monkeypatch.delenv(name)
    for value in ("follow_up", "1"):
        monkeypatch.setenv(world_entrypoint.FAKE_MODELS_ENV, value)
        assert world_entrypoint.main() == 0
    monkeypatch.setenv(world_entrypoint.FAKE_MODELS_ENV, "yes")
    monkeypatch.delenv("WORLD_COST_LIMIT", raising=False)
    assert world_entrypoint.main() == 2
    monkeypatch.setenv("WORLD_COST_LIMIT", "0.75")
    assert world_entrypoint.main() == 0
    assert [m.action.get("follow_up_in_hours") if m else None for m in served] == [
        world_entrypoint.FAKE_FOLLOW_UP_HOURS, None, None]
    assert served[1] is not None and served[2] is None

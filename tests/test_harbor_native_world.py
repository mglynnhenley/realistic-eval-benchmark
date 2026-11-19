"""The native Harbor task's pieces that run in the benchmark environment: the `.eval` -> ATIF converter
on a mockllm world-service trial, and the world image's entrypoint (settings, seed, fake models).

Offline, as tests/test_world_service.py: fixture data, fake world-side models and a stub browser.
The Harbor-side pieces (adapter, installed agent) are in tests/test_harbor_native_task.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess

import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, ModelUsage

from refund_eval.agent.world_mode import TURN_TOKEN_ENV
from refund_eval.harbor import adapter, atif, world_entrypoint
from refund_eval.harbor.world_entrypoint import (
    FakeWorldModels,
    choose_seed,
    credentials_set,
    trial_seed,
    world_argv,
)
from refund_eval.models.client import Message, OutputSchema
from refund_eval.world_service.store import ACTIVITY_KEY
from tests.test_world_service import (  # noqa: F401 - `offline` is an autouse fixture
    MAX_TURNS,
    WORLD_KEY,
    ScriptedAgent,
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


@pytest.mark.skipif(os.environ.get("REFUND_EVAL_HARBOR_PACKAGE_SMOKE") != "1",
                    reason="needs a local Python 3.14 and cached uv dependencies")
def test_native_agent_wheel_starts_and_records_first_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", "off")
    task_dir = adapter.write_task(tmp_path / "prepared", scenario="train_refund_late",
                                  data_mode="fixture", research_mode="saved", sim_weeks=1, max_turns=1,
                                  simulator_model="mockllm/model", grader_model="mockllm/model",
                                  agent_timeout=60, verifier_timeout=60, cpus=1, memory_mb=1024)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    venv = tmp_path / "venv"
    env = {"PATH": os.environ["PATH"], "HOME": str(run_dir), "PYTHON_DOTENV_DISABLED": "1"}
    uv_env = {key: os.environ[key] for key in ("HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR") if key in os.environ}
    subprocess.run(
        ["uv", "sync", "--offline", "--no-config", "--frozen", "--no-dev", "--no-editable", "--python", "3.14",
         "--project", str(task_dir / "environment" / "agent")],
        cwd=run_dir, check=True, timeout=120,
        env={**env, **uv_env, "UV_PYTHON_DOWNLOADS": "never", "UV_PROJECT_ENVIRONMENT": str(venv)},
    )
    _check_installed_agent_first_turn(venv / "bin" / "python", run_dir, tmp_path, env, monkeypatch)


def _check_installed_agent_first_turn(python, run_dir, tmp_path, env, monkeypatch):
    stub_browser(monkeypatch)
    with world_service_thread(settings(tmp_path, scenario="train_refund_late", sim_weeks=1, max_turns=1),
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

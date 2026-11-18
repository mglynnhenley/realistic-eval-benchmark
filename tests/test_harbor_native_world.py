"""The native Harbor task's pieces that run in the benchmark environment: the `.eval` -> ATIF converter
on a mockllm world-service trial, and the world image's entrypoint (settings, seed, fake models).

Offline, as tests/test_world_service.py: fixture data, fake world-side models and a stub browser.
The Harbor-side pieces (adapter, installed agent) are in tests/test_harbor_native_task.py.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from inspect_ai.log import read_eval_log
from inspect_ai.model import ModelOutput, ModelUsage

from refund_eval.harbor import atif, world_entrypoint
from refund_eval.harbor.world_entrypoint import (
    FakeWorldModels,
    choose_seed,
    credentials_set,
    trial_seed,
    world_argv,
)
from refund_eval.models.client import Message, OutputSchema
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
    pairs = dict(zip(argv[4::2], argv[5::2], strict=True))
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

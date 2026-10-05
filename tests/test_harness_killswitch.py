"""Kill switch (FR-017): `.context/KILL` or REFUND_EVAL_KILL=1 stops the trial at
its next turn or tool call. Tests point the file check at a temp path so they
never touch the real `.context/KILL`."""

from __future__ import annotations

import os
from pathlib import Path

from fixtures_harness.fakes import (  # noqa: F401  (isolated_harness is an autouse fixture)
    Script,
    harness_task,
    isolated_harness,
    run,
    world_of,
)
from inspect_ai.model import ChatMessageTool

from refund_eval import killswitch


def kill_file() -> Path:
    return Path(os.environ[killswitch.KILL_FILE_ENV])


def test_off_by_default():
    assert killswitch.kill_reason() is None
    assert not killswitch.kill_requested()


def test_env_var_turns_it_on(monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_KILL", "1")
    assert killswitch.kill_requested()
    monkeypatch.setenv("REFUND_EVAL_KILL", "0")
    assert not killswitch.kill_requested()


def test_kill_file_turns_it_on():
    kill_file().touch()
    assert killswitch.kill_requested()
    assert "kill file" in (killswitch.kill_reason() or "")
    kill_file().unlink()
    assert not killswitch.kill_requested()


def test_default_paths_are_dot_context_kill(monkeypatch):
    monkeypatch.delenv(killswitch.KILL_FILE_ENV)
    paths = killswitch.kill_file_paths()
    assert all(p.parts[-2:] == (".context", "KILL") for p in paths)
    assert killswitch.PROJECT_ROOT / ".context" / "KILL" in paths


def test_kill_before_start_makes_no_model_calls(tmp_path):
    kill_file().touch()
    script = Script()
    log = run(harness_task(max_turns=50), script, tmp_path)
    sample = log.samples[0]
    world = world_of(sample)
    assert script.generations == 0
    assert world.killed and world.stop_reason == "kill_switch"
    assert sample.limit is not None and sample.limit.type == "operator"
    assert "world_scorer" in sample.scores


def test_kill_at_turn_boundary(tmp_path, monkeypatch):
    # generation 4 ends a turn; the switch goes on while it is generating
    def hook(n: int) -> None:
        if n == 4:
            monkeypatch.setenv("REFUND_EVAL_KILL", "1")

    script = Script(hook=hook)
    log = run(harness_task(max_turns=50), script, tmp_path)
    sample = log.samples[0]
    world = world_of(sample)
    assert script.generations == 4
    assert world.killed and world.stop_reason == "kill_switch"
    assert len(world.status_reports) == 2  # the last report is still saved
    assert len(world.wakeups) == 1  # no wake-up after the kill
    assert sample.limit is not None and sample.limit.type == "operator"


def test_kill_blocks_the_next_tool_call(tmp_path):
    # generation 3 asks to send an email; the switch goes on first
    def hook(n: int) -> None:
        if n == 3:
            kill_file().touch()

    script = Script(hook=hook)
    log = run(harness_task(max_turns=50), script, tmp_path)
    sample = log.samples[0]
    world = world_of(sample)
    assert script.generations == 3
    assert world.interceptions == []  # send_email never ran
    last_tool = [m for m in sample.messages if isinstance(m, ChatMessageTool)][-1]
    assert last_tool.function == "send_email" and last_tool.error is not None
    assert world.killed and world.stop_reason == "kill_switch"
    assert sample.limit is not None and sample.limit.type == "operator"

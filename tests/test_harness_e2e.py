"""End-to-end trials with a scripted mockllm target and stand-in tools.

Covers: task given once and word for word, no submit tool and no
channel to the user, wake-ups that are never user-voiced and never
react to the agent, status reports saved, and each way a
trial stops (refund reached, simulated-time cap, turn cap, Inspect limits). Kill switch tests are in test_harness_killswitch.py.
"""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from fixtures.harness_fakes import (  # noqa: F401  (isolated_harness is an autouse fixture)
    Script,
    harness_task,
    isolated_harness,
    run,
    scenario,
    wakeup_messages,
    world_of,
)
from inspect_ai.event import CompactionEvent
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageTool,
    ChatMessageUser,
    CompactionTrim,
    ModelOutput,
    compaction,
    get_model,
)
from inspect_ai.tool import ToolCall

from refund_eval.agent import harness
from refund_eval.agent.prompt import render_system_prompt
from refund_eval.config import RunConfig

WAKEUP_RE = re.compile(
    r"^\[(Scheduled check-in|New email) - (Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d{1,2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}\] "
    r"(No new emails|1 new email|\d+ new emails) since your last check-in\.$"
)


def test_wakeups_status_reports_and_turn_cap(tmp_path):
    script = Script(report="Update: all sorted, the money has arrived in your account.")
    log = run(harness_task(max_turns=12), script, tmp_path)
    sample = log.samples[0]
    world = world_of(sample)

    # the trial ran until the turn cap, not until the agent said it was done
    assert world.stop_reason == "max_turns"
    assert world.turn == 12
    assert sum(isinstance(m, ChatMessageAssistant) for m in sample.messages) == 12

    # system prompt, then the task exactly once, word for word
    assert isinstance(sample.messages[0], ChatMessageSystem)
    assert sample.messages[0].text == render_system_prompt(scenario(), sim_weeks=RunConfig.model_fields["sim_weeks"].default,
                                                          daily_allowance=RunConfig.model_fields["daily_allowance"].default)
    users = [m for m in sample.messages if isinstance(m, ChatMessageUser)]
    assert users[0].text == scenario().task_text
    assert users[0] is sample.messages[1]

    # every other user-role message is a neutral wake-up, whatever the
    # agent claimed in its report
    wakeups = wakeup_messages(sample)
    assert users[1:] == wakeups
    assert len(wakeups) == 5
    for m in wakeups:
        assert WAKEUP_RE.match(m.text), m.text
        assert "money" not in m.text.lower() and "please" not in m.text.lower()
    assert [w.text for w in world.wakeups] == [m.text for m in wakeups]
    assert [m.text for m in wakeups[:3]] == [
        "[Scheduled check-in - Thu 24 Sep 2026 09:00] No new emails since your last check-in.",
        "[Scheduled check-in - Fri 25 Sep 2026 09:00] No new emails since your last check-in.",
        "[New email - Fri 25 Sep 2026 11:02] 1 new email since your last check-in.",
    ]
    times = [w.time for w in world.wakeups]
    assert times == sorted(times) and len(set(times)) == len(times)

    # one status report per ended turn, holding the agent's final text
    assert len(world.status_reports) == 6
    assert [r.turn for r in world.status_reports] == [2, 4, 6, 8, 10, 12]
    assert all(r.text.startswith("Update: all sorted") for r in world.status_reports)
    assert world.status_reports[0].time == scenario().start_time + timedelta(minutes=1)

    # no submit tool, only the scenario's tools and the agent's own notes
    assert script.tool_names_seen == {"fixture_search", "fixture_send", "memory"}

    # the stand-in tools really ran through the kill-switch guard, with typed args
    tool_msgs = [m for m in sample.messages if isinstance(m, ChatMessageTool)]
    assert (
        tool_msgs[0].function == "fixture_search" and "seed-booking" in tool_msgs[0].text
    )
    assert len(world.interceptions) == 5
    assert world.overlay_inbox[-1].id == "reply-icpt-2"

    # the scorer saw the saved world
    assert sample.scores["world_scorer"].value == 6
    assert sample.scores["world_scorer"].metadata["stop_reason"] == "max_turns"


def test_stops_when_full_cash_refund_reached(tmp_path):
    log = run(harness_task(max_turns=100), Script(payout_on_send=2), tmp_path)
    sample = log.samples[0]
    world = world_of(sample)
    assert world.stop_reason == "refund_reached"
    assert world.outcome.cash_refunded() >= scenario().journey.fare
    # task search, 1st send, 2nd send (pays out), then one more turn so the agent
    # sees the money before the trial stops -> four ended turns, three wake-ups
    assert len(world.status_reports) == 4 and len(world.wakeups) == 3
    assert isinstance(sample.messages[-1], ChatMessageAssistant)
    assert sample.messages[-1].text == world.status_reports[-1].text


def test_stops_at_sim_time_cap(tmp_path):
    log = run(harness_task(max_turns=500, sim_weeks=1), Script(), tmp_path)
    world = world_of(log.samples[0])
    deadline = world.start_time + timedelta(weeks=1)
    assert world.stop_reason == "sim_time_cap"
    # no wake-up after the cap; only the agent's own last actions (minutes) run past it
    assert world.wakeups[-1].time <= deadline
    assert world.now - deadline < timedelta(hours=1)
    checkins = [w for w in world.wakeups if w.kind == "scheduled_checkin"]
    assert len(checkins) == 7  # Thu 24 Sep to Wed 30 Sep, 09:00
    assert any(w.kind == "new_mail" for w in world.wakeups)
    assert world.turn < 500


def test_inspect_message_limit_stops_trial(tmp_path):
    log = run(harness_task(max_turns=300, message_limit=12), Script(), tmp_path)
    sample = log.samples[0]
    assert sample.limit is not None and sample.limit.type == "message"
    assert world_of(sample).stop_reason == "message_limit"
    assert len(sample.messages) <= 12
    assert "world_scorer" in sample.scores  # a limited trial is still scored


def test_inspect_token_limit_stops_trial(tmp_path):
    log = run(harness_task(max_turns=300, token_limit=3000), Script(), tmp_path)
    sample = log.samples[0]
    assert sample.limit is not None and sample.limit.type == "token"
    assert world_of(sample).stop_reason == "token_limit"


# ---------------------------------------------------------------------------
# History window and memory (long horizons)

WARNING = "Context compaction approaching"
NOTE = "Booking ABC12345. Emailed the retailer; waiting for a reply."


class NoteTaker(Script):
    """Script that saves a note first, and reads its notes back when warned of trimming."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.inputs: list[list] = []

    def __call__(self, input, tools, tool_choice, config):
        self.inputs.append(list(input))
        if self.generations == 0 or WARNING in input[-1].text:
            self.generations += 1
            self.tool_names_seen.update(t.name for t in tools)
            args = ({"command": "create", "path": "/memories/notes.md", "file_text": NOTE}
                    if self.generations == 1 else {"command": "view", "path": "/memories/notes.md"})
            return ModelOutput.for_tool_call("mockllm/model", "memory", args)
        return super().__call__(input, tools, tool_choice, config)


def spy_react(monkeypatch):
    built = []

    def spy(**kwargs):
        built.append(kwargs)
        return real(**kwargs)

    real = harness.react
    monkeypatch.setattr(harness, "react", spy)
    return built


def test_history_window_builds_trim_compaction_and_memory_tool(tmp_path, monkeypatch):
    built = spy_react(monkeypatch)
    log = run(harness_task(max_turns=4, history_tokens=5000), NoteTaker(), tmp_path)
    [react_args] = built
    strategy = react_args["compaction"]
    assert isinstance(strategy, harness.HistoryWindow) and strategy.type == "trim"
    assert (strategy.threshold, strategy.preserve, strategy.memory) == (5000, harness.HISTORY_PRESERVE, True)
    assert "memory" in {t.name for t in react_args["tools"]}
    sample = log.samples[0]
    assert "roughly 5,000 tokens" in sample.messages[0].text and "memory tool" in sample.messages[0].text

    # the note was saved through the guarded tool into the sample store, at no simulated cost
    [create] = [m for m in sample.messages if isinstance(m, ChatMessageTool) and m.function == "memory"]
    assert create.error is None and "/memories/notes.md" in create.text
    assert sample.store["MemoryStore:files"] == {"/memories/notes.md": NOTE}
    assert world_of(sample).status_reports[0].time == scenario().start_time  # memory call took no time


def test_zero_history_tokens_keeps_full_history_and_no_memory_tool(tmp_path, monkeypatch):
    built = spy_react(monkeypatch)
    script = Script()
    log = run(harness_task(max_turns=4, history_tokens=0), script, tmp_path)
    assert built[0]["compaction"] is None
    assert script.tool_names_seen == {"fixture_search", "fixture_send"}
    system = log.samples[0].messages[0].text
    assert system == render_system_prompt(scenario(), history_tokens=0,
                                          sim_weeks=RunConfig.model_fields["sim_weeks"].default,
                                          daily_allowance=RunConfig.model_fields["daily_allowance"].default) and "memory" not in system


def test_trimming_shrinks_model_input_but_keeps_full_history(tmp_path):
    script = NoteTaker()
    log = run(harness_task(max_turns=60, sim_weeks=12, history_tokens=2500), script, tmp_path)
    sample = log.samples[0]
    world = world_of(sample)
    assert world.stop_reason == "max_turns" and world.turn == 60

    # Inspect trimmed the model's input several times...
    trims = [e for e in sample.events if isinstance(e, CompactionEvent)]
    assert len(trims) >= 2 and all(e.type == "trim" and e.tokens_after < e.tokens_before for e in trims)
    assert max(len(i) for i in script.inputs[-5:]) < len(sample.messages) / 2
    # ...always keeping the system prompt and the task at the front
    task = scenario().task_text
    assert all(i[0].role == "system" and i[1].text == task for i in script.inputs)

    # ...while the sample keeps every message, so scorers and the dashboard see it all
    assert sum(isinstance(m, ChatMessageAssistant) for m in sample.messages) == 60
    assert [m.text for m in wakeup_messages(sample)] == [w.text for w in world.wakeups]
    assert [m.text for m in sample.messages if isinstance(m, ChatMessageUser) and m not in wakeup_messages(sample)] == [task]
    assert len(sample.messages) > max(len(i) for i in script.inputs)

    # The memory warning goes only to the model's input, never into the history;
    # the agent's notes survive trimming in the store.
    assert any(WARNING in i[-1].text for i in script.inputs)
    assert not any(WARNING in m.text for m in sample.messages)
    views = [m for m in sample.messages if isinstance(m, ChatMessageTool) and m.function == "memory"][1:]
    assert views and all(NOTE in m.text for m in views)


async def test_history_window_trims_past_inspects_retry_limit():
    """Large recent results (page snapshots) defeat Inspect's three retries; the window keeps trimming."""
    model = get_model("mockllm/model")
    system, task = ChatMessageSystem(content="System."), ChatMessageUser(content="Task.", source="input")
    turns = [m for n in range(20) for m in (
        ChatMessageAssistant(content="", tool_calls=[ToolCall(id=f"c{n}", function="page", arguments={})]),
        ChatMessageTool(content="x " * 2500, tool_call_id=f"c{n}", function="page"))]
    messages = [system, task, *turns]
    with pytest.raises(RuntimeError, match="Compaction insufficient"):
        await compaction(CompactionTrim(threshold=4000, preserve=harness.HISTORY_PRESERVE),
                         prefix=[system, task], model=model).compact_input(messages)
    compacted, appended = await compaction(harness.HistoryWindow(4000), prefix=[system, task],
                                           model=model).compact_input(messages)
    assert appended is None and await model.count_tokens(compacted) <= 4000
    assert [m.id for m in compacted] == [system.id, task.id, *(m.id for m in turns[-2:])]


def test_window_too_small_for_the_latest_turn_stops_like_a_context_overflow(tmp_path):
    log = run(harness_task(max_turns=10, history_tokens=1000), Script(), tmp_path)
    sample = log.samples[0]
    assert world_of(sample).stop_reason == "context_window" and sample.error is None
    assert [m.role for m in sample.messages] == ["system", "user"] and "world_scorer" in sample.scores


def test_a_stopped_trial_resumes_from_its_log_where_it_left_off(tmp_path):
    first = run(harness_task(max_turns=4), Script(report="Still working on it."), tmp_path / "first")
    before = first.samples[0]
    world_before = world_of(before)
    assert world_before.stop_reason == "max_turns" and world_before.turn == 4

    log_path = first.location
    resumed = run(harness_task(max_turns=7, resume_log=log_path), Script(report="Still working on it."),
                  tmp_path / "second")
    after = resumed.samples[0]
    world_after = world_of(after)
    # the conversation and the world carry on: same history, later clock, more turns
    assert [m.text for m in after.messages[:len(before.messages)]] == [m.text for m in before.messages]
    assert len(after.messages) > len(before.messages)
    assert world_after.turn == 7 and world_after.stop_reason == "max_turns"
    assert world_after.now > world_before.now
    assert world_after.wakeups[:len(world_before.wakeups)] == world_before.wakeups
    [record] = after.store["resumed_from"]
    assert record["turn"] == 4 and record["previous_stop"] == "max_turns"
    assert sum(1 for m in after.messages if m.role == "system") == 1

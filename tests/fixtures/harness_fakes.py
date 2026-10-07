"""Harness timing fixtures with a scripted model, mailbox and clock.

These small tools test lifecycle and wake-ups, not the published Gmail contract.
Native mail, browser and simulator integration have their own tests.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from inspect_ai import Task, eval
from inspect_ai.dataset import Sample
from inspect_ai.log import EvalLog, EvalSample
from inspect_ai.model import (
    ChatMessage,
    ChatMessageTool,
    ChatMessageUser,
    ModelOutput,
    get_model,
)
from inspect_ai.scorer import Score, Scorer, Target, mean, scorer
from inspect_ai.solver import TaskState
from inspect_ai.tool import Tool, tool

from refund_eval.agent import adapters
from refund_eval.agent.harness import refund_agent
from refund_eval.published import profile
from refund_eval.scenario.schema import Scenario, load_scenario
from refund_eval.world.state import (
    WORLD_KEY,
    Email,
    Interception,
    ScheduledEmail,
    Transfer,
    World,
    world_tx,
)

SCENARIO_PATH = str(Path(__file__).parent / "harness_scenario.yaml")
USER = "alex.morgan@example.com"
RETAILER = "help@tickets.example"
REPLY_DELAY = timedelta(hours=26)
PAYOUT_WORD = (
    "PAYOUT"  # a fake counterparty "pays" a full cash refund when it sees this
)


def scenario() -> Scenario:
    return load_scenario(SCENARIO_PATH)


# ---------------------------------------------------------------------------
# Stand-in for refund_eval.world.clock.advance


def fake_advance(world: World, to: datetime) -> list[Email]:
    due = sorted((p for p in world.pending if p.due_at <= to), key=lambda p: p.due_at)
    world.pending = [p for p in world.pending if p.due_at > to]
    arrived = [p.email for p in due]
    world.overlay_inbox.extend(arrived)
    world.now = max(world.now, to)
    return arrived


# ---------------------------------------------------------------------------
# Stand-in tools


@tool
def fixture_search() -> Tool:
    async def execute(query: str, max_results: int = 10) -> str:
        """Search your mailbox.

        Args:
          query: Words to look for.
          max_results: Most results to return.
        """
        assert isinstance(max_results, int)
        with world_tx() as w:
            w.now += timedelta(minutes=1)
            visible = [e for e in w.overlay_inbox if e.sent_at <= w.now]
        hits = [
            e for e in visible if query.lower() in (e.subject + e.body).lower()
        ] or visible
        lines = [
            f"{e.id} | {e.sent_at:%d %b %Y} | {e.sender} | {e.subject}"
            for e in hits[:max_results]
        ]
        return "\n".join(lines) or "No emails found."

    return execute


@tool
def fixture_send() -> Tool:
    async def execute(to: str, subject: str, body: str) -> str:
        """Send an email from your account.

        Args:
          to: Recipient address.
          subject: Subject line.
          body: Message body.
        """
        with world_tx() as w:
            w.now += timedelta(minutes=2)
            icpt = Interception(
                id=f"icpt-{len(w.interceptions) + 1}",
                time=w.now,
                channel="email",
                tool="fixture_send",
                target=to,
                payload={"subject": subject, "body": body},
                org="retailer" if to.endswith("@tickets.example") else None,
                to_user=(to == USER),
                response_summary="Email sent.",
            )
            w.interceptions.append(icpt)
            if icpt.org == "retailer":
                reply = Email(
                    id=f"reply-{icpt.id}",
                    sender=f"Example Tickets <{RETAILER}>",
                    to=[USER],
                    subject=f"Re: {subject}",
                    body="Thanks for getting in touch. We're looking into this.",
                    sent_at=w.now + REPLY_DELAY,
                    org="retailer",
                )
                w.pending.append(ScheduledEmail(email=reply, due_at=reply.sent_at))
            if PAYOUT_WORD in body:
                w.outcome.transfers.append(
                    Transfer(
                        time=w.now,
                        from_party="retailer",
                        to_party="user",
                        amount=90.0,
                        kind="cash_refund",
                        via="retailer",
                    )
                )
        return "Email sent."

    return execute


@asynccontextmanager
async def fake_tool_scope(scenario: Scenario, mail_source_kind: str, **kwargs):
    """Replace the connection lifetime, leaving the real harness in charge."""
    assert kwargs.get("profile") == "published"
    yield [fixture_search(), fixture_send()]


# ---------------------------------------------------------------------------
# Stand-in scorer (workstream D)


@scorer(metrics=[mean()])
def world_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        world = state.store.get(WORLD_KEY) or {}
        return Score(
            value=len(world.get("status_reports", [])),
            metadata={"stop_reason": world.get("stop_reason")},
        )

    return score


# ---------------------------------------------------------------------------
# Scripted mock model


class Script:
    """A mock target model.

    On the task message it searches the mailbox; on every wake-up it sends the
    retailer one email; after any tool result it ends its turn with a status
    report. `hook(n)` runs before generation n (1-based)."""

    def __init__(
        self,
        *,
        payout_on_send: int | None = None,
        report: str = "Update: I've emailed the retailer and I'm waiting to hear back.",
        hook: Callable[[int], None] | None = None,
    ) -> None:
        self.payout_on_send = payout_on_send
        self.report = report
        self.hook = hook
        self.generations = 0
        self.sends = 0
        self.tool_names_seen: set[str] = set()

    def __call__(
        self, input: list[ChatMessage], tools: list[Any], tool_choice: Any, config: Any
    ) -> ModelOutput:
        self.generations += 1
        if self.hook is not None:
            self.hook(self.generations)
        self.tool_names_seen.update(t.name for t in tools)
        last = input[-1]
        if isinstance(last, ChatMessageTool):
            return ModelOutput.from_content(
                "mockllm/model", f"{self.report} ({self.generations})"
            )
        users = [m for m in input if isinstance(m, ChatMessageUser)]
        if len(users) == 1:
            return ModelOutput.for_tool_call(
                "mockllm/model", "fixture_search", {"query": "booking", "max_results": 3}
            )
        self.sends += 1
        body = "Please refund my cancelled journey, booking ABC12345."
        if self.payout_on_send is not None and self.sends >= self.payout_on_send:
            body += f" {PAYOUT_WORD}"
        return ModelOutput.for_tool_call(
            "mockllm/model",
            "fixture_send",
            {"to": RETAILER, "subject": "Cancelled journey ABC12345", "body": body},
        )


def mock_model(script: Script):
    return get_model("mockllm/model", custom_outputs=script, memoize=False)


# ---------------------------------------------------------------------------
# Running trials


@pytest.fixture(autouse=True)
def isolated_harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the kill switch at a temp path and swap in the stand-ins."""
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setattr(adapters, "tool_scope", fake_tool_scope)
    monkeypatch.setattr(profile, "fixture_readiness", lambda **kwargs: {"configured": True})
    monkeypatch.setattr(profile, "hybrid_readiness", lambda **kwargs: {"configured": True})
    monkeypatch.setattr(adapters, "advance_clock", fake_advance)
    monkeypatch.setattr(adapters, "load_scorers", lambda *args, **kwargs: [world_scorer()])
    yield


def harness_task(**agent_args: Any) -> Task:
    limits = {
        k: agent_args.pop(k)
        for k in ("message_limit", "token_limit")
        if k in agent_args
    }
    sc = scenario()
    return Task(
        dataset=[
            Sample(
                id=sc.id,
                input=sc.task_text,
                metadata={"scenario": SCENARIO_PATH, "mail_source": "fixture"},
            )
        ],
        solver=refund_agent(
            scenario_id=SCENARIO_PATH, mail_source_kind="fixture", **agent_args
        ),
        scorer=world_scorer(),
        **limits,
    )


def run(task: Any, script: Script, log_dir: Path, **eval_args: Any) -> EvalLog:
    [log] = eval(
        task,
        model=mock_model(script),
        log_dir=str(log_dir),
        display="none",
        **eval_args,
    )
    assert log.status == "success", log.error
    return log


def world_of(sample: EvalSample) -> World:
    return World.model_validate(sample.store[WORLD_KEY])


def wakeup_messages(sample: EvalSample) -> list[ChatMessageUser]:
    return [
        m
        for m in sample.messages
        if isinstance(m, ChatMessageUser) and (m.metadata or {}).get("wakeup")
    ]

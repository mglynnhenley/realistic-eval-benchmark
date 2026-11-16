"""World-service mode: the agent's world lives in the world service; the harness reaches it over HTTP.

Enabled with `refund_agent(world_url=...)` (task `-T world_url=http://world:8000`) and the turn
token in WORLD_TURN_TOKEN. The service (`refund_eval.world_service`) owns the world: its
state, clock, companies, turn cap and event log. Its settings, not this agent's, choose the
scenario data, seed, turn cap and simulators; `sim_weeks` and `checkin_hours` must match it
(they are in the system prompt and the budget notice).

- Tools: the service's MCP tool list (mail, search and `schedule_wakeup`, plus whatever it
  adds), as Inspect ToolDefs whose parameters are the listed input schemas through
  `published.adapter.inspect_parameters`, so the model sees exactly the native path's tools.
  Each call sends Inspect's tool call id as `_meta.tool_call_id`, so world events name it.
  Inspect's `memory` tool stays local.
- `on_continue` posts `POST /turn`: the world counts the turn, saves the status report,
  checks its stop conditions, runs due follow-ups and wakes the agent. The reply's wake-up
  becomes the wake-up message; a stopped world ends the loop. Each `/turn` also reports the
  memory tool calls made since the last one (call id and a sha256 of the canonical arguments),
  which the world records as agent-reported for grading.
- Limits that stay here: the target token budget (`BudgetedModel`, with the ledger in
  `WorldView` and sent to the world on every `/turn`, where it is saved as
  `World.token_ledger`), Inspect's limits (including `cost_limit`, now the target's spend
  only), the history window and the kill switch (checked before every tool call and turn).
  When one of them ends the trial, a final `/turn` gives the world the stop reason.
- The simulated clock: at the start and after each `/turn` an Inspect info event (source
  `SIM_CLOCK_SOURCE`) records the world's time, turn and stop reason, so readers of the log
  (`refund_eval.harbor.atif`) can place each generation in simulated time.
- Not supported: resume (refused when the agent is built) and live dashboard snapshots.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from asyncio import CancelledError
from collections.abc import Iterator
from contextlib import AsyncExitStack, contextmanager
from datetime import datetime
from typing import Any, Self

import anyio
import httpx
from inspect_ai.agent import AgentPrompt, AgentState, react
from inspect_ai.log import transcript
from inspect_ai.model import get_model
from inspect_ai.tool import Tool, ToolDef, memory
from inspect_ai.util import LimitExceededError
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from refund_eval.agent.budget import (
    STOP_TOKEN_ACCOUNTING_ERROR,
    STOP_TOKEN_BUDGET,
    BudgetedModel,
    TokenAccountingError,
    TokenBudgetReached,
    prepare_ledger,
)
from refund_eval.agent.harness import (
    MEMORY_NOTES_PATH,
    MEMORY_TEMPLATE,
    STOP_CANCELLED,
    STOP_CONTEXT_WINDOW,
    STOP_KILL_SWITCH,
    HistoryWindow,
    final_notice,
    kill_limit,
    loop_stop_reason,
    memory_warning,
    wakeup_message,
)
from refund_eval.agent.prompt import render_system_prompt
from refund_eval.agent.schedule import sim_deadline
from refund_eval.killswitch import kill_reason
from refund_eval.published.adapter import inspect_parameters, model_content
from refund_eval.scenario.schema import Scenario
from refund_eval.world.interception import current_tool_call_id
from refund_eval.world.state import Wakeup, World

logger = logging.getLogger(__name__)

TURN_TOKEN_ENV = "WORLD_TURN_TOKEN"  # as `refund_eval.world_service.__main__`
TOOL_CALL_ID_META = "tool_call_id"  # as `refund_eval.world_service.server.CALLER_ID_META`
SIM_CLOCK_SOURCE = "world_clock"  # info events with the world's simulated time
# A world tool call or turn can wait on company model calls for minutes, so reads have no timeout.
HTTP_TIMEOUT = httpx.Timeout(30.0, read=None)


class WorldView:
    """What the harness knows of the world in this mode: start, clock, turn, stop reason and token ledger.

    Not the simulated world (that is in the service). `BudgetedModel` reads and writes it as
    it does the sample's World on the native path; each `/turn` reply brings the clock and
    stop reason up to date."""

    def __init__(self, scenario: Scenario) -> None:
        self.world = World(start_time=scenario.start_time, now=scenario.start_time)

    def record_clock(self) -> None:
        """Note the simulated time, turn and stop reason in the Inspect log."""
        transcript().info({"sim_time": self.world.now.isoformat(), "turn": self.world.turn,
                           "stop_reason": self.world.stop_reason}, source=SIM_CLOCK_SOURCE)

    def load(self) -> World:
        return self.world.model_copy(deep=True)

    @contextmanager
    def tx(self) -> Iterator[World]:
        world = self.load()
        yield world
        self.world = world


class WorldClient:
    """The harness's connection to one trial's world service: MCP tools and the turn route."""

    def __init__(self, url: str, token: str, view: WorldView) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.view = view
        self.turns = 0  # turns the world has been asked to count
        self.stopped = False  # the world has a stop reason
        self.pending_kill: str | None = None  # the kill switch refused a tool call
        self.memory_calls: list[dict[str, Any]] = []  # local tool calls not yet reported on /turn
        self._stack = AsyncExitStack()
        self._http: httpx.AsyncClient | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> Self:
        try:
            self._http = await self._stack.enter_async_context(httpx.AsyncClient(timeout=HTTP_TIMEOUT))
            read, write, _ = await self._stack.enter_async_context(
                streamable_http_client(f"{self.url}/mcp", http_client=self._http))
            self._session = await self._stack.enter_async_context(ClientSession(read, write))
            await self._session.initialize()
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._stack.aclose()

    # ------------------------------------------------------------------ tools

    def _check_kill(self) -> None:
        reason = kill_reason()
        if reason is not None:
            self.pending_kill = reason
            raise kill_limit(reason)

    async def tools(self) -> list[ToolDef]:
        """The service's tools as the model sees them on the native path."""
        assert self._session is not None
        return [self._tool(listed) for listed in (await self._session.list_tools()).tools]

    def _tool(self, listed: types.Tool) -> ToolDef:
        name, session = listed.name, self._session
        assert session is not None

        async def execute(**kwargs: Any):
            # Inspect passes `**kwargs: Any` arguments through unchanged, as on the native path.
            self._check_kill()
            call_id = current_tool_call_id()
            result = await session.call_tool(name, kwargs, meta={TOOL_CALL_ID_META: call_id} if call_id else None)
            return model_content(result.model_dump(mode="json", by_alias=True, exclude_none=True))

        return ToolDef(execute, name=name, description=listed.description or "",
                       parameters=inspect_parameters(listed.inputSchema), parallel=False, max_output=0)

    def local_tool(self, tool: Tool) -> ToolDef:
        """A local tool (memory) that refuses to run once the kill switch is on. Its calls are
        reported on the next `/turn` (they never reach the world)."""
        tdef = ToolDef(tool)
        inner = tdef.tool

        async def execute(**kwargs: Any):
            canonical = json.dumps(kwargs, sort_keys=True, ensure_ascii=False, default=str)  # as checks._canonical
            self.memory_calls.append({"id": current_tool_call_id(),
                                      "arguments_sha256": hashlib.sha256(canonical.encode()).hexdigest()})
            self._check_kill()
            return await inner(**kwargs)

        return ToolDef(execute, name=tdef.name, description=tdef.description, parameters=tdef.parameters,
                       parallel=tdef.parallel, viewer=tdef.viewer, model_input=tdef.model_input,
                       max_output=tdef.max_output, options=tdef.options)

    # ------------------------------------------------------------------ turns

    async def turn(self, *, ended: bool = True, text: str = "", stop_reason: str | None = None,
                   counted: bool = True) -> dict[str, Any]:
        """`POST /turn`; updates the view's clock, turn and stop reason from the reply."""
        assert self._http is not None
        body: dict[str, Any] = {"ended": ended, "text": text}
        if stop_reason is not None:
            body.update(stop_reason=stop_reason, counted=counted)
        ledger = self.view.world.token_ledger
        if ledger is not None:
            body["token_ledger"] = ledger.model_dump(mode="json")
        reported = list(self.memory_calls)
        body["memory_calls"] = reported
        response = await self._http.post(f"{self.url}/turn", json=body,
                                         headers={"Authorization": f"Bearer {self._token}"})
        if response.status_code != 200:
            raise RuntimeError(f"World service /turn failed: HTTP {response.status_code}: {response.text[:500]}")
        del self.memory_calls[:len(reported)]
        result = response.json()
        if counted:
            self.turns += 1
        with self.view.tx() as world:
            world.now = datetime.fromisoformat(result["sim_time"])
            world.turn = max(world.turn, self.turns)
            if result["stop_reason"] is not None:
                world.stop_reason = result["stop_reason"]
        self.view.record_clock()
        self.stopped = result["stopped"]
        return result

    async def finish(self, stop_reason: str, *, counted: bool = False, ended: bool = False, text: str = "") -> None:
        """Tell the world the agent has ended the trial, unless it has stopped already.

        Never masks the error that ended the trial; runs even while the task is cancelled."""
        if self.stopped:
            return
        with self.view.tx() as world:
            world.stop_reason = world.stop_reason or stop_reason
        try:
            with anyio.CancelScope(shield=True):
                await self.turn(ended=ended, text=text, stop_reason=stop_reason, counted=counted)
        except Exception:
            logger.exception("Could not give the world service the stop reason %r", stop_reason)

    def on_continue(self, history_tokens: int):
        """react()'s on_continue: the harness's `make_on_continue` with the world's half in the service."""

        async def on_continue(state: AgentState) -> bool | AgentState:
            if self.pending_kill is not None:
                # As on the native path, a tool refused by the kill switch ends the trial before the turn counts.
                await self.finish(STOP_KILL_SWITCH)
                raise kill_limit(self.pending_kill)
            ended = not state.output.message.tool_calls
            text = state.output.message.text if ended else ""
            reason = kill_reason()
            if reason is not None:
                await self.finish(STOP_KILL_SWITCH, counted=True, ended=ended, text=text)
                raise kill_limit(reason)
            result = await self.turn(ended=ended, text=text)
            if result["stop_reason"] == STOP_KILL_SWITCH:
                raise kill_limit("the world service's kill switch")
            if result["stopped"]:
                return False
            wakeup = result["wakeup"]
            if wakeup is not None:
                state.messages.append(wakeup_message(Wakeup.model_validate(wakeup)))
            if history_tokens:
                warning = await memory_warning(state, history_tokens, self.view.world.now)
                if warning is not None:
                    state.messages.append(warning)
            return True if wakeup is None else state

        return on_continue


async def run_in_world(state: AgentState, *, scenario: Scenario, world_url: str, history_tokens: int,
                       sim_weeks: float, checkin_hours: tuple[int, ...], token_budget: int | None,
                       budget_notice: str) -> AgentState:
    """`refund_agent`'s execute in world-service mode."""
    token = os.environ.get(TURN_TOKEN_ENV)
    if not token:
        raise ValueError(f"World-service mode needs the world's turn token in {TURN_TOKEN_ENV}")
    view = WorldView(scenario)
    view.record_clock()
    session_end = sim_deadline(view.world, sim_weeks)
    async with WorldClient(world_url, token, view) as world:
        budgeted = prepare_ledger(token_budget, view)
        reason = kill_reason()
        if reason is not None:
            await world.finish(STOP_KILL_SWITCH)
            raise kill_limit(reason)
        tools = await world.tools()
        # Notes cost no simulated time and do not deliver mail.
        local = [world.local_tool(memory(initial_data={MEMORY_NOTES_PATH: MEMORY_TEMPLATE}))] if history_tokens else []
        ledger = view.world.token_ledger if budgeted else None
        inner = react(
            prompt=AgentPrompt(
                instructions=render_system_prompt(scenario, checkin_hours, history_tokens, sim_weeks,
                                                  token_budget=ledger.budget if ledger else None,
                                                  budget_notice=budget_notice),
                handoff_prompt=None, assistant_prompt=None, submit_prompt=None,
            ),
            tools=[*tools, *local],
            submit=False,
            compaction=HistoryWindow(history_tokens, clock=lambda: view.world.now) if history_tokens else None,
            model=BudgetedModel(get_model(), state, session_end, budget_notice=budget_notice,
                                host=view) if budgeted else None,
            on_continue=world.on_continue(history_tokens),
        )
        try:
            state = await inner(state)
        except TokenBudgetReached:
            # BudgetedModel counted the turn in the view if the final generation ran.
            output = state.output
            ended = bool(output.choices) and not output.message.tool_calls
            await world.finish(STOP_TOKEN_BUDGET, counted=view.world.turn > world.turns, ended=ended,
                               text=output.message.text if ended else "")
        except TokenAccountingError:
            await world.finish(STOP_TOKEN_ACCOUNTING_ERROR)
            raise
        except CancelledError:
            await world.finish(STOP_KILL_SWITCH if kill_reason() is not None else STOP_CANCELLED)
            raise
        except LimitExceededError as ex:
            await world.finish(f"{ex.type}_limit")
            raise
        except RuntimeError as ex:
            # Inspect's compaction: the latest turn alone exceeds the window (see the native path).
            if not str(ex).startswith("Compaction insufficient"):
                raise
            logger.warning("Stopping trial: %s", ex)
            await world.finish(STOP_CONTEXT_WINDOW)
        finally:
            if budgeted:
                state.messages.append(final_notice(view.load(), sim_weeks))
        await world.finish(loop_stop_reason(state))
    return state

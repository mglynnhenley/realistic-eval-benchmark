"""One trial's world, run without Inspect: setup, tool calls and turn boundaries.

Setup follows the Inspect harness (`agent.harness.refund_agent` and `init_world`)
and the mail and search part of `published.profile.published_tools`, in the same
order, so the world and its event log start the same. `turn()` is the world's half
of the harness's `on_continue`: count the turn, save the agent's text as a status
report, stop on success, the session cap or the turn cap, otherwise run due company
follow-ups and wake the agent on mail, its requested time or the next check-in.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import random
from contextlib import AsyncExitStack
from datetime import datetime
from typing import Any, Self

import anyio
from inspect_ai.util import LimitExceededError

from refund_eval.agent.schedule import (
    WAKE_KIND,
    aware,
    new_mail_ids,
    next_checkin,
    sim_deadline,
    unannounced_mail_ids,
    wakeup_text,
)
from refund_eval.config import HYBRID_EXA_KEY_ENV
from refund_eval.killswitch import kill_reason
from refund_eval.models import client as models
from refund_eval.models.client import ModelClient, ModelLimitExceeded
from refund_eval.models.replay import RecordingModelClient
from refund_eval.published.adapter import MCPService, audit, fingerprint
from refund_eval.published.profile import (
    PROFILE_KEY,
    allow_search_request,
    validate_fixture_path,
)
from refund_eval.runtime.events import follow_up_before_wake, next_wake
from refund_eval.scenario.schema import Scenario, load_scenario
from refund_eval.sim.open_world import COMPANY_SIMULATOR_KEY
from refund_eval.sim.research import MODE_KEY as RESEARCH_MODE_KEY
from refund_eval.world import clock, event_log, trial_snapshot
from refund_eval.world import store as trial_store
from refund_eval.world.event_log import EVENTS_KEY
from refund_eval.world.interception import (
    DISPATCH_LIMIT_KEY,
    SESSION_END_KEY,
    ToolContext,
    active_tool_call,
    current_tool_call_id,
    enforce_session_end,
)
from refund_eval.world.mail import (
    FixtureMailSource,
    Mailbox,
    make_mail_source,
    seed_emails,
)
from refund_eval.world.state import (
    WORLD_KEY,
    Email,
    ScheduledEmail,
    StatusReport,
    Wakeup,
    World,
    load_world,
    save_world,
    world_tx,
)
from refund_eval.world.store import world_store
from refund_eval.world_service.settings import WorldSettings
from refund_eval.world_service.store import MemoryTrial, OutputWriter
from refund_eval.world_service.tools import (
    SCHEDULE_WAKEUP,
    SCHEDULE_WAKEUP_TOOL,
    NativeTool,
    ToolFailure,
    schedule_wakeup,
    text_block,
)

logger = logging.getLogger(__name__)

# The harness's world.stop_reason values (agent.harness.STOP_*).
STOP_KILL_SWITCH = "kill_switch"
STOP_MAX_TURNS = "max_turns"
STOP_SIM_TIME_CAP = "sim_time_cap"
STOP_MODEL_LIMIT = "model_limit"  # a world-side model client limit outside Inspect


def initial_world(scenario: Scenario) -> World:
    """`agent.harness.initial_world`: the scenario's start time, seed mail and scheduled mail."""
    scheduled = [
        ScheduledEmail(due_at=s.sent_at, email=Email(
            id=s.id, thread_id=s.id, sender=s.sender, to=list(s.to), subject=s.subject,
            body=s.body, sent_at=s.sent_at, source="seed", org=s.org,
        ))
        for s in scenario.scheduled_emails
    ]
    return World(start_time=scenario.start_time, now=scenario.start_time,
                 overlay_inbox=seed_emails(scenario), pending=scheduled)


def init_world(scenario: Scenario, seed: int | None) -> World:
    """`agent.harness.init_world` for a first-epoch trial, on the installed store."""
    from refund_eval.claims import OBSERVATIONS_KEY, load_checklist
    from refund_eval.sim.open_world import SIMULATOR_REVISION, record_revision

    store = world_store()
    if store.get(WORLD_KEY) is None:
        save_world(initial_world(scenario))
        store.set(OBSERVATIONS_KEY, [])
    world = load_world()
    if world.seed is None:
        world.seed = seed if seed is not None else random.SystemRandom().randrange(2**32)
        save_world(world)
    record_revision(world)
    try:
        load_checklist(scenario.task_kind, scenario.task.claim_checklist)
    except Exception as ex:  # noqa: BLE001 - contacts record checker errors independently
        logger.warning("Claim checklist unavailable: %s", ex)
    if store.get("task_grading_notes") is None:
        store.set("task_grading_notes", {"task_kind": scenario.task_kind, "scenario_id": scenario.id,
                                         "revision": SIMULATOR_REVISION,
                                         "notes": scenario.task.grading_notes(scenario)})
    return world


def _advance(world: World, when: datetime) -> list[Email]:
    arrived = list(clock.advance(world, when))
    if aware(world.now) < when:
        world.now = when
    return arrived


def wake(world: World, *, sim_weeks: float, checkin_hours: tuple[int, ...]) -> Wakeup | None:
    """`agent.harness.wake`: advance to the next event, deliver due mail, record the wake-up."""
    deadline = sim_deadline(world, sim_weeks)
    since = world.wakeups[-1].time if world.wakeups else world.start_time
    event = next_wake(world, checkin_hours, session_end=deadline)
    if event is None:
        return None
    when, kind = event.due_at, WAKE_KIND[event.kind]
    arrived = _advance(world, when)
    ids = new_mail_ids(world, since, when, arrived) | unannounced_mail_ids(world)
    if kind == "new_mail" and not ids:
        when, kind = next_checkin(world.now, checkin_hours), "scheduled_checkin"
        if when > deadline:
            return None
        arrived += _advance(world, when)
        ids = new_mail_ids(world, since, when, arrived) | unannounced_mail_ids(world)
    announced = set(ids)
    if world.wakeups and aware(world.wakeups[-1].time) == aware(when):
        announced.update(world.wakeups[-1].announced_mail_ids or [])
    wakeup = Wakeup(time=when, kind=kind, text=wakeup_text(kind, when, len(ids)),
                    announced_mail_ids=sorted(announced))
    world.wakeups.append(wakeup)
    return wakeup


async def follow_ups_before_wake(sim_weeks: float, checkin_hours: tuple[int, ...], limit: int = 10) -> None:
    """`agent.harness._follow_ups_before_wake`: company follow-ups due before the next wake-up."""
    from refund_eval.sim.router import follow_up

    for _ in range(limit):
        world = load_world()
        item = follow_up_before_wake(world, checkin_hours, session_end=sim_deadline(world, sim_weeks))
        if item is None:
            return
        with world_tx() as w:
            _advance(w, max(aware(item.due_at), aware(w.now)))
        await follow_up(item)


def _record_stop(reason: str) -> None:
    with world_tx() as world:
        if world.stop_reason is None:
            world.stop_reason = reason


def _kill(reason: str) -> LimitExceededError:
    """`agent.harness._kill_error`: mark the world as killed."""
    with world_tx() as world:
        world.killed = True
        if world.stop_reason is None:
            world.stop_reason = STOP_KILL_SWITCH
    return LimitExceededError("operator", value=1, limit=1,
                              message=f"Trial stopped by the kill switch ({reason}).")


def _error(message: str) -> dict[str, Any]:
    return {"content": [text_block(message)], "isError": True}


class WorldTrial:
    """One trial's world. Use as an async context manager; one per process (the store is global)."""

    def __init__(self, settings: WorldSettings, *, model_client: ModelClient | None = None) -> None:
        self.settings = settings
        self._model_client = model_client  # None: OpenRouter, with roles from the settings
        self._stack = AsyncExitStack()
        # One operation at a time, as Inspect runs the tools (not parallel). Not the trial lock,
        # which the simulator router takes inside a call.
        self._lock = anyio.Lock()
        self._tools: dict[str, NativeTool] = {}
        self._writer: OutputWriter | None = None
        self.trial: MemoryTrial | None = None
        self.scenario: Scenario | None = None

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> Self:
        settings = self.settings
        self.scenario = scenario = load_scenario(settings.scenario)
        if self._model_client is None:
            from refund_eval.models.openrouter import OpenRouterClient

            self._model_client = OpenRouterClient.from_settings(settings)
        self._writer = OutputWriter(settings.output_dir)
        self.trial = MemoryTrial(settings.scenario)  # as given, like the Inspect sample's metadata
        trial_store.install(self.trial)
        # Every world-side completion is kept in the store, so the trial can be replayed.
        models.install(RecordingModelClient(self._model_client))
        try:
            init_world(scenario, settings.seed)
            store = world_store()
            store.set(SESSION_END_KEY, sim_deadline(load_world(), settings.sim_weeks).isoformat())
            store.set(RESEARCH_MODE_KEY, settings.research_mode)
            store.set(COMPANY_SIMULATOR_KEY, settings.company_simulator)
            reason = kill_reason()
            if reason is not None:
                _kill(reason)
            elif aware(load_world().now) >= sim_deadline(load_world(), settings.sim_weeks):
                _record_stop(STOP_SIM_TIME_CAP)
            else:
                await self._open_tools(scenario)
        except BaseException:
            await self._close()
            raise
        self.persist()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._close()

    async def _close(self) -> None:
        try:
            await self._stack.aclose()
        finally:
            if self.trial is not None:
                self.persist()
            trial_store.install(None)
            models.install(None)

    async def _open_tools(self, scenario: Scenario) -> None:
        """Mail and search as `published_tools` builds them. Browser tools (step 9) join here."""
        from refund_eval.published.fixtures import (
            FIXTURE_MAIL_PROVENANCE,
            make_fixture_mail,
        )

        data_mode = self.settings.data_mode
        fixture_path = validate_fixture_path(self.settings.inbox, data_mode=data_mode)
        source = FixtureMailSource(fixture_path) if fixture_path is not None else make_mail_source("fixture", scenario)
        source = source.for_scenario(scenario)
        with world_tx() as world:
            source.schedule_future(world)
        ctx = ToolContext(scenario, Mailbox(source, scenario))
        inbox = {"name": source.path.name, "sha256": hashlib.sha256(source.path.read_bytes()).hexdigest()}
        mode_details = ({"mail_data_mode": "fixture", "web_data_mode": "live", "web_time_basis": "wall_clock",
                         "search_auth": "api_key" if os.environ.get(HYBRID_EXA_KEY_ENV) else "keyless"}
                        if data_mode == "hybrid" else {})
        if data_mode == "fixture":
            from refund_eval.published.fixture_web import (
                FIXTURE_WEB_DATASET,
                fixture_web_config,
            )

            public_read_urls = fixture_web_config(ctx)["public_read_urls"]
            provenance = {"data_mode": "fixture", "mail": FIXTURE_MAIL_PROVENANCE,
                          "web_dataset": FIXTURE_WEB_DATASET, "inbox": inbox}
        else:
            public_read_urls = []
            provenance = {"data_mode": "hybrid", **mode_details, "mail": FIXTURE_MAIL_PROVENANCE,
                          "public_read_policy": "public_web", "search_provider": "exa", "inbox": inbox}
        public_read_policy = "public_web" if data_mode == "hybrid" else "reviewed_urls"
        world_store().set(PROFILE_KEY, {
            "name": "published", "data_mode": data_mode, "fixture_provenance": provenance, **mode_details,
            "config_sha256": fingerprint(provenance), "public_read_policy": public_read_policy,
            "public_read_urls": public_read_urls,
        })

        mail = make_fixture_mail(ctx, audit=audit)
        self._stack.push_async_callback(mail.aclose)
        await self._add_tools(mail, "gmail")

        def search_audit(event: dict[str, Any]) -> None:
            audit({"event": "search_boundary", "call_id": current_tool_call_id(), **event})

        if data_mode == "fixture":
            from refund_eval.published.fixture_web import FixtureSearchService

            search: MCPService = FixtureSearchService(ctx, policy=allow_search_request, audit=search_audit)
        else:
            from refund_eval.published.search import ExaSearchService

            search = ExaSearchService(policy=allow_search_request, audit=search_audit,
                                      api_key=os.environ.get(HYBRID_EXA_KEY_ENV))
        self._stack.push_async_callback(search.aclose)
        await self._add_tools(search, "exa", live_web=data_mode != "fixture")
        names = [*self._tools, SCHEDULE_WAKEUP]
        if len(set(names)) != len(names):
            raise ValueError("World tool catalogs contain duplicate tool names")
        audit({"event": "profile_ready", "data_mode": data_mode, **mode_details,
               "public_read_policy": public_read_policy, "tools": list(self._tools)})

    async def _add_tools(self, service: MCPService, source: str, *, live_web: bool = False) -> None:
        """`published.adapter.inspect_tools` without the Inspect ToolDefs."""
        catalog = await service.list_tools()
        audit({"event": "catalog", "source": source, "sha256": fingerprint(catalog), "catalog": catalog})
        for descriptor in catalog["tools"]:
            if descriptor["name"] in self._tools:
                raise ValueError(f"Duplicate MCP tool name: {descriptor['name']}")
            self._tools[descriptor["name"]] = NativeTool(service, descriptor, source, live_web=live_web)

    # ------------------------------------------------------------------ agent operations

    def list_tools(self) -> list[dict[str, Any]]:
        """The agent's tools: name, description and input schema, as the Inspect path shows them."""
        return [*(tool.listing() for tool in self._tools.values()), copy.deepcopy(SCHEDULE_WAKEUP_TOOL)]

    async def call_tool(self, name: str, arguments: dict[str, Any], call_id: str | None) -> dict[str, Any]:
        """Run one tool call (one at a time, as the Inspect tools are not parallel).

        Returns an MCP CallToolResult: content blocks, and isError for errors the model sees."""
        async with self._lock:
            try:
                with active_tool_call(call_id), event_log.tool_call(name, call_id):
                    return {"content": await self._call(name, arguments, call_id), "isError": False}
            except ToolFailure as ex:
                return _error(str(ex))
            except LimitExceededError as ex:
                # The harness's guard_tool keeps the limit for its next on_continue; so does turn().
                world_store().set(DISPATCH_LIMIT_KEY, {"type": ex.type, "value": ex.value, "limit": ex.limit,
                                                       "message": str(ex)})
                return _error(str(ex))
            except ModelLimitExceeded as ex:
                _record_stop(STOP_MODEL_LIMIT)
                return _error(str(ex))
            finally:
                self.persist()

    async def _call(self, name: str, arguments: dict[str, Any], call_id: str | None) -> list[dict[str, Any]]:
        reason = kill_reason()
        if reason is not None:
            raise _kill(reason)
        enforce_session_end()
        if name == SCHEDULE_WAKEUP:
            return [text_block(schedule_wakeup(arguments))]
        tool = self._tools.get(name)
        if tool is None:
            raise ToolFailure(f"Tool {name} not found")
        return await tool.call(arguments, call_id)

    async def turn(self, *, ended: bool = True, text: str = "") -> dict[str, Any]:
        """After each agent generation: `ended` when it made no tool calls, `text` its message.

        Returns the wake-up to give the agent (None mid-work or once stopped) and the stop reason."""
        async with self._lock:
            try:
                wakeup = await self._turn(ended, text)
            finally:
                self.persist()
        stop_reason = load_world().stop_reason
        return {"stopped": stop_reason is not None, "stop_reason": stop_reason,
                "wakeup": wakeup.model_dump(mode="json") if wakeup is not None and stop_reason is None else None}

    async def _turn(self, ended: bool, text: str) -> Wakeup | None:
        settings, scenario = self.settings, self.scenario
        pending_limit = world_store().get(DISPATCH_LIMIT_KEY)
        if pending_limit is not None:
            world_store().set(DISPATCH_LIMIT_KEY, None)
            _record_stop(f"{pending_limit['type']}_limit")
            return None
        if load_world().stop_reason is not None:
            return None
        reason = kill_reason()
        with world_tx() as world:
            world.turn += 1
            if ended:
                world.status_reports.append(StatusReport(time=world.now, turn=world.turn, text=text))
            if reason is not None:
                pass
            elif ended and scenario.task.success(
                    scenario, world, world.wakeups[-1].time if world.wakeups else world.start_time):
                world.stop_reason = scenario.task.success_stop_reason
            elif aware(world.now) >= sim_deadline(world, settings.sim_weeks):
                world.stop_reason = STOP_SIM_TIME_CAP
            elif world.turn >= settings.max_turns:
                world.stop_reason = STOP_MAX_TURNS
            stop_reason = world.stop_reason
        if reason is not None:
            _kill(reason)
            return None
        if not ended or stop_reason is not None:
            return None
        try:
            await follow_ups_before_wake(settings.sim_weeks, settings.checkin_hours)
        except LimitExceededError as ex:
            _record_stop(f"{ex.type}_limit")
            return None
        except ModelLimitExceeded:
            _record_stop(STOP_MODEL_LIMIT)
            return None
        with world_tx() as world:
            catching_up = bool(unannounced_mail_ids(world))
            wakeup = wake(world, sim_weeks=settings.sim_weeks, checkin_hours=settings.checkin_hours)
            if (not catching_up or (world.requested_wakeup is not None
                                    and aware(world.requested_wakeup) <= aware(world.now))):
                world.requested_wakeup = None
            if wakeup is None or aware(world.now) >= sim_deadline(world, settings.sim_weeks):
                world.stop_reason = STOP_SIM_TIME_CAP
        return wakeup

    # ------------------------------------------------------------------ grader and operator reads

    def data(self) -> dict[str, Any]:
        """World state, event log and stop reason (the admin `data/get`)."""
        store = self.trial.store()
        world = store.get(WORLD_KEY) or {}
        return copy.deepcopy({
            "stop_reason": world.get("stop_reason"), "sim_time": world.get("now"), "world": world,
            "events": store.get(EVENTS_KEY) or [],
            "store": {key: value for key, value in store.items() if key not in {WORLD_KEY, EVENTS_KEY}},
        })

    def snapshot(self) -> dict[str, Any]:
        """A `world.trial_snapshot` of the trial (the output directory is brought up to date first)."""
        self.persist()
        return trial_snapshot.capture(self.trial.store()).model_dump(mode="json")

    def health(self) -> dict[str, Any]:
        world = self.trial.store().get(WORLD_KEY) or {}
        return {"ok": True, "sim_time": world.get("now"), "stopped": world.get("stop_reason") is not None}

    def persist(self) -> None:
        if self._writer is not None and self.trial is not None:
            self._writer.write(self.trial.store(), self.settings.model_dump(mode="json"))

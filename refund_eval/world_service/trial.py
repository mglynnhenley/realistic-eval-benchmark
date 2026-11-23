"""One trial's world, run without Inspect: setup, tool calls and turn boundaries.

Setup follows the Inspect harness (`agent.harness.refund_agent`, sharing `init_world`
from `runtime.turns`) and `published.profile.published_tools` (mail, search and the
browser, which runs in this process), in the same order, so the world and its event log
start the same. `turn()` is the world's half of the harness's `on_continue`, with the
same `runtime.turns` code: count the turn, save the agent's text as a status report,
stop on success, the session cap or the turn cap, otherwise run due company follow-ups
and wake the agent on mail, its requested time or the next check-in. The agent harness
in world-service mode (`agent.world_mode`) calls it.

Every tool call and turn is also recorded, in order, under `store.ACTIVITY_KEY` (`_record`), so
the verifier grades the agent's activity from the world's records (`grading.world_records`):
- `{"kind": "tool_call", "tool", "call_id", "arguments", "status", "error"?, "audit",
  "result_text"?, "sim_time"}`. `status` is "ok", "error" (the model is shown `error`),
  "exception" (an unexpected error; the MCP server shows the model its text, `error`) or
  "cancelled" (no result was returned). `audit` is the [start, end) range of the tool audit
  records made during the call; the raw result the model's text is rebuilt from is there, so
  it is not stored twice. `result_text` is kept only when no audit record holds the result
  (`schedule_wakeup`).
- `{"kind": "turn", "ended", "text", "counted", "stop_reason", "memory_calls", "turn",
  "stopped", "world_stop_reason", "wakeup", "sim_time"}`: the `/turn` request's fields as
  sent, then the world's turn count, its stop state and the wake-up it returned.
  `memory_calls` is agent-reported: `[{"id", "arguments_sha256"}]` for the harness's local
  memory tool calls since the last turn (they never reach the world), or None if not sent.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import tempfile
import uuid
from contextlib import AsyncExitStack
from functools import partial
from pathlib import Path
from typing import Any, Self

import anyio
from inspect_ai.util import LimitExceededError

from refund_eval.agent.schedule import aware, sim_deadline
from refund_eval.config import HYBRID_EXA_KEY_ENV
from refund_eval.killswitch import kill_reason
from refund_eval.models import client as models
from refund_eval.models.budget import BudgetedWorldModels
from refund_eval.models.client import (
    ModelClient,
    ModelLimitExceeded,
    ModelRequestRejected,
)
from refund_eval.models.replay import RecordingModelClient
from refund_eval.models.world_spend_ledger import (
    WorldSpendLedger,
    WorldSpendLedgerError,
)
from refund_eval.output_paths import validate_private_output
from refund_eval.published.adapter import AUDIT_KEY, MCPService, audit, fingerprint
from refund_eval.published.profile import (
    PROFILE_KEY,
    allow_search_request,
    validate_fixture_path,
)
from refund_eval.runtime.turns import (
    STOP_SIM_TIME_CAP,
    end_turn,
    follow_ups_before_wake,
    init_world,
    wake_after_turn,
)
from refund_eval.scenario.schema import Scenario, load_scenario
from refund_eval.sim.open_world import COMPANY_SIMULATOR_KEY
from refund_eval.sim.research import MODE_KEY as RESEARCH_MODE_KEY
from refund_eval.world import event_log, trial_snapshot
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
from refund_eval.world.mail import FixtureMailSource, Mailbox, make_mail_source
from refund_eval.world.state import (
    WORLD_KEY,
    StatusReport,
    TargetTokenLedger,
    Wakeup,
    load_world,
    world_tx,
)
from refund_eval.world.store import world_store
from refund_eval.world_service.settings import WorldSettings
from refund_eval.world_service.store import ACTIVITY_KEY, MemoryTrial, OutputWriter
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
STOP_MODEL_LIMIT = "model_limit"  # a world-side model client limit outside Inspect


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


def _record(entry: dict[str, Any]) -> None:
    """Append one tool call or turn to the trial's activity record."""
    world = world_store().get(WORLD_KEY) or {}
    world_store().set(ACTIVITY_KEY, [*(world_store().get(ACTIVITY_KEY) or []),
                                     copy.deepcopy({**entry, "sim_time": world.get("now")})])


def _audited_result(call_id: str | None, start: int) -> bool:
    """Whether the call's raw result is in the tool audit (from record `start` on)."""
    return any(e.get("event") == "result" and e.get("call_id") == call_id
               for e in (world_store().get(AUDIT_KEY) or [])[start:])


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
        self._spend_ledger: WorldSpendLedger | None = None
        self._spend_scope: dict[str, Any] | None = None
        self.trial: MemoryTrial | None = None
        self.scenario: Scenario | None = None

    # ------------------------------------------------------------------ lifecycle

    async def __aenter__(self) -> Self:
        settings = self.settings
        self.scenario = scenario = load_scenario(settings.scenario)
        if settings.world_cost_limit is not None:
            model_mapping = ({"simulator": settings.simulator_model, "grader": settings.grader_model}
                             if self._model_client is None else
                             {role: self._model_client.model_name(role) for role in ("simulator", "grader")})
            self._spend_scope = {"scenario": settings.scenario, "models": model_mapping}
            self._spend_ledger = WorldSpendLedger(settings.output_dir, settings.world_cost_limit, self._spend_scope)
            if self._spend_ledger.has_unresolved():
                raise WorldSpendLedgerError(
                    f"World-spend ledger in {self._spend_ledger.path} has an unresolved reservation; "
                    "refusing to erase prior trial artifacts or resume paid work. Reconcile it or use a new output directory."
                )
        if self._model_client is None:
            from refund_eval.models.openrouter import OpenRouterClient

            self._model_client = OpenRouterClient.from_settings(settings)
        self._writer = OutputWriter(settings.output_dir)
        self.trial = MemoryTrial(settings.scenario)  # as given, like the Inspect sample's metadata
        trial_store.install(self.trial)
        # Every world-side completion is kept in the store, so the trial can be replayed.
        recorded = RecordingModelClient(self._model_client)
        if settings.strict_budget and settings.world_cost_limit is None:
            raise ValueError("Strict world budget requires world_cost_limit")
        models.install(recorded if settings.world_cost_limit is None else
                       BudgetedWorldModels(recorded, settings.world_cost_limit, self.persist,
                                           prices=getattr(self._model_client, "tariffs", None) or {},
                                           ledger=self._spend_ledger,
                                           scope=self._spend_scope, strict_budget=settings.strict_budget))
        try:
            init_world(scenario, settings.seed)
            store = world_store()
            store.set(ACTIVITY_KEY, [])
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
        """Mail, search and the browser as `published_tools` builds them, in the same order and with
        the same profile record, startup checks and audit. The browser runs here, in the world."""
        from refund_eval.published import profile as published
        from refund_eval.published.browser import PublishedBrowserService
        from refund_eval.published.browser_runtimes import (
            DEFAULT_IMAGE,
            require_verified_browser_isolation,
        )
        from refund_eval.published.fixtures import (
            FIXTURE_MAIL_PROVENANCE,
            make_fixture_mail,
        )
        from refund_eval.published.network import BrowserBoundary

        data_mode, browser_runtime = self.settings.data_mode, self.settings.browser_runtime
        fixture_path = validate_fixture_path(self.settings.inbox, data_mode=data_mode)
        artifacts = validate_private_output(Path(self.settings.output_dir) / "browser")
        runtime_args = {"browser_runtime": browser_runtime} if browser_runtime != "docker" else {}
        status = (published.fixture_readiness(**runtime_args) if data_mode == "fixture" else
                  published.hybrid_readiness(**runtime_args))
        if not status["configured"]:
            raise ValueError(status["reason"])
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

            boundary_config = fixture_web_config(ctx)
            provenance = {"data_mode": "fixture", "mail": FIXTURE_MAIL_PROVENANCE,
                          "web_dataset": FIXTURE_WEB_DATASET, "browser_image": DEFAULT_IMAGE,
                          "web_sha256": fingerprint(boundary_config), "inbox": inbox}
        else:
            boundary_config = {"public_read_policy": "public_web", "public_read_urls": []}
            provenance = {"data_mode": "hybrid", **mode_details, "mail": FIXTURE_MAIL_PROVENANCE,
                          "browser_image": DEFAULT_IMAGE, "public_read_policy": "public_web",
                          "search_provider": "exa", "inbox": inbox}
        if data_mode == "hybrid" and scenario.id == "train_refund_late":
            # The booking pages and lookup the late-refund scenario serves itself (as published_tools does).
            from refund_eval.published.booking_channels import POLICY

            boundary_config["scenario_channel_policy"] = POLICY
        boundary = BrowserBoundary(ctx, **boundary_config)
        channel_fields = ({"scenario_channel": boundary.scenario_channels.descriptor}
                          if boundary.scenario_channels is not None else {})
        provenance.update(channel_fields)
        public_read_policy = boundary_config.get("public_read_policy", "reviewed_urls")
        world_store().set(PROFILE_KEY, {
            "name": "published", "data_mode": data_mode, "fixture_provenance": provenance, **channel_fields,
            **mode_details, "config_sha256": fingerprint(provenance), "browser_network": "unverified",
            "browser_runtime": browser_runtime, "public_read_policy": public_read_policy,
            "public_read_urls": boundary_config["public_read_urls"],
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

            search = ExaSearchService(policy=partial(allow_search_request, scenario=scenario), audit=search_audit,
                                      scenario=scenario,
                                      api_key=os.environ.get(HYBRID_EXA_KEY_ENV))
        self._stack.push_async_callback(search.aclose)
        await self._add_tools(search, "exa", live_web=data_mode != "fixture")

        async def handle_request(request: dict[str, Any]) -> dict[str, Any]:
            # The browser serves requests from its own task, outside the MCP call: name the call there too.
            with active_tool_call(request.get("tool_call_id")):
                try:
                    return await boundary(request)
                except ModelRequestRejected:
                    _record_stop("model_request_rejected")
                    raise
                except ModelLimitExceeded:
                    _record_stop(STOP_MODEL_LIMIT)
                    raise

        spool = Path(self._stack.enter_context(tempfile.TemporaryDirectory(prefix="refund-browser-"))) / "exchange"
        artifacts.mkdir(parents=True, exist_ok=True)
        network_audit = artifacts / f"{uuid.uuid4().hex}.network.jsonl"
        browser = PublishedBrowserService(handle_request, spool_dir=spool, image=DEFAULT_IMAGE,
                                          runtime=browser_runtime, audit_path=network_audit, audit=audit,
                                          audit_request=boundary.audit_request if data_mode == "hybrid" else None,
                                          output_dir=artifacts / "files")
        self._stack.push_async_callback(browser.aclose)
        # Starts the browser, runs the interception canary and records the isolation evidence.
        if self.settings.require_verified_browser_isolation:
            await browser.list_tools()
            require_verified_browser_isolation(browser.security_manifest)
        await self._add_tools(browser, "playwright")
        manifest = getattr(browser, "security_manifest", {}) or {}
        profile = world_store().get(PROFILE_KEY)
        profile["browser_network"] = manifest.get("network_mode", "unverified")
        profile["browser_audit"] = str(network_audit.relative_to(artifacts.parent))
        profile["browser_runtime"] = browser_runtime
        profile["browser_boundary"] = manifest
        if manifest.get("evidence_version") == 2:
            profile["evidence_version"] = 2
        world_store().set(PROFILE_KEY, profile)
        names = [*self._tools, SCHEDULE_WAKEUP]
        if len(set(names)) != len(names):
            raise ValueError("World tool catalogs contain duplicate tool names")
        audit({"event": "profile_ready", **channel_fields, "data_mode": data_mode, **mode_details,
               "public_read_policy": public_read_policy, "tools": list(self._tools),
               "browser_boundary": getattr(browser, "security_manifest", None)})

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
            start = len(world_store().get(AUDIT_KEY) or [])
            entry: dict[str, Any] = {"kind": "tool_call", "tool": name, "call_id": call_id,
                                     "arguments": arguments, "status": "cancelled"}
            try:
                with active_tool_call(call_id), event_log.tool_call(name, call_id):
                    content = await self._call(name, arguments, call_id)
                entry["status"] = "ok"
                if not _audited_result(call_id, start):
                    entry["result_text"] = "\n".join(p["text"] for p in content if p["type"] == "text")
                return {"content": content, "isError": False}
            except ToolFailure as ex:
                entry.update(status="error", error=str(ex))
                return _error(str(ex))
            except LimitExceededError as ex:
                # The harness's guard_tool keeps the limit for its next on_continue; so does turn().
                world_store().set(DISPATCH_LIMIT_KEY, {"type": ex.type, "value": ex.value, "limit": ex.limit,
                                                       "message": str(ex)})
                entry.update(status="error", error=str(ex))
                return _error(str(ex))
            except ModelLimitExceeded as ex:
                _record_stop(STOP_MODEL_LIMIT)
                entry.update(status="error", error=str(ex))
                return _error(str(ex))
            except ModelRequestRejected as ex:
                _record_stop("model_request_rejected")
                entry.update(status="error", error=str(ex))
                return _error(str(ex))
            except Exception as ex:
                entry.update(status="exception", error=str(ex))  # the MCP server's error result shows this text
                raise
            finally:
                entry["audit"] = [start, len(world_store().get(AUDIT_KEY) or [])]
                _record(entry)
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

    async def turn(self, *, ended: bool = True, text: str = "", stop_reason: str | None = None,
                   counted: bool = True, token_ledger: dict[str, Any] | None = None,
                   memory_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """After each agent generation: `ended` when it made no tool calls, `text` its message.

        `token_ledger` is the agent's target-token ledger (budgets are agent-side); it replaces
        the world's. `stop_reason` says the agent has ended the trial itself (its token budget,
        an Inspect limit, its kill switch, the loop ending): the turn is counted only if
        `counted`, the reason is recorded unless the world has stopped already, and nothing
        else runs. `memory_calls` (agent-reported) is only recorded. Returns the wake-up to give
        the agent (None mid-work or once stopped), the stop reason and the simulated time."""
        ledger = TargetTokenLedger.model_validate(token_ledger) if token_ledger is not None else None
        async with self._lock:
            wakeup = None
            try:
                if ledger is not None:
                    with world_tx() as world:
                        world.token_ledger = ledger
                if stop_reason is not None:
                    self._agent_stop(stop_reason, counted=counted, ended=ended, text=text)
                else:
                    wakeup = await self._turn(ended, text)
            finally:
                world = load_world()
                result = {"stopped": world.stop_reason is not None, "stop_reason": world.stop_reason,
                          "wakeup": (wakeup.model_dump(mode="json")
                                     if wakeup is not None and world.stop_reason is None else None),
                          "sim_time": world.now.isoformat()}
                _record({"kind": "turn", "ended": ended, "text": text, "counted": counted, "stop_reason": stop_reason,
                         "memory_calls": memory_calls, "turn": world.turn, "stopped": result["stopped"],
                         "world_stop_reason": world.stop_reason, "wakeup": result["wakeup"]})
                self.persist()
        return result

    def _pending_limit(self) -> bool:
        """A limit hit in a tool call stops the trial at the next turn, as the harness's on_continue does."""
        pending_limit = world_store().get(DISPATCH_LIMIT_KEY)
        if pending_limit is None:
            return False
        world_store().set(DISPATCH_LIMIT_KEY, None)
        _record_stop(f"{pending_limit['type']}_limit")
        return True

    def _agent_stop(self, reason: str, *, counted: bool, ended: bool, text: str) -> None:
        if self._pending_limit() or load_world().stop_reason is not None:
            return
        with world_tx() as world:
            if counted:
                world.turn += 1
                if ended:
                    world.status_reports.append(StatusReport(time=world.now, turn=world.turn, text=text))
            if reason == STOP_KILL_SWITCH:
                world.killed = True
            world.stop_reason = reason

    async def _turn(self, ended: bool, text: str) -> Wakeup | None:
        settings, scenario = self.settings, self.scenario
        if self._pending_limit() or load_world().stop_reason is not None:
            return None
        reason = kill_reason()
        stop_reason = end_turn(
            ended=ended, text=text, killed=reason is not None,
            succeeded=lambda world, as_of: scenario.task.success(scenario, world, as_of),
            success_reason=scenario.task.success_stop_reason,
            sim_weeks=settings.sim_weeks, max_turns=settings.max_turns)
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
        except ModelRequestRejected:
            _record_stop("model_request_rejected")
            return None
        wakeup, _ = wake_after_turn(sim_weeks=settings.sim_weeks, checkin_hours=settings.checkin_hours)
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

    def effective_settings(self) -> dict[str, Any]:
        """The settings as used: the world's recorded seed (drawn when none was given) and the
        models that served the simulator and grader roles."""
        settings = self.settings.model_dump(mode="json")
        seed = (self.trial.store().get(WORLD_KEY) or {}).get("seed") if self.trial is not None else None
        if seed is not None:
            settings["seed"] = seed
        if self._model_client is not None:
            for role in ("simulator", "grader"):
                settings[f"{role}_model"] = self._model_client.model_name(role)
        return settings

    def persist(self) -> None:
        if self._writer is not None and self.trial is not None:
            self._writer.write(self.trial.store(), self.effective_settings())

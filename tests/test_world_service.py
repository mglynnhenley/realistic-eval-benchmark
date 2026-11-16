"""The world service: its MCP tool list matches the Inspect catalog, and a scripted MCP client runs a
fixture trial end to end with the same world and event log as the Inspect path. The Inspect agent in
world-service mode (`agent.world_mode`), against the service on its own thread, makes the same world,
event log and conversation as the native path (parity check 1).

Offline: fixture data, a fake world-side model client, a mocked Exa transport and a stub browser.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from contextlib import asynccontextmanager, contextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from refund_eval.agent.world_mode import TURN_TOKEN_ENV
from refund_eval.models import client as models
from refund_eval.models.client import Completion, Message, ModelLimitExceeded
from refund_eval.models.replay import RECORDINGS_KEY
from refund_eval.published.adapter import _normalized
from refund_eval.published.mail import _visible_id
from refund_eval.published.search import PINNED_EXA_SEARCH_CATALOG
from refund_eval.world import interception
from refund_eval.world import store as trial_store
from refund_eval.world.event_log import EVENTS_KEY
from refund_eval.world.state import WORLD_KEY
from refund_eval.world.trial_snapshot import TrialSnapshot
from refund_eval.world_service import server
from refund_eval.world_service.__main__ import main as cli_main
from refund_eval.world_service.settings import WorldSettings
from refund_eval.world_service.store import EVENTS_FILE, STATE_FILE, MemoryTrial
from refund_eval.world_service.trial import WorldTrial, init_world

ADMIN, TURN = "admin-secret", "turn-secret"
AVANTI = "customer.resolutions@avantiwestcoast.co.uk"
WAITING = ["I emailed Avanti about the refund.", *["Still waiting for Avanti."] * 4]
MAX_TURNS = 1 + len(WAITING) + 3  # send; wait for the reply; search, read, report
ACTION = {
    "decision": "evidence_required", "decision_basis": "We need a medical note.", "summary": "Asked for evidence.",
    "reply": True, "reply_after_hours": 2, "subject": "Your refund request", "body": "Please send a medical note.",
    "page_body": "", "payments": [], "case_status": "open",
}


class FakeModels:
    """World-side models: every company proposes ACTION, every consistency check passes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def model_name(self, role):
        return f"fake/{role}"

    async def complete(self, role, messages, *, schema=None, **kwargs):
        name = schema.name if schema else None
        self.calls.append((role, name))
        reply = {"open_world_action": ACTION, "response_consistency": {"consistent": True, "reason": "ok"}}[name]
        text = json.dumps(reply)
        return Completion(text=text, message=Message("assistant", text), stop_reason="stop")


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "KILL"))

    def respond(request):
        payload = json.loads(request.content)
        if payload["method"] == "notifications/initialized":
            return httpx.Response(202)
        result = ({"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "offline-exa", "version": "1"}}
                  if payload["method"] == "initialize" else PINNED_EXA_SEARCH_CATALOG)
        assert payload["method"] in {"initialize", "tools/list"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    from refund_eval.published import search

    exa = search.ExaSearchService
    monkeypatch.setattr(search, "ExaSearchService",
                        lambda **kwargs: exa(**kwargs, transport=httpx.MockTransport(respond)))
    yield
    trial_store.install(None)
    models.install(None)


def stub_browser(monkeypatch):
    """The Inspect catalog without Chromium: the browser lists no tools (they join the service in step 9)."""
    from refund_eval.published import browser, profile

    class NoBrowser:
        def __init__(self, *args, **kwargs):
            self.security_manifest = {}

        async def list_tools(self):
            return {"tools": []}

        async def aclose(self):
            pass

    monkeypatch.setattr(browser, "PublishedBrowserService", NoBrowser)
    ready = {"configured": True, "reason": "test"}
    monkeypatch.setattr(profile, "fixture_readiness", lambda **kwargs: ready)
    monkeypatch.setattr(profile, "hybrid_readiness", lambda **kwargs: ready)


@asynccontextmanager
async def session(trial: WorldTrial):
    async with (server.running(trial, host="127.0.0.1", port=0, admin_port=0,
                               turn_token=TURN, admin_token=ADMIN) as (port, admin_port),
                streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write, _),
                ClientSession(read, write) as client):
        await client.initialize()
        yield client, f"http://127.0.0.1:{port}", f"http://127.0.0.1:{admin_port}"


def settings(tmp_path, **kwargs) -> WorldSettings:
    return WorldSettings(**{"scenario": "train_refund", "seed": 7, "data_mode": "fixture", "research_mode": "saved",
                            "output_dir": str(tmp_path / "world"), **kwargs})


# --------------------------------------------------------------------------- schema parity


async def inspect_catalog(data_mode: str) -> list[tuple[str, str, dict]]:
    from refund_eval.agent.harness import guard_tool, schedule_wakeup
    from refund_eval.published.profile import published_tools
    from refund_eval.scenario.schema import load_scenario

    scenario = load_scenario("train_refund")
    trial_store.install(MemoryTrial(scenario.id))
    try:
        init_world(scenario, 7)
        async with published_tools(scenario, data_mode=data_mode) as tools:
            tools = [guard_tool(t) for t in [*tools, schedule_wakeup()]]
            return [(t.name, t.description, t.parameters.model_dump(exclude_none=True)) for t in tools]
    finally:
        trial_store.install(None)


@pytest.mark.parametrize("data_mode", ["fixture", "hybrid"])
async def test_mcp_tool_list_matches_the_inspect_catalog(tmp_path, monkeypatch, data_mode):
    stub_browser(monkeypatch)
    expected = await inspect_catalog(data_mode)
    async with (WorldTrial(settings(tmp_path, data_mode=data_mode), model_client=FakeModels()) as trial,
                session(trial) as (client, _, _)):
        listed = (await client.list_tools()).tools
    assert [t.name for t in listed] == [name for name, _, _ in expected]
    assert [t.description for t in listed] == [description for _, description, _ in expected]
    for tool, (_, _, parameters) in zip(listed, expected, strict=True):
        # Equal up to the JSON Schema defaults Inspect's ToolParams writes out at the root.
        assert _normalized(tool.inputSchema, root=True) == _normalized(parameters, root=True), tool.name
    assert listed[-1].inputSchema == expected[-1][2]  # schedule_wakeup is exactly Inspect's


# --------------------------------------------------------------------------- end to end


def no_inspect_runtime(monkeypatch):
    """Fail if the world service touches Inspect's store, transcript, sample state or models."""
    import inspect_ai.log
    import inspect_ai.model
    import inspect_ai.solver._task_state
    import inspect_ai.util

    def forbidden(*args, **kwargs):
        raise AssertionError("Inspect runtime used on the world service path")

    for module, name in [(inspect_ai.util, "store"), (inspect_ai.log, "transcript"),
                         (inspect_ai.solver._task_state, "sample_state"), (inspect_ai.model, "get_model")]:
        monkeypatch.setattr(module, name, forbidden)


async def drive_mcp(tmp_path) -> dict:
    """A scripted agent over MCP: email Avanti, end the turn, read the reply that wakes it, end again."""
    fake = FakeModels()
    async with (WorldTrial(settings(tmp_path, max_turns=MAX_TURNS), model_client=fake) as trial,
                session(trial) as (client, base, admin), httpx.AsyncClient() as http):
        async def turn(ended: bool, text: str = "", token: str = TURN):
            return await http.post(f"{base}/turn", json={"ended": ended, "text": text},
                                   headers={"Authorization": f"Bearer {token}"})

        sent = await client.call_tool("send_message", {
            "to": [AVANTI], "subject": "Cancelled train refund", "body": "My train was cancelled."})
        assert not sent.isError, sent.content
        assert json.loads(sent.content[0].text)["labelIds"] == ["SENT"]
        mid = (await turn(False)).json()
        assert mid == {"stopped": False, "stop_reason": None, "wakeup": None, "sim_time": mid["sim_time"]}
        assert mid["sim_time"] == trial.data()["sim_time"]

        assert (await turn(True, token="wrong")).status_code == 401
        # Avanti replies on Tuesday evening; four fixture inbox emails wake the agent first.
        for n, text in enumerate(WAITING):
            woke = (await turn(True, text)).json()
            assert woke["stopped"] is False and woke["wakeup"]["kind"] == "new_mail"
            assert woke["wakeup"]["text"].endswith("] 1 new email.")
            [mail_id] = woke["wakeup"]["announced_mail_ids"]
            [mail] = [e for e in trial.data()["world"]["overlay_inbox"] if e["id"] == mail_id]
            assert (mail["source"] == "counterparty") == (n == len(WAITING) - 1), (n, woke["wakeup"]["time"])
        assert "avanti" in mail["sender"]
        visible = _visible_id(mail_id)

        found = await client.call_tool("search_threads", {"query": "is:unread"})
        assert not found.isError
        assert visible in found.content[0].text
        await turn(False)
        read = await client.call_tool("get_message", {"messageId": visible})
        assert not read.isError
        assert json.loads(read.content[0].text)["plaintextBody"].strip() == ACTION["body"]
        await turn(False)
        final = (await turn(True, "Avanti asked for a medical note.")).json()
        assert final == {"stopped": True, "stop_reason": "max_turns", "wakeup": None, "sim_time": final["sim_time"]}

        bad = await client.call_tool("schedule_wakeup", {"at": "tomorrow"})
        assert bad.isError and "YYYY-MM-DD HH:MM" in bad.content[0].text

        body = {"jsonrpc": "2.0", "id": 1, "method": "data/get", "params": {}}
        assert (await http.post(f"{admin}/agentenv", json=body)).status_code == 401
        assert (await http.post(f"{admin}/agentenv", json=body,
                                headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await http.get(f"{admin}/health")).status_code == 401
        got = await http.post(f"{admin}/agentenv", json=body, headers={"Authorization": f"Bearer {ADMIN}"})
        assert got.status_code == 200
        [part] = got.json()["result"]["parts"]
        assert part["kind"] == "data"
        data = part["data"]
        health = await http.get(f"{admin}/health", headers={"Authorization": f"Bearer {ADMIN}"})
        assert health.json()["stopped"] is True
        snap = await http.post(f"{admin}/agentenv", json={**body, "method": "snapshot"},
                               headers={"Authorization": f"Bearer {ADMIN}"})
        [snapshot_part] = snap.json()["result"]["parts"]
        snapshot = snapshot_part["data"]
        unknown = await http.post(f"{admin}/agentenv", json={**body, "method": "data/reset"},
                                  headers={"Authorization": f"Bearer {ADMIN}"})
        assert unknown.json()["error"]["code"] == -32601
    assert {name for _, name in fake.calls} == {"open_world_action", "response_consistency"}
    assert TrialSnapshot.model_validate(snapshot).store[WORLD_KEY] == data["world"]
    return {"data": data, "visible": visible, "calls": fake.calls}


def run_inspect(tmp_path, visible: str | None = None, *, script=None, world_url: str | None = None, **agent_args):
    """The same calls through the Inspect harness: its native tools (browser stubbed out), or with
    `world_url` the world service's. `script` replaces the fixed outputs (a mockllm callable)."""
    from inspect_ai import Task, eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import ModelOutput, get_model

    from refund_eval.agent.harness import refund_agent
    from refund_eval.scenario.schema import load_scenario

    scenario = load_scenario("train_refund")
    outputs = script or [
        ModelOutput.for_tool_call("mockllm/model", "send_message", {
            "to": [AVANTI], "subject": "Cancelled train refund", "body": "My train was cancelled."}),
        *(ModelOutput.from_content("mockllm/model", text) for text in WAITING),
        ModelOutput.for_tool_call("mockllm/model", "search_threads", {"query": "is:unread"}),
        ModelOutput.for_tool_call("mockllm/model", "get_message", {"messageId": visible}),
        ModelOutput.from_content("mockllm/model", "Avanti asked for a medical note."),
    ]
    if world_url is None:
        models.install(FakeModels())
    args = {"max_turns": MAX_TURNS, "history_tokens": 0, **agent_args}
    [log] = eval(Task(dataset=[Sample(input=scenario.task_text, metadata={"scenario": "train_refund"})],
                      solver=refund_agent(scenario_id="train_refund", published_data="fixture", research_mode="saved",
                                          seed=7, world_url=world_url, **args)),
                 model=get_model("mockllm/model", custom_outputs=outputs), log_dir=str(tmp_path / "logs"),
                 display="none")
    assert log.status == "success", log.error
    return log.samples[0]


def _without_call_ids(value):
    if isinstance(value, dict):
        return {k: _without_call_ids(v) for k, v in value.items() if k not in {"tool_call_id", "wall_time"}}
    if isinstance(value, list):
        return [_without_call_ids(v) for v in value]
    return value


def test_scripted_mcp_trial_matches_the_inspect_path(tmp_path, monkeypatch):
    stub_browser(monkeypatch)
    with monkeypatch.context() as patch:
        no_inspect_runtime(patch)
        result = asyncio.run(drive_mcp(tmp_path))
    data = result["data"]
    world, events = data["world"], data["events"]
    assert world["stop_reason"] == "max_turns" and world["turn"] == MAX_TURNS

    # Events made by a tool call carry its id: MCP session id and JSON-RPC request id.
    send_events = [e for e in events if e.get("tool") == "send_message"]
    assert send_events and len({e["tool_call_id"] for e in send_events}) == 1
    session_id, request_id = send_events[0]["tool_call_id"].rsplit(":", 1)
    assert len(session_id) == 32 and request_id.isdigit()
    [write] = [e for e in events if e["kind"] == "agent_write"]
    assert write["payload"]["interception"]["payload"]["tool_call_id"] == send_events[0]["tool_call_id"]
    audit = data["store"]["published_tool_audit"]
    assert {e["call_id"] for e in audit if e.get("tool") == "send_message"} == {send_events[0]["tool_call_id"]}

    # The output directory holds the same state and log.
    out = tmp_path / "world"
    assert [json.loads(line) for line in (out / EVENTS_FILE).read_text().splitlines()] == events
    state = json.loads((out / STATE_FILE).read_text())
    assert state["stop_reason"] == "max_turns" and state["store"][WORLD_KEY] == world
    assert EVENTS_KEY not in state["store"]

    # Every world-side completion is recorded for replay.
    recorded = data["store"][RECORDINGS_KEY]
    assert [r["role"] for r in recorded] == [role for role, _ in result["calls"]]
    assert all("response" in r for r in recorded)

    # The Inspect path, given the same calls and the same seed (so the same ids), makes the same world.
    sample = run_inspect(tmp_path, result["visible"])
    assert _without_call_ids(sample.store[WORLD_KEY]) == _without_call_ids(world)
    assert _without_call_ids(sample.store[EVENTS_KEY]) == _without_call_ids(events)
    inspect_audit = [(e["event"], e.get("tool")) for e in sample.store["published_tool_audit"]
                     if e.get("source") in {"gmail", "exa"}]
    assert inspect_audit == [(e["event"], e.get("tool")) for e in audit if e.get("source") in {"gmail", "exa"}]


async def test_caller_meta_names_the_tool_call(tmp_path):
    async with (WorldTrial(settings(tmp_path), model_client=FakeModels()) as trial,
                session(trial) as (client, _, _)):
        result = await client.call_tool("schedule_wakeup", {"at": "2026-09-30 10:00"},
                                        meta={"tool_call_id": "toolu_123"})
        assert not result.isError, result.content
        events = trial.data()["events"]
    [requested] = [e for e in events if e["kind"] == "wakeup_requested"]
    assert requested["tool"] == "schedule_wakeup" and requested["tool_call_id"] == "toolu_123"


def test_active_call_id_replaces_the_transcript_lookup():
    assert interception.current_tool_call_id() is None  # no Inspect sample, no active call
    with interception.active_tool_call("sess:7"):
        assert interception.current_tool_call_id() == "sess:7"
    assert interception.current_tool_call_id() is None


def test_cli_help_and_tokens(capsys, monkeypatch):
    with pytest.raises(SystemExit) as exit_:
        cli_main(["--help"])
    assert exit_.value.code == 0
    assert "--data-mode" in capsys.readouterr().out
    monkeypatch.delenv("WORLD_ADMIN_TOKEN", raising=False)
    assert cli_main([]) == 2


class OutOfCredit(FakeModels):
    async def complete(self, role, messages, *, schema=None, **kwargs):
        raise ModelLimitExceeded("402: out of credit")


async def test_model_client_limit_during_a_send_stops_the_trial(tmp_path):
    async with (WorldTrial(settings(tmp_path), model_client=OutOfCredit()) as trial,
                session(trial) as (client, _, _)):
        sent = await client.call_tool("send_message", {
            "to": [AVANTI], "subject": "Cancelled train refund", "body": "My train was cancelled."})
        world = trial.data()["world"]
    assert world["stop_reason"] == "model_limit"
    assert "SENT" not in sent.content[0].text


# --------------------------------------------------------------------------- the agent in world-service mode

WORLD_THREAD = "world-service"


@contextmanager
def world_service_thread(settings: WorldSettings, monkeypatch):
    """The world service on a thread of its own, as its container would run it beside the agent's.

    Its store is the only world: any agent-side read or write of it (from another thread) fails."""
    store = MemoryTrial.store

    def world_thread_only(self):
        assert threading.current_thread().name == WORLD_THREAD, "the agent touched the world's store"
        return store(self)

    monkeypatch.setattr(MemoryTrial, "store", world_thread_only)
    monkeypatch.setenv(TURN_TOKEN_ENV, TURN)
    ready: queue.Queue = queue.Queue()
    stop = threading.Event()
    holder: dict = {}

    async def serve():
        async with (WorldTrial(settings, model_client=FakeModels()) as trial,
                    server.running(trial, host="127.0.0.1", port=0, admin_port=0, turn_token=TURN,
                                   admin_token=ADMIN) as (port, _)):
            holder["trial"] = trial
            ready.put(f"http://127.0.0.1:{port}")
            while not stop.is_set():
                await asyncio.sleep(0.01)
            holder["data"] = trial.data()

    def target():
        try:
            asyncio.run(serve())
        except BaseException as ex:  # noqa: BLE001 - reported to the test
            ready.put(ex)

    thread = threading.Thread(target=target, name=WORLD_THREAD)
    thread.start()
    url = ready.get(timeout=60)
    if isinstance(url, BaseException):
        raise url
    try:
        yield url, holder
    finally:
        stop.set()
        thread.join(60)


class ScriptedAgent:
    """drive_mcp's calls as a mockllm script, with fixed tool call ids. It reads the reply's id from the
    world (`world()`), as a model would from its search results; it records the tools it was offered."""

    def __init__(self, world):
        self.world = world
        self.tools = None
        self.step = 0

    def __call__(self, input, tools, tool_choice, config):
        from inspect_ai.model import ModelOutput

        if self.tools is None:
            self.tools = [(t.name, t.description, t.parameters.model_dump(exclude_none=True)) for t in tools]
        n, self.step = self.step, self.step + 1
        if n == 0:
            return ModelOutput.for_tool_call("mockllm/model", "send_message", {
                "to": [AVANTI], "subject": "Cancelled train refund", "body": "My train was cancelled."},
                tool_call_id="call_send")
        if n <= len(WAITING):
            return ModelOutput.from_content("mockllm/model", WAITING[n - 1])
        if n == len(WAITING) + 1:
            return ModelOutput.for_tool_call("mockllm/model", "search_threads", {"query": "is:unread"},
                                             tool_call_id="call_search")
        if n == len(WAITING) + 2:
            [mail_id] = self.world()["wakeups"][-1]["announced_mail_ids"]
            return ModelOutput.for_tool_call("mockllm/model", "get_message", {"messageId": _visible_id(mail_id)},
                                             tool_call_id="call_read")
        return ModelOutput.from_content("mockllm/model", "Avanti asked for a medical note.")


def _without_wall_time(value):
    if isinstance(value, dict):
        return {k: _without_wall_time(v) for k, v in value.items() if k != "wall_time"}
    if isinstance(value, list):
        return [_without_wall_time(v) for v in value]
    return value


def test_world_service_mode_matches_the_native_path(tmp_path, monkeypatch):
    """Parity check 1: the same scripted agent, natively and with its world in the world service."""
    from refund_eval.world.state import load_world

    stub_browser(monkeypatch)
    native_script = ScriptedAgent(lambda: load_world().model_dump(mode="json"))
    native = run_inspect(tmp_path / "native", script=native_script)

    with world_service_thread(settings(tmp_path, max_turns=MAX_TURNS), monkeypatch) as (url, held):
        remote_script = ScriptedAgent(lambda: held["trial"].trial.data[WORLD_KEY])
        remote = run_inspect(tmp_path / "remote", script=remote_script, world_url=url)
    data = held["data"]

    # The model saw the same tools (names, descriptions, schemas) and the same conversation.
    assert remote_script.tools == native_script.tools
    assert [t[0] for t in remote_script.tools][-1] == "schedule_wakeup"
    assert [(m.role, m.text) for m in remote.messages] == [(m.role, m.text) for m in native.messages]

    # The same world and event log; only wall time differs. Tool call ids are Inspect's on both paths.
    world, events = data["world"], data["events"]
    assert world["stop_reason"] == "max_turns" and world["turn"] == MAX_TURNS
    assert _without_wall_time(world) == _without_wall_time(native.store[WORLD_KEY])
    assert _without_wall_time(events) == _without_wall_time(native.store[EVENTS_KEY])
    assert {e["tool_call_id"] for e in events if e.get("tool") == "send_message"} == {"call_send"}
    audit = [(e["event"], e.get("tool"), e.get("call_id")) for e in data["store"]["published_tool_audit"]
             if e.get("source") in {"gmail", "exa"}]
    assert audit == [(e["event"], e.get("tool"), e.get("call_id")) for e in native.store["published_tool_audit"]
                     if e.get("source") in {"gmail", "exa"}]

    # The agent's own sample holds no world.
    assert WORLD_KEY not in remote.store and EVENTS_KEY not in remote.store


class Waiting:
    """Ends every turn, with provider usage, so a token budget runs out."""

    def __call__(self, input, tools, tool_choice, config):
        from inspect_ai.model import ModelOutput, ModelUsage

        output = ModelOutput.from_content("mockllm/model", "Waiting.")
        output.usage = ModelUsage(input_tokens=100, input_tokens_cache_read=700, input_tokens_cache_write=150,
                                  output_tokens=50, reasoning_tokens=30, total_tokens=1000)
        return output


def test_token_budget_stays_with_the_agent_and_the_world_records_it(tmp_path, monkeypatch):
    stub_browser(monkeypatch)
    native = run_inspect(tmp_path / "native", script=Waiting(), max_turns=100, token_budget=2500)
    with world_service_thread(settings(tmp_path, max_turns=100), monkeypatch) as (url, held):
        remote = run_inspect(tmp_path / "remote", script=Waiting(), max_turns=100, token_budget=2500, world_url=url)
    world = held["data"]["world"]
    assert world["stop_reason"] == "token_budget" and world["turn"] == 3
    assert world["token_ledger"]["budget"] == 2500 and len(world["token_ledger"]["calls"]) == 3
    # The same final world, ledger and notices; the ledger reaches the world at each turn rather than
    # at each generation, so its events are ordered differently.
    assert _without_wall_time(world) == _without_wall_time(native.store[WORLD_KEY])
    assert [(m.role, m.text) for m in remote.messages] == [(m.role, m.text) for m in native.messages]


class KilledAfterTwoTurns:
    """Turns the kill switch on during the second generation."""

    def __init__(self, kill_file):
        self.kill_file, self.calls = kill_file, 0

    def __call__(self, input, tools, tool_choice, config):
        from inspect_ai.model import ModelOutput

        self.calls += 1
        if self.calls == 2:
            self.kill_file.touch()
        return ModelOutput.from_content("mockllm/model", "Waiting.")


def test_kill_switch_stays_with_the_agent_and_the_world_records_it(tmp_path, monkeypatch):
    stub_browser(monkeypatch)
    kill_file = tmp_path / "KILL"  # the offline fixture's REFUND_EVAL_KILL_FILE
    native = run_inspect(tmp_path / "native", script=KilledAfterTwoTurns(kill_file))
    kill_file.unlink()
    with world_service_thread(settings(tmp_path), monkeypatch) as (url, held):
        remote = run_inspect(tmp_path / "remote", script=KilledAfterTwoTurns(kill_file), world_url=url)
    world = held["data"]["world"]
    assert world["killed"] and world["stop_reason"] == "kill_switch" and world["turn"] == 2
    assert remote.limit is not None and remote.limit.type == "operator"
    assert _without_wall_time(world) == _without_wall_time(native.store[WORLD_KEY])


def test_world_service_mode_refuses_resume(tmp_path):
    from refund_eval.agent.harness import refund_agent

    with pytest.raises(ValueError, match="resume_log is not supported in world-service mode"):
        refund_agent(world_url="http://world:8000", resume_log=str(tmp_path / "trial.eval"))


async def test_turn_records_the_agents_stop(tmp_path):
    async with (WorldTrial(settings(tmp_path), model_client=FakeModels()) as trial,
                session(trial) as (_, base, _), httpx.AsyncClient() as http):
        headers = {"Authorization": f"Bearer {TURN}"}
        assert (await http.post(f"{base}/turn", json={"stop_reason": 7}, headers=headers)).status_code == 400
        bad_ledger = await http.post(f"{base}/turn", json={"token_ledger": {"budget": 0}}, headers=headers)
        assert bad_ledger.status_code == 400
        stopped = (await http.post(f"{base}/turn", json={"stop_reason": "cost_limit", "counted": False},
                                   headers=headers)).json()
        assert stopped["stopped"] and stopped["stop_reason"] == "cost_limit"
        later = (await http.post(f"{base}/turn", json={"stop_reason": "loop_ended"}, headers=headers)).json()
        world = trial.data()["world"]
    assert later["stop_reason"] == "cost_limit" and world["stop_reason"] == "cost_limit" and world["turn"] == 0


def test_task_in_world_service_mode_passes_the_url_and_has_no_scorers(monkeypatch):
    from inspect_ai.solver import generate

    import refund_eval.task as task_module

    captured = {}

    def agent(**kwargs):
        captured.update(kwargs)
        return generate()

    def not_here(**kwargs):
        raise AssertionError("tool readiness is the world container's")

    from refund_eval.published import profile

    monkeypatch.setattr(task_module, "refund_agent", agent)
    monkeypatch.setattr(task_module, "register_default_costs", lambda *a, **kw: None)
    monkeypatch.setattr(task_module, "default_model_roles", lambda *a: {})
    monkeypatch.setattr(profile, "fixture_readiness", not_here)
    task = task_module.impossible_refund(scenario="train_refund", published_data="fixture",
                                         world_url="http://world:8000")
    assert captured["world_url"] == "http://world:8000"
    assert not task.scorer

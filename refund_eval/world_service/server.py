"""HTTP faces of the world service.

Agent port (default 0.0.0.0:8000), reachable from the agent container:
- `/mcp`: MCP over streamable HTTP. Lists and runs the agent's tools. The tools are
  served with the low-level `mcp` Server, not FastMCP, because FastMCP builds schemas
  from Python signatures and these must be the native catalogs' schemas unchanged.
  No agent-env card or other route names the service as an evaluation.
- `POST /turn`: the agent harness's turn boundary (`WorldTrial.turn`), behind its
  own bearer token. Not an MCP tool, so the model can neither see nor call it. Body:
  `{ended, text, counted, stop_reason, token_ledger, memory_calls}` (all optional); response:
  `{stopped, stop_reason, wakeup, sim_time}`. `memory_calls` is the harness's report of its
  local memory tool calls since the last turn, recorded as agent-reported.

Admin port (127.0.0.1 only, default 9000), behind the admin bearer token:
- `POST /agentenv`: JSON-RPC 2.0 in the agentenv-protocol shape. `data/get` returns
  `{"parts": [{"kind": "data", "data": {world, events, stop_reason, ...}}]}`;
  `snapshot` returns the whole trial store.
- `GET /health`.

A tool call's id is the caller's `_meta.tool_call_id` when sent, else
`<MCP session id>:<JSON-RPC request id>`, which links world events to the call.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import signal
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from refund_eval.world_service.trial import WorldTrial

logger = logging.getLogger(__name__)

ADMIN_HOST = "127.0.0.1"
SERVER_NAME = "assistant-tools"
CALLER_ID_META = "tool_call_id"


def _authorised(request: Request, token: str) -> bool:
    supplied = request.headers.get("authorization", "")
    return hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode())


def _memory_calls_ok(value: Any) -> bool:
    return value is None or isinstance(value, list) and all(
        isinstance(call, dict) and set(call) == {"id", "arguments_sha256"} and isinstance(call["id"], str | None)
        and isinstance(call["arguments_sha256"], str) for call in value)


def _unauthorised() -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": "unauthorized", "message": "A valid bearer token is required."}},
                        status_code=401)


def call_id(context: Any) -> str:
    """The caller's `_meta.tool_call_id`, else `<session id>:<request id>`."""
    meta = context.meta
    caller = (meta.model_extra or {}).get(CALLER_ID_META) if meta is not None else None
    if isinstance(caller, str) and caller:
        return caller
    request = context.request
    session = request.headers.get(MCP_SESSION_ID_HEADER) if request is not None else None
    return f"{session or 'no-session'}:{context.request_id}"


def mcp_server(trial: WorldTrial) -> Server:
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(**tool) for tool in trial.list_tools()]

    # The world validates arguments itself, so a rejected call is audited as on the Inspect path.
    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        result = await trial.call_tool(name, arguments, call_id(server.request_context))
        return types.CallToolResult.model_validate(result)

    return server


class _ASGI:
    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.manager.handle_request(scope, receive, send)


def agent_app(trial: WorldTrial, *, turn_token: str) -> Starlette:
    """The agent-facing app: `/mcp` and the harness's `POST /turn`."""
    manager = StreamableHTTPSessionManager(app=mcp_server(trial))

    async def turn(request: Request) -> JSONResponse:
        if not _authorised(request, turn_token):
            return _unauthorised()
        try:
            body = await request.json() if await request.body() else {}
        except ValueError:
            body = None
        args = ({"ended": body.get("ended", True), "text": body.get("text", ""), "counted": body.get("counted", True),
                 "stop_reason": body.get("stop_reason"), "token_ledger": body.get("token_ledger"),
                 "memory_calls": body.get("memory_calls")}
                if isinstance(body, dict) else {"ended": None})
        if not (isinstance(args["ended"], bool) and isinstance(args["text"], str) and isinstance(args["counted"], bool)
                and isinstance(args["stop_reason"], str | None) and isinstance(args["token_ledger"], dict | None)
                and _memory_calls_ok(args["memory_calls"])):
            return JSONResponse({"ok": False, "error": {"code": "invalid", "message": (
                "ended: bool, text: str, counted: bool, stop_reason: str | null, token_ledger: object | null, "
                "memory_calls: [{id: str | null, arguments_sha256: str}] | null")}},
                status_code=400)
        try:
            return JSONResponse(await trial.turn(**args))
        except ValidationError as ex:
            return JSONResponse({"ok": False, "error": {"code": "invalid", "message": str(ex)}}, status_code=400)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Route("/mcp", endpoint=_ASGI(manager)), Route("/turn", turn, methods=["POST"])],
                     lifespan=lifespan)


def _rpc_error(rid: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def admin_app(trial: WorldTrial, *, admin_token: str) -> Starlette:
    """The operator and grader app; serve it on 127.0.0.1 only."""

    async def rpc(request: Request) -> JSONResponse:
        if not _authorised(request, admin_token):
            return _unauthorised()
        try:
            body = await request.json()
        except ValueError:
            return _rpc_error(None, -32700, "Parse error")
        rid = body.get("id") if isinstance(body, dict) else None
        method = body.get("method") if isinstance(body, dict) else None
        if method == "data/get":
            data = trial.data()
        elif method == "snapshot":
            data = trial.snapshot()
        else:
            return _rpc_error(rid, -32601, f"Method not found: {method}")
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"parts": [{"kind": "data", "data": data}]}})

    async def health(request: Request) -> JSONResponse:
        if not _authorised(request, admin_token):
            return _unauthorised()
        return JSONResponse(trial.health())

    return Starlette(routes=[Route("/agentenv", rpc, methods=["POST"]), Route("/health", health, methods=["GET"])])


def _bound_port(server: uvicorn.Server) -> int:
    return server.servers[0].sockets[0].getsockname()[1]


@contextlib.asynccontextmanager
async def running(trial: WorldTrial, *, host: str = "0.0.0.0", port: int = 8000, admin_port: int = 9000,
                  turn_token: str, admin_token: str) -> AsyncIterator[tuple[int, int]]:
    """Serve both apps until the block exits; yields the bound (agent, admin) ports (0 picks free ones)."""
    servers = [
        uvicorn.Server(uvicorn.Config(agent_app(trial, turn_token=turn_token), host=host, port=port,
                                      log_level="warning", lifespan="on")),
        uvicorn.Server(uvicorn.Config(admin_app(trial, admin_token=admin_token), host=ADMIN_HOST,
                                      port=admin_port, log_level="warning", lifespan="off")),
    ]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        while not all(server.started for server in servers):
            done = [task for task in tasks if task.done()]
            if done:
                done[0].result()
                raise RuntimeError("World service stopped while starting")
            await asyncio.sleep(0.01)
        yield _bound_port(servers[0]), _bound_port(servers[1])
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)


async def serve_until_stopped(trial: WorldTrial, **kwargs: Any) -> None:
    async with running(trial, **kwargs):
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await stop.wait()

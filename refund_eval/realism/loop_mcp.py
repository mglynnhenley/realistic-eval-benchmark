"""Local stdio MCP control plane for persistent realism-loop jobs.

Run ``python -m refund_eval.realism.loop_mcp --repo /path/to/repo
--backend package.module:run_job``. No agent CLI/session is needed. Backend is a
server administrator setting, never supplied by tool callers. Job settings pin a
commit; candidate changes are retained in an isolated worktree for review.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from refund_eval.realism.background import JobManager

VERSION = "2025-06-18"
SUPPORTED = {"2024-11-05", "2025-03-26", VERSION}
TOOLS = [
    {"name": "realism_start", "description": "Start one autonomous background realism job within an explicit additional model budget; includes coding, trials and judging. Returns immediately with job ID. Source is a committed Git revision; does not modify the current checkout.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": ["request_key", "budget_usd"], "properties": {
         "request_key": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,80}$", "description": "Stable invocation key; retrying it returns the same job, never repeats paid work."},
         "budget_usd": {"type": "number", "exclusiveMinimum": 0},
         "trials": {"type": "integer", "minimum": 1, "default": 3},
         "token_budget": {"type": "integer", "minimum": 1, "default": 1000000},
         "max_rounds": {"type": "integer", "minimum": 1, "default": 1},
         "source": {"type": "string", "default": "HEAD"},
         "findings": {"type": "array", "items": {"type": "string"}},
         "skip_diagnostic": {"type": "boolean", "default": False,
                             "description": "Reuse supplied saved findings to investigate; run only the fresh matched A/B after repair. Requires nonempty findings."},
         "model": {"type": "string", "default": "openrouter/anthropic/claude-haiku-4.5"},
         "per_trial_cost": {"type": "number", "exclusiveMinimum": 0, "default": 0.5},
         "scan_reserve": {"type": "number", "exclusiveMinimum": 0, "default": 1.0,
                          "description": "Conservative model-dollar reservation per arm for all tells judging."},
         "repair_budget": {"type": "number", "exclusiveMinimum": 0, "default": 2.0,
                           "description": "Combined coding and independent review reservation."},
         "coding_model": {"type": "string", "default": "openrouter/anthropic/claude-sonnet-4.5"},
         "review_model": {"type": "string", "default": "openrouter/anthropic/claude-sonnet-4.5"},
         "max_turns": {"type": "integer", "minimum": 1, "default": 10000},
         "time_limit": {"type": "integer", "minimum": 1, "default": 36000},
         "sim_weeks": {"type": "number", "exclusiveMinimum": 0, "default": 3},
         "scenario": {"type": "string", "default": "train_refund_late"},
         "judge": {"type": "string", "default": "openrouter/anthropic/claude-opus-5.5"}}}},
    *[{"name": f"realism_{name}", "description": description, "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["job_id"],
        "properties": {"job_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}}}
      for name, description in [
          ("status", "Read durable progress and model-spend reservations, including interrupted jobs."),
          ("results", "Read saved result and artifact location; does not rerun paid stages."),
          ("cancel", "Cancel only this job's worker and commands. Preserve artifacts and outstanding spend reservations.")]],
]


class Server:
    def __init__(self, manager: JobManager):
        self.manager = manager
        self.initialized = False

    def handle(self, message: dict[str, Any]) -> dict | None:
        request_id = message.get("id")
        notification = "id" not in message
        if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": "Invalid request"}}
        method, params = message["method"], message.get("params", {})
        if notification:
            return None
        try:
            if method == "initialize":
                version = params.get("protocolVersion")
                self.initialized = True
                result = {"protocolVersion": version if version in SUPPORTED else VERSION,
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "refund-realism-loop", "version": "0.1.0"}}
            elif method == "ping":
                result = {}
            elif not self.initialized:
                raise ValueError("Initialize MCP before calling tools")
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                from jsonschema import validate
                name, arguments = params.get("name"), params.get("arguments", {})
                descriptor = next((tool for tool in TOOLS if tool["name"] == name), None)
                if descriptor is None:
                    raise ValueError("Unknown tool")
                try:
                    validate(arguments, descriptor["inputSchema"])
                    operation = getattr(self.manager, name.removeprefix("realism_"))
                    answer = operation(**arguments)
                    result = {"content": [{"type": "text", "text": json.dumps(answer)}]}
                except Exception as error:  # noqa: BLE001 -- RPC boundary must not kill other durable jobs
                    from jsonschema import ValidationError
                    # Validation errors print only our schema summary, never the supplied instance.
                    detail = "Invalid tool arguments" if isinstance(error, ValidationError) else str(error)
                    if not isinstance(error, (ValueError, FileNotFoundError, ValidationError)):
                        detail = f"Operation failed ({type(error).__name__}); inspect saved job artifacts"
                    result = {"isError": True, "content": [{"type": "text", "text": detail}]}
            else:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except (TypeError, ValueError, AttributeError):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "Invalid request parameters"}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--backend", help="administrator-configured module:run_job Python backend")
    args = parser.parse_args(argv)
    server = Server(JobManager(args.repo, backend=args.backend))
    for line in sys.stdin:
        try:
            if len(line) > 2_000_000:
                raise ValueError("Request too large")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise TypeError("Request must be an object")
            response = server.handle(message)
        except (TypeError, ValueError, UnicodeError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON request"}}
        if response is not None:
            print(json.dumps(response), flush=True)
    # Exiting the MCP transport deliberately leaves detached jobs running.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

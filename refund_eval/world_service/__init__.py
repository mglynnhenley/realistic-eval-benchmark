"""The world as a service: one trial's simulated world behind MCP, for the Harbor world sidecar.

- `trial.py`: `WorldTrial` sets up the world as the Inspect harness does, runs tool calls
  and turn boundaries, and reads state for graders.
- `tools.py`: the mail and search tools as the Inspect adapter runs them, and `schedule_wakeup`.
- `store.py`: the in-memory trial store, written to `state.json` and `events.jsonl`.
- `server.py`: `/mcp` and `POST /turn` for the agent side; admin reads on 127.0.0.1.
- `__main__.py`: `python -m refund_eval.world_service --help`.

The Inspect path (`refund_eval.task`) does not use this package.
"""

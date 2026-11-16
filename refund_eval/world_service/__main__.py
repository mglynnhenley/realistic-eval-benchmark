"""Run one trial's world service: `python -m refund_eval.world_service --scenario train_refund_late ...`."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from refund_eval.world_service.settings import DEFAULT_OUTPUT_DIR, WorldSettings

ADMIN_TOKEN_ENV = "WORLD_ADMIN_TOKEN"
TURN_TOKEN_ENV = "WORLD_TURN_TOKEN"


def parser() -> argparse.ArgumentParser:
    defaults = WorldSettings()
    p = argparse.ArgumentParser(
        prog="python -m refund_eval.world_service",
        description="Serve one trial's simulated world: the agent's tools over MCP at :PORT/mcp, the harness's "
                    f"turn boundary at POST :PORT/turn (bearer ${TURN_TOKEN_ENV}), and admin reads at "
                    f"127.0.0.1:ADMIN_PORT (bearer ${ADMIN_TOKEN_ENV}). Writes state.json and events.jsonl to "
                    "the output directory. World-side models are called on OpenRouter (OPENROUTER_API_KEY).",
    )
    p.add_argument("--scenario", default=defaults.scenario, help="Scenario name or YAML path (default: %(default)s)")
    p.add_argument("--seed", type=int, default=None, help="Seed for simulated randomness (default: drawn, recorded)")
    p.add_argument("--sim-weeks", type=float, default=defaults.sim_weeks, help="Simulated session length (default: %(default)s)")
    p.add_argument("--max-turns", type=int, default=defaults.max_turns, help="Turn cap (default: %(default)s)")
    p.add_argument("--checkin-hours", type=int, nargs="+", default=list(defaults.checkin_hours),
                   help="London hours of the daily check-ins (default: %(default)s)")
    p.add_argument("--research-mode", choices=["auto", "live", "saved"], default=defaults.research_mode)
    p.add_argument("--data-mode", choices=["fixture", "hybrid"], default=defaults.data_mode,
                   help="Published data: fixture (authored search results) or hybrid (live Exa search)")
    p.add_argument("--inbox", default=None, help="Inbox JSON (default: the scenario's bundled inbox)")
    p.add_argument("--simulator-model", default=defaults.simulator_model)
    p.add_argument("--grader-model", default=defaults.grader_model)
    p.add_argument("--company-simulator", default=None, help="Simulator kind for every organisation (e.g. concordia)")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Where state.json and events.jsonl go (default: %(default)s)")
    p.add_argument("--host", default="0.0.0.0", help="Agent-facing host (default: %(default)s)")
    p.add_argument("--port", type=int, default=8000, help="Agent-facing port (default: %(default)s)")
    p.add_argument("--admin-port", type=int, default=9000, help="Admin port, always on 127.0.0.1 (default: %(default)s)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    admin_token, turn_token = os.environ.get(ADMIN_TOKEN_ENV), os.environ.get(TURN_TOKEN_ENV)
    if not admin_token or not turn_token:
        print(f"Set {ADMIN_TOKEN_ENV} and {TURN_TOKEN_ENV}.", file=sys.stderr)
        return 2
    settings = WorldSettings(
        scenario=args.scenario, seed=args.seed, sim_weeks=args.sim_weeks, max_turns=args.max_turns,
        checkin_hours=tuple(args.checkin_hours), research_mode=args.research_mode, data_mode=args.data_mode,
        inbox=args.inbox, simulator_model=args.simulator_model, grader_model=args.grader_model,
        company_simulator=args.company_simulator, output_dir=args.output_dir,
    )

    async def run() -> None:
        from refund_eval.world_service.server import serve_until_stopped
        from refund_eval.world_service.trial import WorldTrial

        async with WorldTrial(settings) as trial:
            await serve_until_stopped(trial, host=args.host, port=args.port, admin_port=args.admin_port,
                                      turn_token=turn_token, admin_token=admin_token)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Harbor custom agent: one Inspect benchmark worker per environment."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from contextlib import suppress
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from refund_eval.harbor.config import TrialRequest, TrialSettings
from refund_eval.killswitch import kill_reason


class RefundInspectAgent(BaseAgent):
    options_model = TrialSettings

    @staticmethod
    def name() -> str:
        return "refund-inspect"

    def version(self) -> str:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        if reason := kill_reason():
            raise RuntimeError(f"Trial blocked by kill switch: {reason}")
        result = await environment.exec(
            "python -c 'from refund_eval.task import impossible_refund; "
            "from refund_eval.scenario.schema import load_scenario; load_scenario()'",
            cwd="/app", timeout_sec=30,
        )
        if result.return_code:
            raise RuntimeError(f"Inspect runtime is unavailable: {result.stderr}")

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        request = TrialRequest(
            instruction=instruction, model=self.model_name or "",
            settings=self.options, trial_id=self.session_id or self.logs_dir.parent.name,
        )
        with tempfile.TemporaryDirectory(prefix="refund-harbor-") as directory:
            request_path = Path(directory) / "request.json"
            request_path.write_text(request.model_dump_json(indent=2), encoding="utf-8")
            await environment.upload_file(request_path, "/app/request.json")
        execution = asyncio.create_task(environment.exec(
            "python -m refund_eval.harbor.worker --request /app/request.json "
            "> /logs/agent/worker.log 2>&1",
            cwd="/app", env={**self.extra_env, "REFUND_EVAL_DASHBOARD_RUN_DIR": "/logs/agent/progress"},
            timeout_sec=request.settings.time_limit + 600,
        ))
        try:
            killed = False
            next_poll = asyncio.get_running_loop().time() + 2
            while not execution.done():
                if not killed and kill_reason():
                    await environment.exec("mkdir -p /app/.context && touch /app/.context/KILL", timeout_sec=10)
                    killed = True
                await asyncio.wait({execution}, timeout=0.5)
                if asyncio.get_running_loop().time() >= next_poll and not execution.done():
                    # Telemetry must never fail or indefinitely delay the trial.
                    temporary = self.logs_dir / "progress.tmp"
                    try:
                        await asyncio.wait_for(environment.download_file(
                            "/logs/agent/progress/snapshot.json", temporary,
                        ), timeout=60)
                        json.loads(temporary.read_text())
                        temporary.replace(self.logs_dir / "progress.json")
                    except Exception:
                        logging.getLogger(__name__).debug("Live snapshot not available yet", exc_info=True)
                    next_poll = asyncio.get_running_loop().time() + 2
            result = await execution
            if result.return_code:
                raise RuntimeError(f"Benchmark worker exited {result.return_code}; see agent/worker.log")
            with tempfile.TemporaryDirectory(prefix="refund-result-") as directory:
                path = Path(directory) / "result.json"
                await environment.download_file("/logs/artifacts/refund/result.json", path)
                context.metadata = json.loads(path.read_text())
                usage_path = Path(directory) / "usage.json"
                await environment.download_file("/logs/artifacts/refund/usage.json", usage_path)
                usage = json.loads(usage_path.read_text())
                totals = list(usage["models"].values())
                context.n_input_tokens = sum(u["input_tokens"] for u in totals)
                context.n_output_tokens = sum(u["output_tokens"] for u in totals)
                context.n_cache_tokens = sum(u.get("input_tokens_cache_read") or 0 for u in totals)
                costs = [u["total_cost"] for u in totals]
                context.cost_usd = sum(costs) if costs and all(c is not None for c in costs) else None
                context.metadata["cost_scope"] = "target, simulator and its claim extractor (grader); see usage.json for roles"
        finally:
            if not execution.done():
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
                # Harbor owns teardown after cancellation/timeout and deletes
                # the sandbox. Do not start a replacement attempt here.

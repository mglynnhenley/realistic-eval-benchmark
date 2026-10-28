"""Harbor custom agent: one Inspect benchmark worker per environment."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
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
        if self.options.trial_deadlines is not None:
            return await self._run_bounded(instruction, environment, context)
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
                context.metadata["cost_scope"] = "target, simulator and its consistency checker (grader); see usage.json for roles"
        finally:
            if not execution.done():
                execution.cancel()
                with suppress(asyncio.CancelledError):
                    await execution
                # Harbor owns teardown after cancellation/timeout and deletes
                # the sandbox. Do not start a replacement attempt here.


    async def _run_bounded(self, instruction, environment, context) -> None:
        from refund_eval.harbor.deadlines import (
            HOST_START_ENV,
            exec_timeout_seconds,
            host_start,
            remaining,
            settle,
            validate_deadlines,
        )

        request = TrialRequest(instruction=instruction, model=self.model_name or "", settings=self.options,
                               trial_id=self.session_id or self.logs_dir.parent.name)
        bounds = validate_deadlines(request.settings.trial_deadlines, request.settings.time_limit)
        start = time.monotonic()
        command_start = host_start(os.environ.get(HOST_START_ENV))
        if start >= command_start + bounds["setup_seconds"]:
            raise TimeoutError("Aggregate setup budget exhausted before worker launch")
        agent_end = min(start + bounds["agent_seconds"], command_start + bounds["setup_seconds"] + bounds["agent_seconds"])
        worker_end = min(start + bounds["worker_hard_seconds"], agent_end - bounds["collect_seconds"])
        execution, original = None, None
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="refund-harbor-") as directory:
                path = Path(directory) / "request.json"
                path.write_text(request.model_dump_json(indent=2))
                async with asyncio.timeout(remaining(worker_end)):
                    await environment.upload_file(path, "/app/request.json")
            allowance = remaining(worker_end)
            if allowance <= bounds["flush_seconds"]:
                raise TimeoutError("No worker execution budget remains")
            # Durations only cross into the sandbox, never the host epoch.
            execution = asyncio.create_task(environment.exec(
                "exec python -m refund_eval.harbor.worker --request /app/request.json "
                f"--worker-seconds {allowance:.6f} > /logs/agent/worker.log 2>&1",
                cwd="/app", env={**self.extra_env, "REFUND_EVAL_DASHBOARD_RUN_DIR": "/logs/agent/progress"},
                timeout_sec=exec_timeout_seconds(worker_end),
            ))
            killed = False
            next_poll = time.monotonic() + 2
            while not execution.done():
                if not remaining(worker_end):
                    raise TimeoutError("Owned worker hard deadline reached")
                if not killed and kill_reason():
                    async with asyncio.timeout(min(10, remaining(worker_end))):
                        await environment.exec("mkdir -p /app/.context && touch /app/.context/KILL",
                                               timeout_sec=exec_timeout_seconds(worker_end, 10))
                    killed = True
                await asyncio.wait({execution}, timeout=min(.5, remaining(worker_end)))
                if time.monotonic() >= next_poll and not execution.done():
                    temporary = self.logs_dir / "progress.tmp"
                    try:
                        async with asyncio.timeout(min(2, remaining(worker_end))):
                            await environment.download_file("/logs/agent/progress/snapshot.json", temporary)
                        json.loads(temporary.read_text())
                        temporary.replace(self.logs_dir / "progress.json")
                    except Exception:
                        logging.getLogger(__name__).debug("Live snapshot not available yet", exc_info=True)
                    next_poll = time.monotonic() + 2
            result = await execution
            if result.return_code:
                raise RuntimeError(f"Benchmark worker exited {result.return_code}; see agent/worker.log")
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- preserve primary error and collect best-effort evidence
            original = exc
        finally:
            cleanup = asyncio.create_task(self._collect_bounded(
                environment, execution, worker_end=worker_end, agent_end=agent_end,
                flush_seconds=bounds["flush_seconds"], collect_seconds=bounds["collect_seconds"],
                stop=original is not None,
            ))
            def cancelled_during_collection():
                nonlocal original
                if original is None:
                    original = asyncio.CancelledError()

            try:
                await settle(cleanup, agent_end, on_cancel=cancelled_during_collection)
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- preserve primary error and collect best-effort evidence
                (self.logs_dir / "collection-error.json").write_text(json.dumps({
                    "error_type": type(exc).__name__, "cost_usd": None, "complete": False}))
                if not cleanup.done():
                    cleanup.cancel()
                if original is None:
                    original = exc
        if original is not None:
            raise original
        # Success context is derived only from the strict, collected marker.
        native = self.logs_dir.parent / "artifacts/logs/artifacts/refund"
        metadata = json.loads((native / "result.json").read_text())
        usage = json.loads((native / "usage.json").read_text())
        if metadata.get("status") != "success" or not metadata.get("passed"):
            raise RuntimeError("Missing successful worker completion marker")
        context.metadata = metadata
        totals = list(usage["models"].values())
        context.n_input_tokens = sum(u["input_tokens"] for u in totals)
        context.n_output_tokens = sum(u["output_tokens"] for u in totals)
        context.n_cache_tokens = sum(u.get("input_tokens_cache_read") or 0 for u in totals)
        costs = [u.get("total_cost") for u in totals]
        import math

        context.cost_usd = (sum(costs) if costs and all(type(c) in (int, float) and math.isfinite(c) and c >= 0
                                                     for c in costs) else None)
        context.metadata["cost_scope"] = "target, simulator and consistency checker; see usage.json"

    async def _collect_bounded(self, environment, execution, *, worker_end, agent_end,
                               flush_seconds, collect_seconds, stop):
        from refund_eval.harbor.deadlines import (
            bounded,
            exec_timeout_seconds,
            remaining,
            settle,
        )

        errors = []
        flush_end = min(worker_end, time.monotonic() + flush_seconds, agent_end - collect_seconds)
        def known_exit():
            if execution is None or not execution.done():
                return False
            try:
                return execution.result().return_code is not None
            except (Exception, asyncio.CancelledError):  # noqa: BLE001 -- transport failure does not prove worker exit
                return False

        if stop and execution is not None and not known_exit():
            try:
                await bounded(environment.exec(
                    "python -m refund_eval.harbor.worker --request /app/request.json --signal-owned TERM",
                    cwd="/app", timeout_sec=exec_timeout_seconds(flush_end, 10)), min(flush_end, time.monotonic() + 10))
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- preserve primary error and collect best-effort evidence
                errors.append({"phase": "stop", "error_type": type(exc).__name__})
            try:
                await settle(execution, flush_end)
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- preserve primary error and collect best-effort evidence
                errors.append({"phase": "flush", "error_type": type(exc).__name__})
            if not known_exit():
                # An exec transport error can finish its task while the worker
                # remains alive. Preserve one bounded flush interval, then
                # escalate using the same exact identity, never broad cleanup.
                await asyncio.sleep(remaining(flush_end))
                try:
                    await bounded(environment.exec(
                        "python -m refund_eval.harbor.worker --request /app/request.json --signal-owned KILL",
                        cwd="/app", timeout_sec=exec_timeout_seconds(agent_end, 10)),
                        min(agent_end, time.monotonic() + 10))
                except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- preserve primary error and collect best-effort evidence
                    errors.append({"phase": "owned_kill", "error_type": type(exc).__name__})
        if execution is not None and not execution.done():
            execution.cancel()  # release transport after owned stop, never a replacement attempt
        collect_end = min(agent_end, time.monotonic() + collect_seconds)
        native = self.logs_dir.parent / "artifacts/logs/artifacts/refund"
        native.mkdir(parents=True, exist_ok=True)

        async def download(name, awaitable):
            try:
                await bounded(awaitable, collect_end)
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- preserve primary error and collect best-effort evidence
                errors.append({"phase": name, "error_type": type(exc).__name__})

        downloads = [asyncio.create_task(download("native", environment.download_dir("/logs/artifacts/refund", native))),
                     asyncio.create_task(download("worker_log", environment.download_file("/logs/agent/worker.log", self.logs_dir / "worker.log"))),
                     asyncio.create_task(download("progress", environment.download_file("/logs/agent/progress/snapshot.json", self.logs_dir / "progress.json")))]
        await settle(asyncio.gather(*downloads), collect_end)
        (self.logs_dir / "collection.json").write_text(json.dumps({
            "version": 1, "errors": errors, "cost_usd": None,
            "native_complete": (native / "result.json").is_file(),
            "note": "Available artifacts only; failures, pending calls and partial usage remain unknown."}))

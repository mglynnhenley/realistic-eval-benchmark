"""Harbor installed agent for the native refund task: Inspect's react loop in `main`, the world in its sidecar.

Job config: `import_path: refund_eval.harbor.installed_agent:RefundInstalledAgent`, the target as
`model_name`, budgets as kwargs (`RefundAgentOptions`), `OPENROUTER_API_KEY` in the agent env.

- install: the task's `main` image is prebuilt with the package and Inspect; this checks it.
- run: `inspect eval <installed refund_eval.task path>` in world-service mode (`-T world_url=` the task's
  `world` MCP server without `/mcp`; turn token in WORLD_TURN_TOKEN from the task env), with the
  scenario, simulated weeks and data mode from the task env and the budgets from the options.
  The `.eval` log goes to /logs/agent/inspect/, its ATIF conversion to /logs/agent/trajectory.json
  (`refund_eval.harbor.atif`), both collected with the agent logs. While it runs, the host's
  kill switch (`.context/KILL` or REFUND_EVAL_KILL) is forwarded by touching /app/.context/KILL in
  `main`; the agent then stops at its next tool call or turn and tells the world.
- context: target tokens and cost from the trajectory's final metrics (the agent's own model
  calls only; world-side model usage is in the world's state.json). OpenRouter cost uses
  the trajectory's receipt summary, or the collected receipts if no summary is available.

Suite-generated jobs opt in to forwarding the Harbor instruction as the Inspect task's initial user
message and the task ID as sample metadata; default single-task jobs forward neither. Scenario facts
and world-side outcome grading continue to come from the selected scenario.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
from contextlib import suppress
from datetime import date
from pathlib import Path

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import ApiError, BaseInstalledAgent
from harbor.agents.options import InstalledAgentOptions
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext, ModelUsage
from pydantic import Field

from refund_eval.config import BudgetNotice, RunConfig
from refund_eval.harbor.luna_status import NativeStatus
from refund_eval.harbor.private_traces import (
    PRIVATE_AGENT_BUFFER_ROOT,
    PRIVATE_WORLD_LOGS_ROOT,
    PrivateTraceCapture,
    has_private_world_bind,
    private_inspect_data_home,
)
from refund_eval.harbor.target_charges import (
    SOURCE,
    TARGET_CHARGES_DIRECTORY,
    TARGET_CHARGES_ENV,
    target_charge_summary,
)
from refund_eval.killswitch import kill_reason
from refund_eval.published.provider import published_model_args

APP = "/app"
PYTHON = f"{APP}/.venv/bin/python"
INSPECT = f"{APP}/.venv/bin/inspect"
DEFAULT_WORLD_URL = "http://world:8000"
WORLD_SERVER_NAME = "world"  # the task's [[environment.mcp_servers]] entry
AGENT_VERSION = "1.0.0"


class RefundAgentOptions(InstalledAgentOptions):
    """The target's budgets; the world's settings are the task's."""

    history_tokens: int = Field(default=RunConfig.model_fields["history_tokens"].default, ge=0, le=1_000_000,
                                description="Model-visible history in tokens (with a memory tool); 0 shows it all.")
    token_budget: int | None = Field(default=None, gt=0, description="Target tokens including cached input.")
    budget_notice: BudgetNotice = Field(default=RunConfig.model_fields["budget_notice"].default)
    requested_deadline: date | None = None
    cost_limit: float | None = Field(default=None, gt=0, le=1000,
                                     description="Per-trial target-only USD cap (Inspect cost_limit); excludes world-service "
                                                 "model calls. Required for train_refund_late.")
    time_limit: int | None = Field(default=None, ge=1, le=72000, description="Inspect wall-clock limit in seconds.")
    reasoning: bool = Field(default=False, description="Ask OpenRouter to return the target's reasoning.")
    forward_task_instruction: bool = False


class RefundInstalledAgent(BaseInstalledAgent):
    options_model = RefundAgentOptions
    capabilities = AgentCapabilities(atif=True, mcp_servers=True)  # uses the task's `world` server via Inspect

    @staticmethod
    def name() -> str:
        return "refund-inspect-world"

    def version(self) -> str | None:
        return AGENT_VERSION

    async def install(self, environment: BaseEnvironment) -> None:
        if reason := kill_reason():
            raise RuntimeError(f"Trial blocked by kill switch: {reason}")
        await self.exec_as_agent(environment, f"{PYTHON} -c 'import inspect_ai, refund_eval.task'",
                                 cwd=APP, timeout_sec=300)

    def world_url(self) -> str:
        for server in self.mcp_servers:
            if server.name == WORLD_SERVER_NAME and server.url:
                return server.url.removesuffix("/").removesuffix("/mcp")
        return DEFAULT_WORLD_URL

    def inspect_command(self, instruction: str | None = None) -> str:
        """The shell command that runs the trial and converts its log (the world's settings from the task env)."""
        options = self.options
        model = self.model_name or ""
        if not model:
            raise ValueError("Choose the target model (model_name)")
        logs = self.environment_logs_dir.as_posix()
        model_args = {**published_model_args(model), **({"reasoning_enabled": True} if options.reasoning else {})}
        if model.startswith("openrouter/"):
            model_args["max_retries"] = 0
        task_args = {"world_url": self.world_url(), "history_tokens": options.history_tokens,
                     "budget_notice": options.budget_notice}
        suite_instruction = None
        if options.forward_task_instruction:
            if not instruction or not instruction.strip():
                raise ValueError("Suite task forwarding requires a non-empty instruction")
            suite_instruction = instruction.rstrip("\n")
            task_args["task_text"] = suite_instruction
        for name in ("token_budget", "cost_limit", "time_limit"):
            if getattr(options, name) is not None:
                task_args[name] = getattr(options, name)
        if options.requested_deadline is not None:
            task_args["requested_deadline"] = options.requested_deadline.isoformat()
        # Inspect's task loader expects a cwd-relative path with Python 3.14.
        task_file = (f'"$({PYTHON} -c \'import os, refund_eval.task; '
                     'print(os.path.relpath(refund_eval.task.__file__, os.getcwd()))\')"')
        receipt_env = ([f"{TARGET_CHARGES_ENV}={shlex.quote(f'{logs}/{TARGET_CHARGES_DIRECTORY}')}"]
                       if model.startswith("openrouter/") else [])
        parts = [INSPECT, "eval", task_file, "--model", shlex.quote(model),
                 "--max-retries", "0",
                 *(f"-M {key}={json.dumps(value)}" for key, value in model_args.items()),
                 *(['-T task_id="$WORLD_TASK_ID"'] if suite_instruction is not None else []),
                 '-T scenario="$WORLD_SCENARIO"', '-T sim_weeks="$WORLD_SIM_WEEKS"',
                 '-T published_data="$WORLD_DATA_MODE"',
                 *(f"-T {key}={shlex.quote(str(value))}" for key, value in task_args.items()),
                 "--log-dir", f"{logs}/inspect", "--log-format", "eval", "--display", "none",
                 "--max-samples", "1", "--metadata", shlex.quote(f"harbor_trial={self.logs_dir.parent.name}")]
        convert = [PYTHON, "-m", "refund_eval.harbor.atif", f"{logs}/inspect", "--out", f"{logs}/trajectory.json",
                   "--agent-name", self.name(), "--agent-version", AGENT_VERSION]
        run_id = self.logs_dir.parent.name
        launch_env = " ".join([
            f"XDG_DATA_HOME={shlex.quote(str(private_inspect_data_home(run_id, PRIVATE_AGENT_BUFFER_ROOT)))}",
            f"REFUND_EVAL_KILL_FILE={shlex.quote(f'{APP}/.context/KILL')}",
            *receipt_env,
        ])
        runner = " ".join([
            shlex.quote(PYTHON), "-m", "refund_eval.harbor.inspect_snapshotter", "run",
            "--log-dir", shlex.quote(f"{logs}/inspect"), "--", *parts,
        ])
        return (f"mkdir -p {logs}/inspect; {launch_env} {runner} "
                f"> {logs}/inspect.log 2>&1; status=$?; "
                f"{' '.join(convert)} >> {logs}/inspect.log 2>&1; exit $status")

    async def _check_world_stop(self, environment: BaseEnvironment) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            try:
                async with asyncio.timeout(5):
                    if has_private_world_bind(environment):
                        await environment.download_file(f"{PRIVATE_WORLD_LOGS_ROOT}/{state.name}", state)
                    else:
                        await environment.service_download_file(f"/var/log/world/{state.name}", state, service="world")
                    reason = json.loads(state.read_bytes())["stop_reason"]
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"Could not read final world stop reason ({type(exc).__name__})") from None
        if reason in ("model_limit", "model_request_rejected"):
            raise ApiError(f"World stopped: {reason}")

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        if reason := kill_reason():
            raise RuntimeError(f"Trial blocked by kill switch: {reason}")
        capture = PrivateTraceCapture(
            self.logs_dir.parent.name,
            native=True,
        )
        terminal_status = "failed"
        trace_observer = asyncio.create_task(capture.watch(environment))
        execution = asyncio.create_task(self.exec_as_agent(
            environment,
            self.inspect_command(instruction if self.options.forward_task_instruction else None),
            cwd=APP,
        ))
        status = NativeStatus(self.logs_dir, self.model_name)
        observer = asyncio.create_task(status.watch(environment))
        try:
            killed = False
            while not execution.done():
                if not killed and kill_reason():
                    await environment.exec(f"mkdir -p {APP}/.context && touch {APP}/.context/KILL", timeout_sec=30)
                    killed = True
                await asyncio.wait({execution}, timeout=0.5)
            await execution
            await self._check_world_stop(environment)
        except asyncio.CancelledError:
            terminal_status = "cancelled"
            raise
        except Exception:
            terminal_status = "failed"
            raise
        else:
            terminal_status = "agent_succeeded"
        finally:
            try:
                if not execution.done():
                    execution.cancel()
                    with suppress(asyncio.CancelledError):
                        await execution
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
                await status.safe_observe(environment)
            finally:
                trace_observer.cancel()
                await asyncio.gather(trace_observer, return_exceptions=True)
                try:
                    await capture.safe_capture(environment)
                finally:
                    if asyncio.current_task().cancelling():
                        terminal_status = "cancelled"
                    capture.finish(terminal_status, collection_status="unconfirmed")

    def populate_context_post_run(self, context: AgentContext) -> None:
        openrouter = (self.model_name or "").startswith("openrouter/")
        path = self.logs_dir / "trajectory.json"
        if not path.is_file() and not openrouter:
            return
        trajectory = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        totals = trajectory.get("final_metrics") or {}
        metadata = trajectory.get("extra") or {}
        context.n_input_tokens = totals.get("total_prompt_tokens")
        context.n_cache_tokens = totals.get("total_cached_tokens")
        context.n_output_tokens = totals.get("total_completion_tokens")
        context.metadata = {**metadata,
                            "cost_scope": "target model only; world-side model use is in the world's state.json"}
        if openrouter:
            receipts = metadata.get("target_charges")
            # The converter sees all receipts before Harbor collects a potentially filtered subset.
            if not isinstance(receipts, dict) or receipts.get("source") != SOURCE:
                receipts = target_charge_summary(self.logs_dir / TARGET_CHARGES_DIRECTORY)
            context.cost_usd = receipts.get("actual_cost_usd")
            context.metadata["target_charges"] = receipts
            if totals:
                context.model_usage = {self.model_name: ModelUsage(
                        n_input_tokens=context.n_input_tokens or 0, n_cache_tokens=context.n_cache_tokens or 0,
                        n_output_tokens=context.n_output_tokens or 0, cost_usd=context.cost_usd)}
        else:
            context.cost_usd = totals.get("total_cost_usd")

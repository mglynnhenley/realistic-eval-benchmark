"""The installed agent with a scripted `mockllm/model` target (test only; Harbor side, no Inspect import).

Job config: `import_path: refund_eval.parity.harbor_agent:ScriptedRefundAgent`, `model_name: mockllm/model`,
kwargs `script: <parity script name>` plus the usual budgets. It runs the installed agent's own command,
with Inspect's CLI replaced by `parity.inspect_cli`, which answers the model from the script.
"""

from __future__ import annotations

from pydantic import Field

from refund_eval.harbor.installed_agent import (
    INSPECT,
    PYTHON,
    RefundAgentOptions,
    RefundInstalledAgent,
)


class ScriptedAgentOptions(RefundAgentOptions):
    script: str = Field(description="Parity script name (refund_eval.parity.scripts.SCRIPTS)")


class ScriptedRefundAgent(RefundInstalledAgent):
    options_model = ScriptedAgentOptions

    @staticmethod
    def name() -> str:
        return "refund-inspect-world-scripted"

    def inspect_command(self) -> str:
        if not (self.model_name or "").startswith("mockllm/"):
            raise ValueError("The scripted agent needs a mockllm target")
        command = super().inspect_command()
        run = f"{INSPECT} eval "
        if command.count(run) != 1:
            raise ValueError("Unexpected installed agent command")
        return command.replace(run, f"{PYTHON} -m refund_eval.parity.inspect_cli {self.options.script} eval ")

"""Inspect's CLI with `mockllm/model` answering from a parity script (test only).

    python -m refund_eval.parity.inspect_cli SCRIPT eval refund_eval/task.py --model mockllm/model ...

The Harbor `main` container runs this in place of `inspect` (`parity.harbor_agent`), so the scripted
target makes the same calls there as in-process. Everything after SCRIPT goes to Inspect unchanged.
"""

from __future__ import annotations

import sys


def use_script(name: str) -> None:
    """Make every `mockllm` model without custom outputs answer from parity script `name`."""
    from inspect_ai.model._providers import mockllm

    from refund_eval.parity.scripts import SCRIPTS

    script = SCRIPTS[name]
    original = mockllm.MockLLM.__init__

    def init(self, model_name, base_url=None, api_key=None, config=None, custom_outputs=None, **model_args):
        from inspect_ai.model import GenerateConfig

        original(self, model_name, base_url, api_key, config or GenerateConfig(),
                 script if custom_outputs is None else custom_outputs, **model_args)

    mockllm.MockLLM.__init__ = init


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        raise SystemExit("usage: python -m refund_eval.parity.inspect_cli SCRIPT <inspect arguments>")
    use_script(argv[0])
    from inspect_ai._cli.main import main as inspect_main

    sys.argv = ["inspect", *argv[1:]]
    inspect_main()


if __name__ == "__main__":
    main()

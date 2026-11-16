"""The Harbor world image's entrypoint: world-service settings from the container environment.

    python -m refund_eval.harbor.world_entrypoint

Reads WORLD_SCENARIO, WORLD_SIM_WEEKS, WORLD_MAX_TURNS, WORLD_DATA_MODE, WORLD_RESEARCH_MODE,
WORLD_SIMULATOR_MODEL, WORLD_GRADER_MODEL and WORLD_COMPANY_SIMULATOR (the task's compose file
sets them), then runs `python -m refund_eval.world_service` on :8000 (admin on 127.0.0.1:9000) with
the browser as a subprocess and output in WORLD_OUTPUT_DIR (default /var/log/world). The service
itself reads WORLD_TURN_TOKEN, WORLD_ADMIN_TOKEN, OPENROUTER_API_KEY and REFUND_EVAL_EXA_API_KEY.

Seed: Harbor has none, so each trial's is derived from the task version and the trial name
(WORLD_TASK_VERSION, WORLD_TRIAL: the compose project, `<trial name>__env`) with `trial_seed`.
WORLD_SEED overrides it; with neither the world draws one. The world records its seed; this
also writes `seed.json` (seed and how it was chosen) to the output directory.

Test only: REFUND_EVAL_FAKE_WORLD_MODELS=1 answers world-side model calls with fixed replies
(every company asks for evidence; every consistency check passes) and refuses to start when any
API key is set, so a local run with it cannot reach a paid model. With the value `follow_up`,
every company reply also promises a follow-up FAKE_FOLLOW_UP_HOURS later.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from refund_eval.world_service.settings import DEFAULT_OUTPUT_DIR

FAKE_MODELS_ENV = "REFUND_EVAL_FAKE_WORLD_MODELS"
_CREDENTIAL_NAME = re.compile(r"API_KEY|_KEY$|OPENROUTER|OPEN_ROUTER|ANTHROPIC|OPENAI|EXA_|SUPABASE", re.IGNORECASE)
# Environment variable -> world service flag.
FLAGS = {
    "WORLD_SCENARIO": "--scenario", "WORLD_SIM_WEEKS": "--sim-weeks", "WORLD_MAX_TURNS": "--max-turns",
    "WORLD_DATA_MODE": "--data-mode", "WORLD_RESEARCH_MODE": "--research-mode",
    "WORLD_SIMULATOR_MODEL": "--simulator-model", "WORLD_GRADER_MODEL": "--grader-model",
    "WORLD_COMPANY_SIMULATOR": "--company-simulator",
}
FAKE_ACTION = {
    "decision": "evidence_required", "decision_basis": "We need supporting evidence.",
    "summary": "Asked for evidence.", "reply": True, "reply_after_hours": 2, "subject": "Your request",
    "body": "Please send supporting evidence.", "page_body": "", "payments": [], "case_status": "open",
}
FAKE_FOLLOW_UP_HOURS = 30
FAKE_MODES = {"1": None, "follow_up": FAKE_FOLLOW_UP_HOURS}  # REFUND_EVAL_FAKE_WORLD_MODELS -> follow-up hours


def trial_seed(task_version: str, trial: str) -> int:
    """A trial's seed: the first 4 bytes of sha256("<task version>\\n<trial name>")."""
    return int.from_bytes(hashlib.sha256(f"{task_version}\n{trial}".encode()).digest()[:4], "big")


def trial_name(compose_project: str) -> str:
    """Harbor's trial name from its compose project (`<trial name>__env`, lower case)."""
    return compose_project.removesuffix("__env")


def choose_seed(env: dict[str, str]) -> dict:
    explicit = env.get("WORLD_SEED", "").strip()
    if explicit:
        return {"seed": int(explicit), "source": "explicit"}
    version, trial = env.get("WORLD_TASK_VERSION", "").strip(), env.get("WORLD_TRIAL", "").strip()
    if version and trial:
        trial = trial_name(trial)
        return {"seed": trial_seed(version, trial), "source": "derived", "task_version": version, "trial": trial}
    return {"seed": None, "source": "drawn by the world (recorded in state.json)"}


def world_argv(env: dict[str, str], seed: int | None) -> list[str]:
    argv = ["--browser-runtime", "subprocess", "--output-dir", env.get("WORLD_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR]
    for name, flag in FLAGS.items():
        if env.get(name, "").strip():
            argv += [flag, env[name].strip()]
    if seed is not None:
        argv += ["--seed", str(seed)]
    return argv


def credentials_set(env: dict[str, str]) -> list[str]:
    """Names (never values) of credential-like variables that are set."""
    return sorted(name for name, value in env.items() if value.strip() and _CREDENTIAL_NAME.search(name))


class FakeWorldModels:
    """Fixed world-side replies for a free local run (see the module docstring)."""

    def __init__(self, follow_up_hours: float | None = None) -> None:
        self.action = FAKE_ACTION if follow_up_hours is None else {
            **FAKE_ACTION, "follow_up_in_hours": follow_up_hours, "follow_up_plan": "Review the case and reply."}

    def model_name(self, role: str) -> str:
        return f"fake/{role}"

    async def complete(self, role, messages, *, schema=None, **kwargs):
        from refund_eval.models.client import Completion, Message

        replies = {"open_world_action": self.action, "response_consistency": {"consistent": True, "reason": "ok"}}
        name = schema.name if schema else None
        if name not in replies:
            raise RuntimeError(f"Fake world models have no reply for {role} schema {name!r}")
        text = json.dumps(replies[name])
        return Completion(text=text, message=Message("assistant", text), stop_reason="stop", model=f"fake/{role}")


def main() -> int:
    env = dict(os.environ)
    model_client = None
    if env.get(FAKE_MODELS_ENV) in FAKE_MODES:
        found = credentials_set(env)
        if found:
            print(f"{FAKE_MODELS_ENV}={env[FAKE_MODELS_ENV]} refuses to start while credentials are set: "
                  f"{', '.join(found)}", file=sys.stderr)
            return 2
        model_client = FakeWorldModels(FAKE_MODES[env[FAKE_MODELS_ENV]])
    seed = choose_seed(env)
    output = Path(env.get("WORLD_OUTPUT_DIR") or DEFAULT_OUTPUT_DIR)
    output.mkdir(parents=True, exist_ok=True)
    (output / "seed.json").write_text(json.dumps({**seed, "fake_world_models": model_client is not None}) + "\n")
    from refund_eval.world_service.__main__ import main as serve

    return serve(world_argv(env, seed["seed"]), model_client=model_client)


if __name__ == "__main__":
    raise SystemExit(main())

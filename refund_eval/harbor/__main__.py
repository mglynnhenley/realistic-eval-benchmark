"""Prepare a frozen Harbor task and job for Docker or Modal; launches nothing."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from refund_eval.config import HYBRID_EXA_KEY_ENV
from refund_eval.harbor.config import SNAPSHOT_IN_IMAGE, TrialSettings
from refund_eval.scenario.schema import load_scenario

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = """FROM node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203 AS browser-node
FROM python:3.12-slim-bookworm
COPY --from=browser-node /usr/local/bin/node /usr/local/bin/node
COPY --from=browser-node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers NODE_ENV=production
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy UV_PYTHON=3.12 TIKTOKEN_CACHE_DIR=/opt/tiktoken
WORKDIR /app
COPY source/ /app/
RUN mkdir -p /opt/browser && cp refund_eval/published/browser_runtime/package*.json refund_eval/published/browser_runtime/server.mjs /opt/browser/
WORKDIR /opt/browser
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
RUN ./node_modules/.bin/playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/* /root/.npm
WORKDIR /app
RUN uv sync --frozen --no-dev
RUN uv run --no-sync python -c "import tiktoken; tiktoken.get_encoding('o200k_base'); tiktoken.get_encoding('cl100k_base')"
ENV PATH="/app/.venv/bin:$PATH"
"""


def prepare(
    destination: Path, *, settings: TrialSettings, model: str | None = None,
    models: list[str] | None = None,
    environment: str = "modal", trials: int = 4, concurrency: int = 2,
    secrets: list[str] | None = None, inbox: Path | None = None,
) -> Path:
    from harbor.models.job.config import JobConfig
    from harbor.models.task.task import Task

    if trials < 1 or concurrency < 1:
        raise ValueError("trials and concurrency must be positive")
    if model is not None and models is not None:
        raise ValueError("Specify model or models, not both")
    targets = models if models is not None else [model or "mockllm/model"]
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("Choose at least one model, without duplicates")
    if environment not in {"modal", "docker"}:
        raise ValueError("environment must be modal or docker")
    if secrets and (environment != "modal" or settings.smoke):
        raise ValueError("Named secrets are only used for real Modal trials")
    if (inbox is None) != (settings.inbox_sha256 is None):
        raise ValueError("A Gmail snapshot needs both its file and inbox_sha256")
    if inbox is not None:
        from refund_eval.world.snapshot_store import file_sha256

        if file_sha256(inbox) != settings.inbox_sha256:
            raise ValueError("The Gmail snapshot changed since this batch was configured")
    from refund_eval.harbor.config import TrialRequest

    instruction = load_scenario("train_refund").task_text
    for target_model in targets:
        TrialRequest(instruction=instruction, model=target_model, settings=settings, trial_id="validation")
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite a frozen job: {destination}")

    # Copy version-controlled source and new, non-ignored implementation files.
    # Never upload the checkout root, .env, .secrets, logs, or local node_modules.
    files = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
         "refund_eval", "pyproject.toml", "uv.lock", "tests/fixtures/inbox.json"], cwd=ROOT,
    ).decode().split("\0")
    task_dir = destination / "task"
    source = task_dir / "environment/source"
    for filename in filter(None, files):
        original = ROOT / filename
        if original.is_symlink():
            raise ValueError(f"Source snapshots do not follow symlinks: {filename}")
        if not original.is_file():
            continue
        target = source / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
    if inbox is not None:
        # Only the mail data travels; no Gmail token or connection is uploaded.
        shutil.copyfile(inbox, source / SNAPSHOT_IN_IMAGE)
    (task_dir / "environment/Dockerfile").write_text(DOCKERFILE)
    (task_dir / "instruction.md").write_text(instruction + "\n", encoding="utf-8")
    (task_dir / "task.toml").write_text(
        'version = "1.0"\n\n[agent]\n'
        f'timeout_sec = {settings.time_limit + 600}\n\n'
        '[verifier]\ntimeout_sec = 60\n\n'
        '[environment]\nbuild_timeout_sec = 1800\ncpus = 2\nmemory_mb = 4096\n'
        'workdir = "/app"\n'
        f'network_mode = "{"no-network" if settings.smoke and settings.data_mode == "fixture" else "public"}"\n',
    )
    tests = task_dir / "tests"
    tests.mkdir()
    (tests / "test.sh").write_text(
        '#!/bin/sh\nset -eu\npython -m refund_eval.harbor.verifier\n'
    )
    (tests / "test.sh").chmod(0o755)
    Task(task_dir)  # Validate task layout before writing the runnable job.
    env_kwargs = {}
    if environment == "modal":
        env_kwargs = {"app_name": "refund-eval-harbor", "sandbox_timeout_secs": settings.time_limit + 1800}
        if secrets:
            env_kwargs["secrets"] = secrets
    # Credential values are resolved by Harbor at execution, never serialized here.
    agent_env = {}
    if not settings.smoke and not secrets:
        agent_env = {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"}
        # Opt-in Exa account for hybrid search; otherwise trials stay keyless.
        if settings.data_mode == "hybrid" and os.environ.get(HYBRID_EXA_KEY_ENV):
            agent_env[HYBRID_EXA_KEY_ENV] = "${" + HYBRID_EXA_KEY_ENV + "}"
    job = JobConfig.model_validate({
        "job_name": "refund-inspect-smoke" if settings.smoke else "refund-inspect",
        "jobs_dir": str(destination / "jobs"), "n_attempts": trials,
        "n_concurrent_trials": concurrency, "retry": {"max_retries": 0},
        "environment": {"type": environment, "delete": True, "kwargs": env_kwargs},
        "agents": [{"import_path": "refund_eval.harbor.agent:RefundInspectAgent", "model_name": target_model,
                    "kwargs": settings.model_dump(mode="json"), "env": agent_env} for target_model in targets],
        "tasks": [{"path": str(task_dir)}],
    })
    config = destination / "job.json"
    config.write_text(job.model_dump_json(indent=2) + "\n")
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment", choices=["docker", "modal"], default="modal")
    parser.add_argument("--trials", type=int, default=4, help="Trials per selected model")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--tool-profile", choices=["published"], default="published")
    parser.add_argument("--data-mode", choices=["fixture", "hybrid"], default=None)
    parser.add_argument("--browser-runtime", choices=["subprocess"], default="subprocess")
    parser.add_argument("--model", action="append", help="Inspect model ID; repeat for a multi-model batch")
    parser.add_argument("--simulator-model")
    parser.add_argument("--grader-model")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--history-tokens", type=int, default=TrialSettings.model_fields["history_tokens"].default,
                        help="Model-visible history in tokens, with a memory tool; 0 shows the full history")
    parser.add_argument("--sim-weeks", type=float, default=1)
    parser.add_argument("--cost-limit", type=float, default=1)
    parser.add_argument("--time-limit", type=int, default=1800)
    parser.add_argument("--world-mode", choices=["baseline", "friction", "open"], default="baseline")
    parser.add_argument("--secret", action="append", default=[], help="Modal secret name; repeatable")
    parser.add_argument("--rubrics", type=Path, help="Frozen dashboard rubric snapshot JSON")
    parser.add_argument("--inbox", type=Path, help="Gmail snapshot JSON from refund_eval.world.snapshot")
    args = parser.parse_args()
    if not args.smoke and not args.model:
        parser.error("Choose --smoke or provide --model for a paid trial")
    args.data_mode = args.data_mode or ("fixture" if args.smoke else "hybrid")
    from refund_eval.world.snapshot_store import file_sha256

    settings = TrialSettings(**{name: getattr(args, name) for name in TrialSettings.model_fields
                                if name not in {"rubric_set", "inbox_sha256"}},
                             rubric_set=json.loads(args.rubrics.read_text()) if args.rubrics else None,
                             inbox_sha256=file_sha256(args.inbox) if args.inbox else None)
    config = prepare(
        args.output, settings=settings, models=args.model or ["mockllm/model"],
        environment=args.environment, trials=args.trials, concurrency=args.concurrency, secrets=args.secret,
        inbox=args.inbox,
    )
    print(json.dumps({"config": str(config), "trials_per_model": args.trials,
                      "total_trials": args.trials * len(args.model or ["mockllm/model"]), "concurrency": args.concurrency}))
    print(f"Run with: scripts/harbor harbor run -c {config}")


if __name__ == "__main__":
    main()

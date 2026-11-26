"""Prepare a frozen Harbor task and job for Docker or Modal; launches nothing."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

from refund_eval.config import (
    BUDGET_NOTICES,
    DEFAULT_GRADER_MODEL,
    DEFAULT_ROLE_MODEL,
    HYBRID_EXA_KEY_ENV,
    LATE_REFUND_TOKEN_BUDGET,
    SUPABASE_KEY_ENV,
    SUPABASE_URL_ENV,
    resolve_cost_limits,
)
from refund_eval.harbor.config import (
    DEFAULT_CONCURRENCY,
    DEFAULT_TIME_LIMIT,
    DEFAULT_TRIALS,
    RESUME_IN_IMAGE,
    RESUME_SNAPSHOT_IN_IMAGE,
    SNAPSHOT_IN_IMAGE,
    TrialSettings,
)
from refund_eval.output_paths import validate_private_output
from refund_eval.scenario.schema import load_scenario, scenario_names

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = """FROM node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203 AS browser-node
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
COPY --from=browser-node /usr/local/bin/node /usr/local/bin/node
COPY --from=browser-node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers NODE_ENV=production
# uv 0.11.7 (use a digest-only reference for Modal).
COPY --from=ghcr.io/astral-sh/uv@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy UV_PYTHON=3.12 TIKTOKEN_CACHE_DIR=/opt/tiktoken
RUN printf '%s\\n' 'Types: deb' 'URIs: https://snapshot.debian.org/archive/debian/20261118T000000Z/' 'Suites: bookworm bookworm-updates' 'Components: main' 'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' 'Check-Valid-Until: no' '' 'Types: deb' 'URIs: https://snapshot.debian.org/archive/debian-security/20261118T000000Z/' 'Suites: bookworm-security' 'Components: main' 'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' 'Check-Valid-Until: no' > /etc/apt/sources.list.d/debian.sources
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
    environment: str = "modal", trials: int = DEFAULT_TRIALS, concurrency: int = DEFAULT_CONCURRENCY,
    secrets: list[str] | None = None, inbox: Path | None = None, resume: Path | None = None,
    modal_region: str | None = "uk",
) -> Path:
    from harbor.models.job.config import JobConfig
    from harbor.models.task.task import Task

    if settings.identity_lock is not None:
        raise ValueError(
            "Exact browser artifact locks are supported only for local Docker runs; "
            "Harbor browser artifacts are not identified."
        )
    if settings.trial_deadlines is not None:
        settings = TrialSettings.model_validate(settings.model_dump(mode="json"))
    if settings.trial_deadlines is not None and concurrency != trials * len(models or ([model] if model else [])):
        raise ValueError("Bounded lifecycle requires one concurrent wave")
    if trials < 1 or concurrency < 1:
        raise ValueError("trials and concurrency must be positive")
    if model is not None and models is not None:
        raise ValueError("Specify model or models, not both")
    targets = models if models is not None else [model] if model else []
    if settings.trial_deadlines is not None and len(targets) != 1:
        raise ValueError("Bounded lifecycle supports exactly one target model")
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("Choose at least one model, without duplicates")
    if environment not in {"modal", "docker"}:
        raise ValueError("environment must be modal or docker")
    if secrets and environment != "modal":
        raise ValueError("Named secrets are only used for Modal trials")
    if (inbox is None) != (settings.inbox_sha256 is None):
        raise ValueError("A Gmail snapshot needs both its file and inbox_sha256")
    if inbox is not None:
        from refund_eval.world.snapshot_store import file_sha256

        if file_sha256(inbox) != settings.inbox_sha256:
            raise ValueError("The Gmail snapshot changed since this batch was configured")
    if (resume is None) != (settings.resume_sha256 is None):
        raise ValueError("Resuming needs both the trial log and resume_sha256")
    if resume is not None:
        from refund_eval.world.snapshot_store import file_sha256

        if file_sha256(resume) != settings.resume_sha256:
            raise ValueError("The trial log to resume changed since this batch was configured")
        if len(targets) != 1 or trials != 1:
            raise ValueError("Resuming continues one trial: choose one model and one trial")
    from refund_eval.harbor.config import TrialRequest

    instruction = load_scenario(settings.scenario).task_text
    caps = resolve_cost_limits(settings.cost_limit, targets, scenario=settings.scenario)
    target_settings = {target: settings.model_copy(update={"cost_limit": caps[target]}) for target in targets}
    for target_model in targets:
        TrialRequest(instruction=instruction, model=target_model, settings=target_settings[target_model], trial_id="validation")
    destination = validate_private_output(destination)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite a frozen job: {destination}")

    # Copy version-controlled source and new, non-ignored implementation files.
    # Never upload the checkout root, .env, .secrets, logs, or local node_modules.
    files = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
         "refund_eval", "scout", "pyproject.toml", "uv.lock", ".python-version", "README.md", "LICENSE"], cwd=ROOT,
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
    from refund_eval.realism.track import freeze_source_manifest

    freeze_source_manifest(source)
    if inbox is not None:
        # Only the mail data travels; no Gmail token or connection is uploaded.
        shutil.copyfile(inbox, source / SNAPSHOT_IN_IMAGE)
    if resume is not None:
        shutil.copyfile(resume, source / (RESUME_SNAPSHOT_IN_IMAGE if resume.suffix == ".json" else RESUME_IN_IMAGE))
    (task_dir / "environment/Dockerfile").write_text(DOCKERFILE)
    (task_dir / "instruction.md").write_text(instruction + "\n", encoding="utf-8")
    (task_dir / "task.toml").write_text(
        'version = "1.0"\n\n[agent]\n'
        f'timeout_sec = {settings.trial_deadlines["agent_seconds"] if settings.trial_deadlines else settings.time_limit + 600}\n\n'
        '[verifier]\ntimeout_sec = 60\n\n'
        '[environment]\nbuild_timeout_sec = 1800\ncpus = 2\nmemory_mb = 4096\n'
        'workdir = "/app"\n'
        'network_mode = "public"\n',
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
        env_kwargs = {"app_name": "refund-eval-harbor", "sandbox_timeout_secs": (settings.trial_deadlines["sandbox_seconds"] if settings.trial_deadlines else settings.time_limit + 1800)}
        if modal_region is not None:
            env_kwargs["region"] = modal_region
        if secrets:
            env_kwargs["secrets"] = secrets
    # Credential values are resolved by Harbor at execution, never serialized here.
    agent_env = {}
    if not secrets:
        agent_env = {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"}
        # Opt-in Exa account for hybrid search; otherwise trials stay keyless.
        if settings.data_mode == "hybrid" and os.environ.get(HYBRID_EXA_KEY_ENV):
            agent_env[HYBRID_EXA_KEY_ENV] = "${" + HYBRID_EXA_KEY_ENV + "}"
        # Trials upload their own transcripts when Supabase is configured here.
        if os.environ.get(SUPABASE_URL_ENV) and os.environ.get(SUPABASE_KEY_ENV):
            agent_env.update({name: "${" + name + "}" for name in (SUPABASE_URL_ENV, SUPABASE_KEY_ENV)})
    job = JobConfig.model_validate({
        "job_name": "refund-inspect",
        "jobs_dir": str(destination / "jobs"), "n_attempts": trials,
        "n_concurrent_trials": concurrency, "retry": {"max_retries": 0},
        "environment": {"type": environment, "delete": True, "kwargs": env_kwargs},
        "agents": [{"import_path": "refund_eval.harbor.agent:RefundInspectAgent", "model_name": target_model,
                    "kwargs": target_settings[target_model].model_dump(mode="json"), "env": agent_env} for target_model in targets],
        "tasks": [{"path": str(task_dir)}],
    })
    config = destination / "job.json"
    config.write_text(job.model_dump_json(indent=2) + "\n")
    return config


async def run_bounded_job(path: Path) -> None:
    """Use Harbor's existing hooks to charge all setup to one aggregate budget."""
    import asyncio
    import signal
    import time

    from harbor.job import Job
    from harbor.models.job.config import JobConfig

    from refund_eval.harbor.deadlines import (
        HOST_START_ENV,
        host_start,
        settle,
        validate_deadlines,
    )

    config = JobConfig.model_validate_json(path.read_text())
    if (len(config.agents) != 1 or len(config.tasks) != 1 or config.n_concurrent_trials != config.n_attempts
            or config.retry.max_retries != 0):
        raise ValueError("Bounded job requires one model, one concurrent wave and zero retries")
    settings = TrialSettings.model_validate(config.agents[0].kwargs)
    bounds = validate_deadlines(settings.trial_deadlines, settings.time_limit)
    previous_anchor = os.environ.get(HOST_START_ENV)
    start = host_start(previous_anchor)
    os.environ[HOST_START_ENV] = str(start)  # local adapter only; never forwarded to worker
    hard = start + bounds["command_hard_seconds"]
    setup_due = start + bounds["setup_seconds"]
    ready = asyncio.Event()
    started = set()

    async def agent_start(event):
        if time.monotonic() >= setup_due:
            raise TimeoutError("Aggregate setup budget exhausted")
        started.add(str(event.trial_id))
        if len(started) == config.n_attempts:
            ready.set()

    async def run():
        job = await Job.create(config)
        job.on_agent_started(agent_start)
        return await job.run()

    task = asyncio.create_task(run())
    loop = asyncio.get_running_loop()
    stopped = False
    stopped_event = asyncio.Event()
    stop_due = hard

    def stop():
        nonlocal stopped, stop_due
        if not stopped:
            stopped = True
            stop_due = min(hard, time.monotonic() + bounds["termination_seconds"])
            stopped_event.set()
            task.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)
    readiness = asyncio.create_task(ready.wait())
    stop_waiter = asyncio.create_task(stopped_event.wait())
    try:
        done, _ = await asyncio.wait({task, readiness, stop_waiter}, timeout=max(0, setup_due - time.monotonic()),
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            stop()
            try:
                await settle(task, stop_due)
            finally:
                raise TimeoutError("Aggregate setup budget exhausted")
        if task not in done and stop_waiter not in done:
            done, _ = await asyncio.wait({task, stop_waiter}, timeout=max(0, hard - time.monotonic()),
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError("Native command hard deadline reached")
        await settle(task, stop_due if stopped else hard)
    except (TimeoutError, asyncio.CancelledError):
        stop()
        try:
            await settle(task, stop_due)
        except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- preserve original stop, never reset the hard deadline
            import logging

            logging.getLogger(__name__).debug("Bounded job close failed: %s", type(exc).__name__)
        raise
    finally:
        if previous_anchor is None:
            os.environ.pop(HOST_START_ENV, None)
        else:
            os.environ[HOST_START_ENV] = previous_anchor
        readiness.cancel()
        stop_waiter.cancel()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        if not task.done():
            stop()

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    if "--run-bounded-job" in __import__("sys").argv:
        runner = argparse.ArgumentParser(description="Run one explicitly bounded frozen Harbor job")
        runner.add_argument("--run-bounded-job", type=Path, required=True)
        args = runner.parse_args()
        import asyncio

        asyncio.run(run_bounded_job(args.run_bounded_job))
        return
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment", choices=["docker", "modal"], default="modal")
    parser.add_argument("--trials", type=int, default=DEFAULT_TRIALS, help="Trials per selected model")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--tool-profile", choices=["published"], default="published")
    parser.add_argument("--data-mode", choices=["fixture", "hybrid"], default="hybrid")
    parser.add_argument("--browser-runtime", choices=["subprocess"], default="subprocess")
    parser.add_argument("--model", action="append", help="Inspect model ID; repeat for a multi-model batch")
    parser.add_argument("--simulator-model", help=f"Counterparty simulator; default {DEFAULT_ROLE_MODEL}")
    parser.add_argument("--grader-model", help=f"Simulator's financial consistency checker; default {DEFAULT_GRADER_MODEL}")
    parser.add_argument("--max-turns", type=int, default=TrialSettings.model_fields["max_turns"].default)
    parser.add_argument("--history-tokens", type=int, default=TrialSettings.model_fields["history_tokens"].default,
                        help="Model-visible history in tokens, with a memory tool; 0 shows the full history")
    parser.add_argument("--sim-weeks", type=float, default=argparse.SUPPRESS)
    parser.add_argument("--token-budget", type=int, default=argparse.SUPPRESS,
                        help=f"Target tokens including cached input; late-refund defaults to {LATE_REFUND_TOKEN_BUDGET}")
    parser.add_argument("--budget-notice", choices=list(BUDGET_NOTICES),
                        default=TrialSettings.model_fields["budget_notice"].default,
                        help="With a token budget, show the runtime notice every step, daily, or when low")
    parser.add_argument("--cost-limit", default=argparse.SUPPRESS,
                        help='Per-trial USD cap for target and world model calls in-process, or JSON mapping of '
                             'target model IDs to these caps; required for late-refund')
    parser.add_argument("--trial-deadline-policy", choices=["bounded-lifecycle-v1"], default=None)
    parser.add_argument("--time-limit", type=int, default=DEFAULT_TIME_LIMIT)
    parser.add_argument("--reasoning", action="store_true", help="Return the target's reasoning (OpenRouter models)")
    parser.add_argument("--scenario", choices=scenario_names(),
                        default=TrialSettings.model_fields["scenario"].default)
    parser.add_argument("--seed", type=int,
                        help="Non-negative seed for simulated randomness; omit to draw one per trial")
    parser.add_argument("--identity-lock", help="Run identity lock manifest path or JSON object")
    parser.add_argument("--research-mode", choices=["auto", "live", "saved"], default=argparse.SUPPRESS,
                        help="Open-world research: committed dossiers when fresh, always live, or saved only")
    parser.add_argument("--secret", action="append", default=[], help="Modal secret name; repeatable")
    parser.add_argument("--inbox", type=Path, help="Gmail snapshot JSON from refund_eval.world.snapshot")
    args = parser.parse_args()
    if hasattr(args, "cost_limit"):
        try:
            args.cost_limit = json.loads(args.cost_limit)
        except ValueError:
            parser.error("--cost-limit must be a positive USD number or JSON model-to-cap mapping")
    if not args.model:
        parser.error("Provide --model for a paid trial")
    from refund_eval.world.snapshot_store import file_sha256

    # Freeze the resolved roles in the job so its record names what ran.
    args.simulator_model = args.simulator_model or DEFAULT_ROLE_MODEL
    args.grader_model = args.grader_model or DEFAULT_GRADER_MODEL
    settings = TrialSettings(**{name: getattr(args, name) for name in TrialSettings.model_fields
                                if hasattr(args, name)},
                             inbox_sha256=file_sha256(args.inbox) if args.inbox else None)
    if args.trial_deadline_policy is not None:
        from refund_eval.harbor.deadlines import trial_deadlines

        settings = settings.model_copy(update={"trial_deadlines": trial_deadlines(settings.time_limit, args.trial_deadline_policy)})
    config = prepare(
        args.output, settings=settings, models=args.model,
        environment=args.environment, trials=args.trials, concurrency=args.concurrency, secrets=args.secret,
        inbox=args.inbox,
    )
    print(json.dumps({"config": str(config), "trials_per_model": args.trials,
                      "total_trials": args.trials * len(args.model), "concurrency": args.concurrency}))
    if settings.trial_deadlines is None:
        print(f"Run with: scripts/harbor harbor run -c {config}")
    else:
        bounds = settings.trial_deadlines
        print("Run via the reviewed realism measure coordinator with "
              f"--trial-deadline-policy {bounds['version']}; "
              f"it supplies the native {bounds['command_soft_seconds']}s soft / "
              f"{bounds['command_hard_seconds']}s hard parent guard.")
        print(f"Internal coordinator argv: scripts/harbor python -m refund_eval.harbor --run-bounded-job {config}")
        print("The internal argv alone does not supply the coordinator's host hard-stop guard.")


if __name__ == "__main__":
    main()

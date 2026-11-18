"""Generate a native Harbor task for one scenario: a world sidecar, the Inspect agent in `main`, a separate verifier.

    scripts/harbor python -m refund_eval.harbor.adapter --scenario train_refund_late --out .context/harbor_tasks

Writes, and launches nothing:
- `OUT/<scenario>/`: task.toml (schema 1.4), instruction.md, environment/ (`main` Dockerfile,
  docker-compose.yaml, world/Dockerfile and separate agent and world build inputs) and
  tests/ (the verifier image and test.sh);
- `OUT/dataset.toml`: the tasks in OUT, each pinned by Harbor's content digest, and `OUT/metric.py`
  (`grading.harbor_metric`: per-key means that skip missing keys);
- `OUT/job.yaml`: a sample job for the installed agent (`refund_eval.harbor.installed_agent`).
  Running it is paid: it calls OpenRouter for the target and the world's simulator and grader.

World settings (scenario, simulated weeks, turn cap, data mode, research mode, simulator and
grader models) are fixed in the task; target budgets are agent options in the job. The task
directory holds the source and per-task tokens: keep it private (an ignored path).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import shutil
import subprocess
from pathlib import Path

import yaml

from refund_eval.config import (
    DEFAULT_GRADER_MODEL,
    DEFAULT_ROLE_MODEL,
    LATE_REFUND_SCENARIO,
    LATE_REFUND_TOKEN_BUDGET,
    RunConfig,
)
from refund_eval.environment_version import ENVIRONMENT_VERSION, environment_fingerprint
from refund_eval.output_paths import validate_private_output
from refund_eval.scenario.schema import (
    SCENARIO_DIR,
    Scenario,
    load_scenario,
    scenario_names,
)

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path(__file__).with_name("task_template")
METRIC = ROOT / "refund_eval" / "grading" / "harbor_metric.py"  # standard library only: the dataset's metric.py
TASK_VERSION = ENVIRONMENT_VERSION
ORG = "refund-eval"
DATASET_NAME = f"{ORG}/impossible-refund"
AGENT_IMPORT_PATH = "refund_eval.harbor.installed_agent:RefundInstalledAgent"
SOURCE_FILES = ["refund_eval", "pyproject.toml", "uv.lock", "README.md", "LICENSE"]
AGENT_MODULES = {
    "": "__init__ config killswitch model_pricing output_paths task",
    "agent": "__init__ adapters budget harness prompt schedule world_mode",
    "companies": "__init__ models",
    "harbor": "__init__ atif",
    "models": "__init__ client replay",
    "parity": "__init__ inspect_cli scripts",
    "published": "__init__ adapter mail mcp provider search",
    "realism": "__init__ notes track",
    "runtime": "__init__ checkpoint events records snapshots trial_artifacts turns",
    "scenario": "__init__ schema",
    "sim": "__init__ interface",
    "storage": "__init__ hook rows supabase",
    "tasks": "__init__ base",
    "tasks/household_bills": "__init__ facts",
    "tasks/train_refund": "__init__ facts",
    "world": "__init__ clock event_log guards ids interception mail mailquery orgs snapshot_store state store timeutil trial_snapshot",
}
HOST_SEED = "${WORLD_SEED:-}"
DERIVED_SEED = "per trial: first 4 bytes (big-endian) of sha256('<task_version>\\n<trial name>')"


def rules_sha256(scenario_id: str) -> str:
    """Digest of the scenario's committed company rules (names and bytes), or "none"."""
    directory = SCENARIO_DIR / "rules" / scenario_id
    files = sorted(p for p in directory.rglob("*") if p.is_file()) if directory.is_dir() else []
    if not files:
        return "none"
    digest = hashlib.sha256()
    for path in files:
        for value in (path.relative_to(directory).as_posix().encode(), path.read_bytes()):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def copy_source(destination: Path) -> str:
    """Version-controlled and new non-ignored source (never .env, .secrets, logs); returns its sha256."""
    from refund_eval.realism.track import SOURCE_MANIFEST, freeze_source_manifest

    files = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *SOURCE_FILES], cwd=ROOT,
    ).decode().split("\0")
    for filename in filter(None, files):
        original = ROOT / filename
        if original.is_symlink():
            raise ValueError(f"Source snapshots do not follow symlinks: {filename}")
        if original.is_file():
            target = destination / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
    freeze_source_manifest(destination)
    return json.loads((destination / SOURCE_MANIFEST).read_text())["source_sha256"]


def _fill(text: str, values: dict[str, object]) -> str:
    for key, value in values.items():
        text = text.replace(f"__{key}__", str(value))
    if left := re.findall(r"__[A-Z0-9_]+__", text):
        raise ValueError(f"Unfilled template placeholders: {left}")
    return text


def stage_agent(source: Path, destination: Path, scenario: Scenario, *, scenario_name: str) -> None:
    from refund_eval.realism.track import SOURCE_MANIFEST

    files = [Path(name) for name in SOURCE_FILES if name != "refund_eval"]
    files.append(Path("refund_eval/agent/system_prompt.md"))
    files.extend(Path("refund_eval", package, f"{module}.py")
                 for package, modules in AGENT_MODULES.items() for module in modules.split())
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)

    view = scenario.model_dump(mode="json", include={
        "id": True, "task_kind": True, "task_text": True, "start_time": True, "sim_weeks": True,
        "user": {"name", "email_addresses", "phone"}, "facts": True,
        "orgs": {"__all__": {"key", "name", "kind", "email_domains", "contact_url"}},
    }, exclude={"facts": {"true_circumstances"}})
    (destination / "refund_eval" / "scenario" / f"{scenario_name}.yaml").write_text(
        yaml.safe_dump(view), encoding="utf-8",
    )
    manifest = json.loads((source / SOURCE_MANIFEST).read_text(encoding="utf-8"))
    memory = source / "refund_eval" / "scenario" / "realism" / f"{scenario.id}.json"
    if memory.is_file():
        manifest["realism_memory"] = {scenario.id: hashlib.sha256(memory.read_bytes()).hexdigest()[:8]}
    (destination / SOURCE_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def write_task(out: Path, *, scenario: str, data_mode: str, sim_weeks: float | None = None, max_turns: int, research_mode: str,
               simulator_model: str, grader_model: str, agent_timeout: float, verifier_timeout: float,
               cpus: int, memory_mb: int, task_version: str = TASK_VERSION, world_seed: str = HOST_SEED,
               world_cost_limit: float | None = None) -> Path:
    """Write OUT/<scenario>/ and return it."""
    from refund_eval.agent.prompt import task_message

    sc = load_scenario(scenario)
    if sim_weeks is None:
        sim_weeks = sc.sim_weeks
    task_dir = out / scenario
    if task_dir.exists():
        raise FileExistsError(f"Refusing to overwrite a generated task: {task_dir}")
    shutil.copytree(TEMPLATE, task_dir)
    source = task_dir / "environment" / "source"
    source_sha256 = copy_source(source)
    stage_agent(source, task_dir / "environment" / "agent", sc, scenario_name=scenario)
    shutil.copytree(source, task_dir / "tests" / "source")  # the verifier image builds from tests/ alone
    seed_policy = DERIVED_SEED
    if world_seed == HOST_SEED:
        seed_policy += "; WORLD_SEED overrides"
    elif world_seed:
        seed_policy = f"fixed for all trials: {world_seed}"
    values = {
        "TASK_NAME": f"{ORG}/{scenario}", "SCENARIO": scenario, "TASK_VERSION": task_version,
        "ENVIRONMENT_VERSION": ENVIRONMENT_VERSION, "ENVIRONMENT_FINGERPRINT": environment_fingerprint(),
        "DESCRIPTION": f"Impossible-refund benchmark, scenario {sc.id}: a long-horizon customer-service task "
                       "against simulated organisations, graded from the world's event log.",
        "DATA_MODE": data_mode, "SIM_WEEKS": sim_weeks, "MAX_TURNS": max_turns, "RESEARCH_MODE": research_mode,
        "SIMULATOR_MODEL": simulator_model, "GRADER_MODEL": grader_model, "RULES_SHA256": rules_sha256(sc.id),
        "SOURCE_SHA256": source_sha256, "AGENT_TIMEOUT": float(agent_timeout),
        "VERIFIER_TIMEOUT": float(verifier_timeout), "CPUS": cpus, "MEMORY_MB": memory_mb,
        "WORLD_SEED": world_seed, "SEED_POLICY": json.dumps(seed_policy + "; recorded in /var/log/world/seed.json and state.json"),
        "WORLD_COST_LIMIT": world_cost_limit if world_cost_limit is not None else "",
        "TURN_TOKEN": secrets.token_urlsafe(32), "ADMIN_TOKEN": secrets.token_urlsafe(32),
    }
    for name in ("task.toml", "environment/docker-compose.yaml"):
        path = task_dir / name
        path.write_text(_fill(path.read_text(), values), encoding="utf-8")
    (task_dir / "tests" / "test.sh").chmod(0o755)
    (task_dir / "instruction.md").write_text(task_message(sc, sim_weeks) + "\n", encoding="utf-8")
    return task_dir


def sample_job(out: Path, task_dir: Path, *, scenario: str) -> dict:
    late = load_scenario(scenario).id == LATE_REFUND_SCENARIO
    return {
        "job_name": f"refund-native-{scenario}",
        "jobs_dir": str(out / "jobs"),
        "n_attempts": 2,
        "n_concurrent_trials": 2,
        "retry": {"max_retries": 0},
        "environment": {"type": "docker", "delete": True},
        "agents": [{
            "import_path": AGENT_IMPORT_PATH,
            "model_name": "openrouter/anthropic/claude-opus-5.5",
            "kwargs": {"cost_limit": 150.0 if late else 1.0, "history_tokens": 60000, "time_limit": 36000,
                       **({"token_budget": LATE_REFUND_TOKEN_BUDGET} if late else {})},
            "env": {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"},
        }],
        "tasks": [{"path": str(task_dir)}],
    }


def validate_and_index(out: Path, task_dir: Path, job: dict) -> str:
    """Load the task and job with Harbor's own models; pin every task in OUT in dataset.toml."""
    try:
        from harbor.models.dataset.manifest import (
            DatasetFileRef,
            DatasetInfo,
            DatasetManifest,
            DatasetTaskRef,
        )
        from harbor.models.job.config import JobConfig
        from harbor.models.task.task import Task
        from harbor.publisher.packager import Packager
    except ImportError as error:
        raise SystemExit("Harbor is not installed here; run with scripts/harbor python -m refund_eval.harbor.adapter") \
            from error

    selected = Task(task_dir).config.task.name
    JobConfig.model_validate(job)
    refs = []
    for directory in sorted(p for p in out.iterdir() if (p / "task.toml").is_file()):
        task = Task(directory)
        digest, _ = Packager.compute_content_hash(directory)
        refs.append(DatasetTaskRef(name=task.config.task.name, digest=f"sha256:{digest}"))
    # Harbor's default mean counts a missing reward key as 0; this one skips it (see harbor_metric).
    shutil.copyfile(METRIC, out / "metric.py")
    metric = DatasetFileRef(path="metric.py", digest=f"sha256:{Packager.compute_file_hash(out / 'metric.py')}")
    manifest = DatasetManifest(dataset=DatasetInfo(name=DATASET_NAME, version=TASK_VERSION,
                                                   description="Impossible-refund benchmark (native Harbor tasks).",
                                                   keywords=["refund-eval", "long-horizon"]),
                               tasks=refs, files=[metric])
    (out / "dataset.toml").write_text(manifest.to_toml(), encoding="utf-8")
    return next(ref.digest for ref in refs if ref.name == selected)


def main(argv: list[str] | None = None) -> int:
    from pydantic import TypeAdapter

    from refund_eval.config import CostCap

    defaults = RunConfig.model_fields
    p = argparse.ArgumentParser(prog="python -m refund_eval.harbor.adapter", description=__doc__.split("\n")[0])
    p.add_argument("--scenario", required=True, choices=scenario_names())
    p.add_argument("--out", type=Path, required=True, help="Output directory (ignored or outside Git)")
    p.add_argument("--data-mode", choices=["fixture", "hybrid"], default=defaults["data_mode"].default)
    p.add_argument("--sim-weeks", type=float, default=None)
    p.add_argument("--max-turns", type=int, default=defaults["max_turns"].default)
    p.add_argument("--research-mode", choices=["auto", "live", "saved"], default=defaults["research_mode"].default,
                   help="Organisation research mode (default: %(default)s)")
    p.add_argument("--simulator-model", default=DEFAULT_ROLE_MODEL)
    p.add_argument("--grader-model", default=DEFAULT_GRADER_MODEL)
    p.add_argument("--world-cost-limit", type=float, help="Per-trial world model USD allowance; required for paid runs")
    p.add_argument("--agent-timeout", type=float, default=39_600, help="Harbor agent timeout in seconds")
    p.add_argument("--verifier-timeout", type=float, default=600)
    p.add_argument("--cpus", type=int, default=2)
    p.add_argument("--memory-mb", type=int, default=4096)
    args = p.parse_args(argv)
    if args.world_cost_limit is not None:
        try:
            TypeAdapter(CostCap).validate_python(args.world_cost_limit)
        except ValueError:
            p.error("--world-cost-limit must be finite and in (0, 1000]")
    out = validate_private_output(args.out)
    out.mkdir(parents=True, exist_ok=True)
    task_dir = write_task(out, scenario=args.scenario, data_mode=args.data_mode, sim_weeks=args.sim_weeks,
                          max_turns=args.max_turns, research_mode=args.research_mode,
                          simulator_model=args.simulator_model, grader_model=args.grader_model,
                          agent_timeout=args.agent_timeout, verifier_timeout=args.verifier_timeout,
                          cpus=args.cpus, memory_mb=args.memory_mb, world_cost_limit=args.world_cost_limit)
    job = sample_job(out, task_dir, scenario=args.scenario)
    validate_and_index(out, task_dir, job)
    header = ("# Sample Harbor job for the native refund task; the adapter does not launch it.\n"
              "# Paid: it calls OpenRouter for the target, simulator and grader. Run with an explicit budget:\n"
              f"#   scripts/harbor harbor run -c {out / 'job.yaml'}\n")
    (out / "job.yaml").write_text(header + yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    print(json.dumps({"task": str(task_dir), "dataset": str(out / "dataset.toml"), "job": str(out / "job.yaml")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

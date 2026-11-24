"""Generate a native Harbor task for one scenario: a world sidecar, the Inspect agent in `main`, a separate verifier.

    scripts/harbor python -m refund_eval.harbor.adapter --scenario train_refund_late --out .context/harbor_tasks \
        --agent-wheel /path/to/approved-agent.whl --agent-wheel-sha256 SHA256_HEX

The approved wheel path and its SHA256 are required; the adapter hashes the bytes before output creation
and stages those exact bytes. Writes, and launches nothing:
- `OUT/<task_id>/`: task.toml (schema 1.4), instruction.md, environment/ (`main` Dockerfile,
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
import ast
import hashlib
import io
import json
import re
import secrets
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import date
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
from refund_eval.models.shared_role_spend_ledger import SHARED_CAPS_HEADROOM_BOUND_ENV
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
    "": "__init__ config environment_version killswitch model_pricing output_paths task",
    "agent": "__init__ adapters budget dollar_budget harness prompt schedule world_mode",
    "companies": "__init__ models",
    "harbor": "__init__ atif",
    "models": "__init__ client evidence replay shared_role_spend_ledger world_spend_ledger",
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
SPLIT_BUDGET_WHEEL_SOURCES = (
    "refund_eval/agent/dollar_budget.py",
    "refund_eval/agent/harness.py",
    "refund_eval/agent/prompt.py",
    "refund_eval/agent/world_mode.py",
    "refund_eval/model_pricing.py",
    "refund_eval/models/world_spend_ledger.py",
    "refund_eval/models/shared_role_spend_ledger.py",
    "refund_eval/published/provider.py",
    "refund_eval/task.py",
)
SPLIT_BUDGET_TASK_SOURCES = (
    "refund_eval/model_pricing.py",
    "refund_eval/models/budget.py",
    "refund_eval/models/client.py",
    "refund_eval/models/openrouter.py",
    "refund_eval/models/world_spend_ledger.py",
    "refund_eval/models/shared_role_spend_ledger.py",
    "refund_eval/world_service/settings.py",
    "refund_eval/world_service/__main__.py",
    "refund_eval/world_service/trial.py",
    "refund_eval/harbor/world_entrypoint.py",
)
HOST_SEED = "${WORLD_SEED:-}"
DERIVED_SEED = "per trial: first 4 bytes (big-endian) of sha256('<task_version>\\n<trial name>')"
WHEEL_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]*\.whl\Z")
SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}\Z")


@dataclass(frozen=True)
class VerifiedAgentWheel:
    path: Path
    filename: str
    sha256: str
    content: bytes


def read_verified_agent_wheel(path: Path, expected_sha256: str) -> VerifiedAgentWheel:
    filename = path.name
    if not WHEEL_BASENAME.fullmatch(filename):
        raise ValueError("Agent wheel path must have a safe .whl basename")
    if not SHA256_HEX.fullmatch(expected_sha256):
        raise ValueError("Agent wheel SHA256 must contain 64 hexadecimal characters")
    content = path.read_bytes()
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != expected_sha256.lower():
        raise ValueError("Agent wheel SHA256 does not match the supplied digest")
    return VerifiedAgentWheel(path, filename, actual_sha256, content)


def verify_suite_task_wheel(wheel: VerifiedAgentWheel, scenarios: set[str]) -> None:
    error = (
        "Native task suites require a verified wheel whose "
        "refund_eval.task.impossible_refund has optional task_text and task_id parameters "
        "and embedded agent scenario descriptors"
    )
    try:
        with zipfile.ZipFile(io.BytesIO(wheel.content)) as archive:
            source = archive.read("refund_eval/task.py")
            members = set(archive.namelist())
        module = ast.parse(source)
    except (OSError, KeyError, zipfile.BadZipFile, SyntaxError, UnicodeDecodeError):
        raise ValueError(error) from None
    task = next((node for node in module.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "impossible_refund"),
                None)
    if task is None:
        raise ValueError(error)
    positional = task.args.posonlyargs + task.args.args
    optional = {argument.arg for argument in positional[len(positional) - len(task.args.defaults):]}
    optional.update(argument.arg for argument, default in zip(task.args.kwonlyargs, task.args.kw_defaults)
                    if default is not None)
    if not {"task_text", "task_id"} <= optional:
        raise ValueError(error)
    missing = sorted(
        scenario for scenario in scenarios
        if f"refund_eval/scenario/{scenario}.yaml" not in members
    )
    if missing:
        raise ValueError(
            "Native task suite wheel is missing agent scenario descriptors: " + ", ".join(missing)
        )


def verify_split_budget_wheel(path: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            for relative in SPLIT_BUDGET_WHEEL_SOURCES:
                actual = archive.read(relative)
                if actual != (ROOT / relative).read_bytes():
                    raise ValueError("split budget source does not match the current approved wheel source")
                hashes[relative] = hashlib.sha256(actual).hexdigest()
    except (OSError, KeyError, zipfile.BadZipFile):
        raise ValueError("split mode requires an approved wheel with current budget sources") from None
    return hashes


def verify_split_budget_task_sources(task_path: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    source = task_path / "environment" / "source"
    try:
        for relative in SPLIT_BUDGET_TASK_SOURCES:
            actual = (source / relative).read_bytes()
            if actual != (ROOT / relative).read_bytes():
                raise ValueError("split world source does not match current budget enforcement")
            hashes[relative] = hashlib.sha256(actual).hexdigest()
    except OSError:
        raise ValueError("split task is missing current world budget sources") from None
    return hashes


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


def stage_agent(source: Path, destination: Path, scenario: Scenario, *, scenario_name: str,
                verified_wheel: VerifiedAgentWheel | None = None) -> None:
    from refund_eval.realism.track import SOURCE_MANIFEST

    if verified_wheel is not None:
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("pyproject.toml", "uv.lock"):
            shutil.copyfile(source / name, destination / name)
        wheel_dir = destination / "wheels"
        wheel_dir.mkdir()
        (wheel_dir / verified_wheel.filename).write_bytes(verified_wheel.content)
        (destination / "agent-wheel.json").write_text(json.dumps({
            "filename": verified_wheel.filename,
            "sha256": verified_wheel.sha256,
        }, indent=2) + "\n", encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("install_agent.py"), destination / "install-agent.py")
        return

    files = [Path(name) for name in SOURCE_FILES if name != "refund_eval"]
    files.append(Path("refund_eval/agent/system_prompt.md"))
    files.extend(Path("refund_eval", package, f"{module}.py")
                 for package, modules in AGENT_MODULES.items() for module in modules.split())
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)

    view = scenario.model_dump(mode="json", include={
        "id": True, "task_text": True, "start_time": True, "requested_deadline": True, "sim_weeks": True,
        "user": {"name", "email_addresses", "phone"},
    })
    (destination / "refund_eval" / "scenario" / f"{scenario_name}.yaml").write_text(
        yaml.safe_dump(view), encoding="utf-8",
    )
    manifest = json.loads((source / SOURCE_MANIFEST).read_text(encoding="utf-8"))
    manifest.pop("files", None)
    memory = source / "refund_eval" / "scenario" / "realism" / f"{scenario.id}.json"
    if memory.is_file():
        manifest["realism_memory"] = {scenario.id: hashlib.sha256(memory.read_bytes()).hexdigest()[:8]}
    (destination / SOURCE_MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def write_task(out: Path, *, scenario: str, data_mode: str, sim_weeks: float | None = None, max_turns: int, research_mode: str,
               simulator_model: str, grader_model: str, agent_timeout: float, verifier_timeout: float,
               cpus: int, memory_mb: int, task_version: str = TASK_VERSION, world_seed: str = HOST_SEED,
               world_cost_limit: float | None = None, agent_wheel: Path, agent_wheel_sha256: str,
               verified_wheel: VerifiedAgentWheel | None = None, openrouter_key_mode: str = "shared",
               shared_allowance_usd: float | None = None, shared_role_budget_config: str | None = None,
               task_id: str | None = None, task_text: str | None = None, suite_task: bool = False,
               requested_deadline: date | None = None) -> Path:
    """Write OUT/<task_id>/ and return it."""
    from refund_eval.agent.prompt import task_message, with_requested_deadline

    if verified_wheel is None:
        verified_wheel = read_verified_agent_wheel(agent_wheel, agent_wheel_sha256)
    elif (verified_wheel.path != agent_wheel or verified_wheel.sha256 != agent_wheel_sha256.lower()):
        raise ValueError("Verified agent wheel does not match the supplied wheel and SHA256")

    sc = load_scenario(scenario)
    agent_scenario = with_requested_deadline(sc, requested_deadline)
    if sim_weeks is None:
        sim_weeks = sc.sim_weeks
    task_id = scenario if task_id is None else task_id
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", task_id):
        raise ValueError("task_id must start with a lowercase letter and contain only lowercase letters, digits, '_' or '-'")
    if task_text is not None and not task_text.strip():
        raise ValueError("task_text must not be empty")
    task_dir = out / task_id
    if task_dir.exists():
        raise FileExistsError(f"Refusing to overwrite a generated task: {task_dir}")
    shutil.copytree(TEMPLATE, task_dir)
    source = task_dir / "environment" / "source"
    source_sha256 = copy_source(source)
    stage_agent(source, task_dir / "environment" / "agent", agent_scenario, scenario_name=scenario,
                verified_wheel=verified_wheel)
    shutil.copytree(source, task_dir / "tests" / "source")  # the verifier image builds from tests/ alone
    from refund_eval.sim import company_store

    store_root = company_store.store_root()
    seed_store = task_dir / "environment" / "company-store"
    seed_store.mkdir()
    if store_root is not None:
        company_store.harvest_registered_jobs(sc, destination_root=store_root)
        company_store.sync_scenario_entries(sc, source_root=store_root, destination_root=seed_store)
    seed_policy = DERIVED_SEED
    if world_seed == HOST_SEED:
        seed_policy += "; WORLD_SEED overrides"
    elif world_seed:
        seed_policy = f"fixed for all trials: {world_seed}"
    values = {
        "TASK_NAME": f"{ORG}/{task_id}", "TASK_ID": task_id, "SCENARIO": scenario, "TASK_VERSION": task_version,
        "ENVIRONMENT_VERSION": ENVIRONMENT_VERSION, "ENVIRONMENT_FINGERPRINT": environment_fingerprint(),
        "DESCRIPTION": f"Long-horizon customer-service task {task_id} using scenario {sc.id}, "
                       "against simulated organisations, graded from the world's event log.",
        "DATA_MODE": data_mode, "SIM_WEEKS": sim_weeks, "MAX_TURNS": max_turns, "RESEARCH_MODE": research_mode,
        "SIMULATOR_MODEL": simulator_model, "GRADER_MODEL": grader_model, "RULES_SHA256": rules_sha256(sc.id),
        "SOURCE_SHA256": source_sha256, "AGENT_TIMEOUT": float(agent_timeout),
        "AGENT_WHEEL_FILENAME": verified_wheel.filename, "AGENT_WHEEL_SHA256": verified_wheel.sha256,
        "VERIFIER_TIMEOUT": float(verifier_timeout), "CPUS": cpus, "MEMORY_MB": memory_mb,
        "WORLD_SEED": world_seed, "SEED_POLICY": json.dumps(seed_policy + "; recorded in /var/log/world/seed.json and state.json"),
        "COMPANY_STORE_ROOT": "/var/log/world/company-store" if store_root is not None else "off",
        "WORLD_COST_LIMIT": world_cost_limit if world_cost_limit is not None else "",
        "WORLD_STRICT_BUDGET": "true" if openrouter_key_mode in {"split", "shared_roles", "shared_caps"} else "false",
        "OPENROUTER_WORLD_KEY": "${OPENROUTER_WORLD_API_KEY}" if openrouter_key_mode == "split" else "${OPENROUTER_API_KEY:-}",
        "OPENROUTER_KEY_MODE": openrouter_key_mode,
        "SHARED_ALLOWANCE_USD": "" if shared_allowance_usd is None else str(shared_allowance_usd),
        "REQUESTED_DEADLINE": "" if requested_deadline is None else requested_deadline.isoformat(),
        "SHARED_ROLE_MODE": openrouter_key_mode if openrouter_key_mode in {"shared_roles", "shared_caps"} else "disabled",
        "SHARED_ROLE_BUDGET_CONFIG_TOML": json.dumps(shared_role_budget_config or ""),
        "SHARED_ROLE_BUDGET_CONFIG_YAML": yaml.safe_dump(shared_role_budget_config or "", default_style='"').strip(),
        "SUITE_TASK_ID": task_id if suite_task else "",
        "TURN_TOKEN": secrets.token_urlsafe(32), "ADMIN_TOKEN": secrets.token_urlsafe(32),
    }
    for name in ("task.toml", "environment/docker-compose.yaml"):
        path = task_dir / name
        rendered = _fill(path.read_text(), values)
        if name == "environment/docker-compose.yaml":
            if openrouter_key_mode in {"shared_roles", "shared_caps"}:
                rendered = rendered.replace(
                    "    # ROLE_BUDGET_MAIN_VOLUME\n",
                    "    volumes:\n      - role-budget:/var/lib/refund-eval/role-budget\n",
                ).replace(
                    "      # ROLE_BUDGET_WORLD_VOLUME\n",
                    "      - role-budget:/var/lib/refund-eval/role-budget\n"
                    "      - role-budget:/var/log/world/role-budget\n",
                ).replace("  # ROLE_BUDGET_VOLUME\n", "  role-budget:\n")
            if openrouter_key_mode == "shared_caps":
                marker_anchor = '      REFUND_EVAL_SHARED_ROLE_MODE: "shared_caps"\n'
                if rendered.count(marker_anchor) != 1:
                    raise ValueError("shared_caps Compose mode marker is missing")
                rendered = rendered.replace(
                    marker_anchor,
                    marker_anchor + f'      {SHARED_CAPS_HEADROOM_BOUND_ENV}: "false"\n',
                )
            else:
                rendered = rendered.replace("    # ROLE_BUDGET_MAIN_VOLUME\n", "")
                rendered = rendered.replace("      # ROLE_BUDGET_WORLD_VOLUME\n", "")
                rendered = rendered.replace("  # ROLE_BUDGET_VOLUME\n", "")
        path.write_text(rendered, encoding="utf-8")
    (task_dir / "tests" / "test.sh").chmod(0o755)
    instruction = task_text if task_text is not None else task_message(agent_scenario, sim_weeks)
    (task_dir / "instruction.md").write_text(
        instruction if instruction.endswith("\n") else instruction + "\n", encoding="utf-8",
    )
    if store_root is not None:
        company_store.register_native_jobs_root(out / "jobs", root=store_root)
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
    p.add_argument("--agent-wheel", type=Path, required=True, help="Approved agent wheel to install")
    p.add_argument("--agent-wheel-sha256", required=True, help="Expected SHA256 of the approved agent wheel")
    p.add_argument("--agent-timeout", type=float, default=39_600, help="Harbor agent timeout in seconds")
    p.add_argument("--verifier-timeout", type=float, default=600)
    p.add_argument("--cpus", type=int, default=2)
    p.add_argument("--memory-mb", type=int, default=4096)
    args = p.parse_args(argv)
    verified_wheel = read_verified_agent_wheel(args.agent_wheel, args.agent_wheel_sha256)
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
                          cpus=args.cpus, memory_mb=args.memory_mb, world_cost_limit=args.world_cost_limit,
                          agent_wheel=args.agent_wheel, agent_wheel_sha256=args.agent_wheel_sha256,
                          verified_wheel=verified_wheel)
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

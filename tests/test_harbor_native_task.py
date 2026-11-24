"""The native Harbor task: adapter output loaded with Harbor's own models, the installed agent against a
fake environment, and converter output against Harbor's ATIF model. No sandboxes or inference.

Run with scripts/harbor python -m pytest tests/test_harbor_native_task.py.
"""

import asyncio
import base64
import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import tomllib
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

pytestmark = pytest.mark.harbor

pytest.importorskip("harbor.models.job.config")

from harbor.models.agent.context import AgentContext
from harbor.models.dataset.manifest import DatasetManifest
from harbor.models.job.config import JobConfig
from harbor.models.task.config import MCPServerConfig
from harbor.models.task.task import Task
from harbor.models.trajectories import Trajectory
from harbor.publisher.packager import Packager
from harbor.utils.trajectory_validator import TrajectoryValidator

from refund_eval.agent.prompt import (
    render_system_prompt,
    task_message,
)
from refund_eval.agent.schedule import format_long, sim_deadline
from refund_eval.harbor import adapter, atif, batch, world_entrypoint
from refund_eval.harbor.installed_agent import RefundAgentOptions, RefundInstalledAgent
from refund_eval.scenario import schema
from refund_eval.scenario.schema import (
    AgentScenario,
    Org,
    load_agent_scenario,
    load_scenario,
)
from refund_eval.sim import company_store
from scripts.laura_trial import main as laura_trial_main


def _write_inert_wheel(directory):
    filename = "synthetic_agent-0.0.0-py3-none-any.whl"
    dist_info = "synthetic_agent-0.0.0.dist-info"
    files = {
        "synthetic_agent/__init__.py": b'VALUE = "synthetic"\n',
        f"{dist_info}/METADATA": b"Metadata-Version: 2.1\nName: synthetic-agent\nVersion: 0.0.0\n\n",
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\nGenerator: offline-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n\n",
    }
    record_name = f"{dist_info}/RECORD"
    rows = []
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        rows.append((name, f"sha256={digest}", str(len(content))))
    rows.append((record_name, "", ""))
    record = io.StringIO(newline="")
    csv.writer(record, lineterminator="\n").writerows(rows)
    files[record_name] = record.getvalue().encode()
    wheel = directory / filename
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


def _write_split_budget_wheel(directory, scenarios=("train_refund_late",)):
    source = directory / "source"
    public_source = directory / "public-source"
    adapter.copy_source(source)
    for scenario in scenarios:
        adapter.stage_agent(
            source,
            public_source,
            load_scenario(scenario),
            scenario_name=scenario,
        )
    build_dir = directory / "built-wheel"
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(build_dir)],
        cwd=public_source,
        check=True,
        capture_output=True,
        text=True,
    )
    [wheel] = build_dir.glob("*.whl")
    return wheel, hashlib.sha256(wheel.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def approved_split_budget_wheel(tmp_path_factory):
    return _write_split_budget_wheel(tmp_path_factory.mktemp("public-split-wheel"))


@pytest.fixture(scope="module")
def approved_suite_wheel(tmp_path_factory):
    return _write_split_budget_wheel(
        tmp_path_factory.mktemp("public-suite-wheel"), scenarios=("train_refund_late", "bills_price_rise")
    )


@pytest.fixture(scope="module")
def approved_wheel(tmp_path_factory):
    return _write_inert_wheel(tmp_path_factory.mktemp("synthetic_agent_wheel"))


def _wheel_config(approved_wheel):
    path, sha256 = approved_wheel
    return {"agent_wheel": path, "agent_wheel_sha256": sha256}


def _wheel_cli_args(approved_wheel):
    path, sha256 = approved_wheel
    return ["--agent-wheel", str(path), "--agent-wheel-sha256", sha256]


def _native_suite_config(suite, wheel):
    return batch.NativeBatchConfig(
        scenario="train_refund_late", execution="docker", data_mode="fixture", research_mode="saved",
        cost_limit=1, world_cost_limit=1, sim_weeks=12, trials=1, concurrency=2,
        suite=suite, **_wheel_config(wheel),
    )


@pytest.fixture(scope="module")
def generated(tmp_path_factory, approved_wheel):
    out = tmp_path_factory.mktemp("harbor_tasks")
    assert adapter.main(["--scenario", "train_refund_late", "--out", str(out), "--sim-weeks", "12",
                         *_wheel_cli_args(approved_wheel)]) == 0
    return out


def _native_store_entry(scenario, org, *, model="fixture/simulator", summary="Public company information"):
    created = datetime.now(UTC).replace(microsecond=0).isoformat()
    url = "https://native-store.example.test/contact"
    text = "The company publishes its contact details."
    page = {"url": url, "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "retrieved_at": created}
    page_refs = [{key: page[key] for key in ("url", "sha256", "retrieved_at")}]
    pages_digest = hashlib.sha256(json.dumps(
        [{"url": url, "sha256": page["sha256"]}], sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    provenance = {
        "identity": company_store.org_identity(org), "scenario_id": scenario.id,
        "intent_sha256": hashlib.sha256((scenario.intent or "").encode()).hexdigest(),
        "subject_sha256": hashlib.sha256((scenario.subject or "").encode()).hexdigest(),
        "terms_sha256": hashlib.sha256((org.terms or "").encode()).hexdigest(),
        "model": model, "temperature": 0, "rules_prompt_version": "1", "research_prompt_version": "2",
        "exemplar_prompt_version": "2", "checker_version": "1", "pages": page_refs,
        "pages_sha256": pages_digest,
    }
    inputs = {
        "identity": provenance["identity"], "scenario": scenario.id,
        "intent_sha256": provenance["intent_sha256"], "subject_sha256": provenance["subject_sha256"],
        "terms_sha256": provenance["terms_sha256"], "model": model, "rules_prompt_version": "1",
        "research_prompt_version": "2", "exemplar_prompt_version": "2", "checker_version": "1",
    }
    fingerprint = hashlib.sha256(json.dumps(
        inputs, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()[:16]
    return {
        "schema": 1, "fingerprint": fingerprint, "identity": provenance["identity"], "scenario": scenario.id,
        "created_at": created, "provenance": provenance, "org": org.model_dump(mode="json"),
        "research": {
            "org": org.key, "name": org.name, "mode": "live", "retrieved_at": created,
            "dossier": {"summary": summary, "facts": [], "persona": {"voice": "Professional."}},
            "pages": [page],
        },
        "rules": {"rules": {"terms": "The published company terms.", "grounds": []},
                  "model": model, "generated_at": created},
        "temperament": "Professional.", "examples": [],
    }


def test_native_company_store_roundtrip_is_world_only_and_immutable(tmp_path, monkeypatch, capsys, approved_wheel):
    scenario = load_scenario("train_refund_late")
    org = Org(key="native_dynamic", name="Native Example", kind="dynamic_advice",
              email_domains=["native-store.example.test"], web_domains=["native-store.example.test"],
              terms="The published company terms.")
    host_store = tmp_path / "host-store"
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", str(host_store))
    first = _native_store_entry(scenario, org)
    company_store.save_entry(scenario, org, first, root=host_store)
    original = company_store._entry_path(
        scenario.id, company_store.org_identity(org), first["fingerprint"], host_store).read_bytes()

    prepared = tmp_path / "prepared"
    assert adapter.main(["--scenario", scenario.id, "--out", str(prepared), *_wheel_cli_args(approved_wheel)]) == 0
    capsys.readouterr()
    task_dir = prepared / scenario.id
    environment = task_dir / "environment"
    seed_root = environment / "company-store"
    assert len(list(seed_root.rglob("*.json"))) == 1
    assert not list((environment / "agent").rglob("company-store"))
    assert not list((task_dir / "tests").rglob("company-store"))
    assert not (environment / "source" / "company-store").exists()
    world_dockerfile = (environment / "world" / "Dockerfile").read_text()
    assert "COPY company-store/ /opt/company-store-seed/" in world_dockerfile
    assert "!company-store/**" in (environment / "world" / "Dockerfile.dockerignore").read_text()

    jobs_dir = tmp_path / "jobs"
    artifact_store = jobs_dir / "trial-1" / "artifacts" / "world" / "company-store"
    monkeypatch.setattr(world_entrypoint, "COMPANY_STORE_SEED_ROOT", seed_root)
    assert world_entrypoint.initialize_company_store({
        "REFUND_EVAL_COMPANY_STORE": str(artifact_store), "WORLD_SCENARIO": scenario.id,
    }) == 1
    assert company_store._entry_path(
        scenario.id, company_store.org_identity(org), first["fingerprint"], artifact_store).is_file()

    second = _native_store_entry(scenario, org, model="fixture/updated-simulator", summary="New public information")
    company_store.save_entry(scenario, org, second, root=artifact_store)
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", str(host_store))
    assert company_store.main(["harvest", str(jobs_dir)]) == 0
    assert capsys.readouterr().out == "Harvested 1 company-store entries.\n"
    assert company_store._entry_path(
        scenario.id, company_store.org_identity(org), first["fingerprint"], host_store).read_bytes() == original
    assert company_store._entry_path(
        scenario.id, company_store.org_identity(org), second["fingerprint"], host_store).is_file()

    next_prepared = tmp_path / "next-prepared"
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", str(host_store))
    assert adapter.main(["--scenario", scenario.id, "--out", str(next_prepared),
                         *_wheel_cli_args(approved_wheel)]) == 0
    assert len(list((next_prepared / scenario.id / "environment" / "company-store").rglob("*.json"))) == 2


def test_native_prepare_harvests_registered_jobs_before_seeding(tmp_path, monkeypatch, capsys, approved_wheel):
    scenario = load_scenario("train_refund_late")
    org = Org(key="automatic_dynamic", name="Automatic Example", kind="dynamic_advice",
              email_domains=["automatic-store.example.test"], web_domains=["automatic-store.example.test"],
              terms="The published company terms.")
    host_store = tmp_path / "host-store"
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", str(host_store))

    first = tmp_path / "prepared-one"
    assert adapter.main(["--scenario", scenario.id, "--out", str(first), *_wheel_cli_args(approved_wheel)]) == 0
    capsys.readouterr()
    first_task = first / scenario.id
    first_seed = first_task / "environment" / "company-store"
    assert not list(first_seed.rglob("*.json"))

    jobs_root = first / "jobs"
    artifact_store = jobs_root / "job" / "trial" / "artifacts" / "world" / "company-store"
    entry = _native_store_entry(scenario, org)
    company_store.save_entry(scenario, org, entry, root=artifact_store)

    unregistered_artifact = (
        tmp_path / "unregistered-jobs" / "job" / "trial" / "artifacts" / "world" / "company-store"
    )
    unregistered_org = Org(
        key="unregistered_dynamic", name="Unregistered Example", kind="dynamic_advice",
        email_domains=["unregistered-store.example.test"], web_domains=["unregistered-store.example.test"],
        terms="The published company terms.",
    )
    unregistered_entry = _native_store_entry(scenario, unregistered_org)
    company_store.save_entry(scenario, unregistered_org, unregistered_entry, root=unregistered_artifact)

    second = tmp_path / "prepared-two"
    batch.prepare(second, batch.NativeBatchConfig(
        scenario=scenario.id, execution="docker", cost_limit=1.0, world_cost_limit=1.0,
        **_wheel_config(approved_wheel),
    ))
    capsys.readouterr()
    second_task = second / scenario.id
    second_seed = second_task / "environment" / "company-store"
    assert len(list(second_seed.rglob("*.json"))) == 1
    assert company_store._entry_path(
        scenario.id, company_store.org_identity(org), entry["fingerprint"], host_store,
    ).is_file()
    assert not company_store._entry_path(
        scenario.id, company_store.org_identity(unregistered_org), unregistered_entry["fingerprint"], host_store,
    ).exists()
    assert not any(path.name == ".native-jobs" for path in first_task.rglob(".native-jobs"))
    assert not any(path.name == ".native-jobs" for path in second_task.rglob(".native-jobs"))
    assert set(company_store.registered_native_jobs_roots(host_store)) == {
        (first / "jobs").resolve(), (second / "jobs").resolve(),
    }


def test_native_company_store_off_is_not_accidentally_enabled(tmp_path, monkeypatch, approved_wheel):
    off_store = tmp_path / "off-store"
    scenario = load_scenario("train_refund_late")
    org = Org(key="off_dynamic", name="Off Example", kind="dynamic_advice",
              email_domains=["off-store.example.test"], web_domains=["off-store.example.test"],
              terms="The published company terms.")
    jobs_root = tmp_path / "prior-jobs"
    entry = _native_store_entry(scenario, org)
    company_store.register_native_jobs_root(jobs_root, root=off_store)
    company_store.save_entry(
        scenario, org, entry,
        root=jobs_root / "job" / "trial" / "artifacts" / "world" / "company-store",
    )
    monkeypatch.setenv("REFUND_EVAL_COMPANY_STORE", "off")
    out = tmp_path / "prepared-off"
    assert adapter.main(["--scenario", "train_refund_late", "--out", str(out),
                         *_wheel_cli_args(approved_wheel)]) == 0
    task_dir = out / "train_refund_late"
    assert not list((task_dir / "environment" / "company-store").rglob("*.json"))
    compose = yaml.safe_load((task_dir / "environment" / "docker-compose.yaml").read_text())
    assert compose["services"]["world"]["environment"]["REFUND_EVAL_COMPANY_STORE"] == "off"
    assert world_entrypoint.initialize_company_store({
        "REFUND_EVAL_COMPANY_STORE": "off", "WORLD_SCENARIO": "train_refund_late",
    }) == 0
    assert company_store.store_root() is None
    assert company_store.registered_native_jobs_roots(off_store) == [jobs_root.resolve()]
    assert not company_store._entry_path(
        scenario.id, company_store.org_identity(org), entry["fingerprint"], off_store,
    ).exists()


def test_cost_limit_schema_documents_target_only_scope():
    field = RefundAgentOptions.model_json_schema()["properties"]["cost_limit"]
    assert "target-only" in field["description"]
    assert "excludes world-service model calls" in field["description"]
    assert field["default"] is None


def test_task_loads_with_harbors_task_model(generated):
    task_dir = generated / "train_refund_late"
    config = Task(task_dir).config
    assert config.schema_version == "1.4"
    assert config.task.name == "refund-eval/train_refund_late"
    meta = config.metadata
    assert meta["task_version"] == adapter.TASK_VERSION and meta["scenario"] == "train_refund_late"
    assert meta["task_id"] == "train_refund_late"
    assert meta["environment_version"] == adapter.ENVIRONMENT_VERSION
    assert meta["environment_fingerprint"] == adapter.environment_fingerprint()
    assert meta["rules_sha256"] == adapter.rules_sha256("train_refund_late") != "none"
    assert len(meta["source_sha256"]) == 64 and "trial name" in meta["seed_policy"]
    assert (meta["sim_weeks"], meta["research_mode"], meta["data_mode"]) == (12.0, "auto", "hybrid")
    assert config.verifier.environment_mode.value == "separate"
    [collect] = config.verifier.collect
    assert collect.service == "world" and "127.0.0.1:9000/agentenv" in collect.command
    assert "$WORLD_ADMIN_TOKEN" in collect.command and "final_snapshot.json" in collect.command
    sources = {(a.source, a.service, a.destination) for a in config.artifacts}
    assert sources == {("/var/log/world", "world", "world"), ("/logs/agent/inspect", None, "agent-eval")}
    [server] = config.environment.mcp_servers
    assert (server.name, server.url) == ("world", "http://world:8000/mcp")
    env = config.environment.env
    assert set(env) == {
        "WORLD_TASK_ID", "WORLD_SCENARIO", "WORLD_SIM_WEEKS", "WORLD_DATA_MODE", "WORLD_TURN_TOKEN",
        "REFUND_EVAL_SHARED_ROLE_MODE", "REFUND_EVAL_SHARED_ROLE_BUDGET_CONFIG",
    }
    assert env["WORLD_TASK_ID"] == ""
    assert env["WORLD_TURN_TOKEN"].startswith("${WORLD_TURN_TOKEN:-") and len(env["WORLD_TURN_TOKEN"]) > 40
    assert config.agent.timeout_sec == 39600 and config.environment.network_mode.value == "public"
    instruction = (task_dir / "instruction.md").read_text()
    assert instruction == task_message(load_scenario("train_refund_late"), 12.0) + "\n"


def test_compose_world_sidecar(generated):
    environment = generated / "train_refund_late" / "environment"
    compose = yaml.safe_load((environment / "docker-compose.yaml").read_text())
    assert compose["services"]["main"] == {"depends_on": {"world": {"condition": "service_healthy"}}}
    world = compose["services"]["world"]
    assert world["build"] == {"context": "${CONTEXT_DIR}", "dockerfile": "world/Dockerfile"}
    assert world["volumes"] == ["world-logs:/var/log/world"] and "world-logs" in compose["volumes"]
    assert world["security_opt"] == ["seccomp=unconfined"]
    assert world["environment"]["OPENROUTER_API_KEY"] == "${OPENROUTER_API_KEY:-}"
    assert world["environment"]["WORLD_TRIAL"] == "${COMPOSE_PROJECT_NAME}"
    assert world["environment"]["WORLD_RESEARCH_MODE"] == "auto"
    assert world["environment"]["WORLD_ADMIN_TOKEN"].startswith("${WORLD_ADMIN_TOKEN:-")
    # The admin token is the world's only; `main` gets the turn token through the task env.
    assert "WORLD_ADMIN_TOKEN" not in Task(generated / "train_refund_late").config.environment.env
    assert "WORLD_ADMIN_TOKEN" not in compose["services"]["main"]
    dockerfile = (environment / "world" / "Dockerfile").read_text()
    assert "HEALTHCHECK" in dockerfile and "8000" in dockerfile and "refund_eval.harbor.world_entrypoint" in dockerfile
    for text in (dockerfile, (environment / "docker-compose.yaml").read_text()):
        assert "__" not in text.replace("__env", "")


@pytest.mark.parametrize("mode", ["auto", "saved", "live"])
def test_adapter_preserves_explicit_research_mode(tmp_path, mode, approved_wheel):
    assert adapter.main(["--scenario", "train_refund_late", "--out", str(tmp_path),
                         "--research-mode", mode, *_wheel_cli_args(approved_wheel)]) == 0
    task_dir = tmp_path / "train_refund_late"
    assert Task(task_dir).config.metadata["research_mode"] == mode
    compose = yaml.safe_load((task_dir / "environment" / "docker-compose.yaml").read_text())
    assert compose["services"]["world"]["environment"]["WORLD_RESEARCH_MODE"] == mode


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_merges_with_harbors_base_file(generated):
    import harbor.environments.docker as harbor_docker

    environment = generated / "train_refund_late" / "environment"
    base = Path(harbor_docker.__file__).with_name("docker-compose-build.yaml")
    env = {**os.environ, "CONTEXT_DIR": str(environment), "WORLD_SCENARIO": "train_refund_late",
           "WORLD_SIM_WEEKS": "12", "WORLD_DATA_MODE": "hybrid", "WORLD_TURN_TOKEN": "t"}
    result = subprocess.run(["docker", "compose", "-p", "trial__abc__env", "--project-directory", str(environment),
                             "-f", str(base), "-f", str(environment / "docker-compose.yaml"), "config", "--format",
                             "json"], env=env, capture_output=True, text=True, check=False)
    if result.returncode and "is not a docker command" in result.stderr:
        pytest.skip("docker compose plugin not installed")
    assert result.returncode == 0, result.stderr
    merged = json.loads(result.stdout)
    assert merged["services"]["world"]["environment"]["WORLD_TRIAL"] == "trial__abc__env"
    assert merged["services"]["main"]["depends_on"]["world"]["condition"] == "service_healthy"


def test_verifier_image_and_test_script(generated):
    tests = generated / "train_refund_late" / "tests"
    test_sh = tests / "test.sh"
    assert test_sh.stat().st_mode & stat.S_IXUSR
    script = test_sh.read_text()
    assert "python -m refund_eval.grading.verifier" in script and "--out /logs/verifier/reward.json" in script
    assert "/var/log/world/state.json" in script and "/logs/agent/inspect/*.eval" in script
    assert "COPY test.sh /tests/test.sh" in (tests / "Dockerfile").read_text()
    for source in (tests / "source", generated / "train_refund_late" / "environment" / "source"):
        assert (source / "refund_eval" / "grading" / "verifier.py").is_file()
        assert (source / "uv.lock").is_file() and not (source / ".env").exists()


def test_main_image_builds_from_only_the_agent_payload(generated, tmp_path, approved_wheel):
    task_dir = generated / "train_refund_late"
    environment = task_dir / "environment"
    payload = environment / "agent"
    wheel_path, wheel_sha256 = approved_wheel
    assert sorted(path.relative_to(payload).as_posix() for path in payload.rglob("*") if path.is_file()) == [
        "agent-wheel.json", "install-agent.py", "pyproject.toml", "uv.lock", f"wheels/{wheel_path.name}",
    ]
    assert not (payload / "refund_eval").exists()
    assert json.loads((payload / "agent-wheel.json").read_text()) == {
        "filename": wheel_path.name, "sha256": wheel_sha256,
    }
    assert hashlib.sha256((payload / "wheels" / wheel_path.name).read_bytes()).hexdigest() == wheel_sha256
    copy_inputs = [line.split()[1:-1] for line in (environment / "Dockerfile").read_text().splitlines()
                   if line.startswith("COPY ") and not line.startswith("COPY --from=")]
    assert copy_inputs == [["agent/pyproject.toml", "agent/uv.lock"], ["agent/"]]
    assert (environment / "Dockerfile.dockerignore").read_text().splitlines() == ["*", "!agent/", "!agent/**"]
    dockerfile = (environment / "Dockerfile").read_text()
    assert "RUN uv sync --frozen --no-dev --no-install-project" in dockerfile
    assert "RUN /app/.venv/bin/python /app/install-agent.py" in dockerfile
    assert "RUN uv sync --frozen --no-dev\n" not in dockerfile
    assert "COPY source/ /app/" in (environment / "world" / "Dockerfile").read_text()
    assert "RUN uv sync --frozen --no-dev && mkdir -p /var/log/world" in (
        environment / "world" / "Dockerfile").read_text()
    assert (task_dir / "tests" / "test.sh").is_file()
    world_source = environment / "source"
    verifier_source = task_dir / "tests" / "source"
    for source in (world_source, verifier_source):
        assert (source / "refund_eval" / "scenario" / "rules" / "train_refund_late").is_dir()
        assert (source / "refund_eval" / "grading" / "verifier.py").is_file()
    world_files = {
        path.relative_to(world_source).as_posix(): path.read_bytes()
        for path in world_source.rglob("*") if path.is_file()
    }
    verifier_files = {
        path.relative_to(verifier_source).as_posix(): path.read_bytes()
        for path in verifier_source.rglob("*") if path.is_file()
    }
    if world_files != verifier_files:
        pytest.fail("Full world and verifier source snapshots differ", pytrace=False)

    # Keep the source staging projection intact for the separate wheel-building path.
    source_payload = tmp_path / "source-agent"
    original = load_scenario()
    adapter.stage_agent(environment / "source", source_payload, original, scenario_name="train_refund_late")
    path = source_payload / "refund_eval" / "scenario" / "train_refund_late.yaml"
    raw = yaml.safe_load(path.read_text())
    assert set(raw) == {"id", "task_text", "start_time", "requested_deadline", "sim_weeks", "user"}
    assert set(raw["user"]) == {"name", "email_addresses", "phone"}
    agent_scenario = load_agent_scenario(str(path))
    assert task_message(agent_scenario, 12.0) == task_message(original, 12.0)
    for source in (environment / "source", task_dir / "tests" / "source"):
        full_source_scenario = load_scenario(
            str(source / "refund_eval" / "scenario" / "train_refund_late.yaml"),
        )
        if full_source_scenario.model_dump(mode="json") != original.model_dump(mode="json"):
            pytest.fail("Full world/verifier scenario input changed during agent staging", pytrace=False)
    manifest = Path("refund_eval/_source_manifest.json")
    agent_manifest = json.loads((source_payload / manifest).read_text())
    world_manifest = json.loads((environment / "source" / manifest).read_text())
    memory = environment / "source" / "refund_eval" / "scenario" / "realism" / "train_refund_late.json"
    assert agent_manifest.pop("realism_memory") == {
        "train_refund_late": hashlib.sha256(memory.read_bytes()).hexdigest()[:8],
    }
    assert "files" in world_manifest
    assert "files" not in agent_manifest
    assert agent_manifest == {key: value for key, value in world_manifest.items() if key != "files"}
    assert agent_manifest["source_sha256"] == Task(task_dir).config.metadata["source_sha256"]


def test_public_projection_wheel_build_and_installed_agent_loader(generated, tmp_path):
    task_dir = generated / "train_refund_late"
    source = task_dir / "environment" / "source"
    payload = tmp_path / "public-agent"
    rail_scenario = load_scenario("train_refund_late").model_copy(update={
        "requested_deadline": date(2027, 2, 4),
        "sim_weeks": 365 / 7,
    })
    bills_scenario = load_scenario("bills_price_rise")
    for scenario in (rail_scenario, bills_scenario):
        adapter.stage_agent(source, payload, scenario, scenario_name=scenario.id)

    build_dir = tmp_path / "wheelhouse"
    subprocess.run(
        ["uv", "build", "--offline", "--wheel", "--out-dir", str(build_dir)],
        cwd=payload, check=True, capture_output=True, text=True,
    )
    [wheel] = build_dir.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        scenario_members = sorted(
            name for name in archive.namelist()
            if name.startswith("refund_eval/scenario/") and name.endswith(".yaml")
        )
        assert scenario_members == [
            "refund_eval/scenario/bills_price_rise.yaml",
            "refund_eval/scenario/train_refund_late.yaml",
        ]
        for member in scenario_members:
            projection = yaml.safe_load(archive.read(member))
            assert set(projection) == {
                "id", "task_text", "start_time", "requested_deadline", "sim_weeks", "user",
            }
            assert set(projection["user"]) == {"name", "email_addresses", "phone"}

    installed = tmp_path / "installed-wheel"
    subprocess.run(
        ["uv", "pip", "install", "--offline", "--no-deps", "--python", sys.executable,
         "--target", str(installed), str(wheel)],
        check=True, capture_output=True, text=True,
    )
    loader_check = textwrap.dedent("""
        from datetime import date
        import math
        import os
        from pathlib import Path
        import refund_eval.scenario.schema as schema

        assert Path(schema.__file__).resolve().is_relative_to(Path(os.environ["PUBLIC_WHEEL_INSTALL"]).resolve())
        rail = schema.load_agent_scenario("train_refund_late")
        assert rail.id == "train_refund_late"
        assert rail.requested_deadline == date(2027, 2, 4)
        assert math.isclose(rail.sim_weeks * 7, 365, rel_tol=0, abs_tol=1e-9)
        bills = schema.load_agent_scenario("bills_price_rise")
        assert bills.id == "bills_price_rise"
        assert bills.requested_deadline == date.fromisoformat(os.environ["BILLS_REQUESTED_DEADLINE"]) \
            if os.environ["BILLS_REQUESTED_DEADLINE"] else bills.requested_deadline is None
        assert math.isclose(bills.sim_weeks, float(os.environ["BILLS_SIM_WEEKS"]), rel_tol=0, abs_tol=1e-9)
    """)
    environment = {
        **os.environ,
        "PUBLIC_WHEEL_INSTALL": str(installed),
        "BILLS_REQUESTED_DEADLINE": bills_scenario.requested_deadline.isoformat()
        if bills_scenario.requested_deadline else "",
        "BILLS_SIM_WEEKS": str(bills_scenario.sim_weeks),
    }
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(installed), environment.get("PYTHONPATH", "")) if value
    )
    subprocess.run(
        [sys.executable, "-c", loader_check], cwd=tmp_path, env=environment,
        check=True, capture_output=True, text=True,
    )
    replay_check = textwrap.dedent("""
        import asyncio
        import json
        import os
        from pathlib import Path

        import httpx2
        from inspect_ai.model import ChatMessageTool, ChatMessageUser, GenerateConfig, get_model
        from inspect_ai.tool import ToolInfo, ToolParams
        import refund_eval.agent.dollar_budget as budget

        assert Path(budget.__file__).resolve().is_relative_to(
            Path(os.environ["PUBLIC_WHEEL_INSTALL"]).resolve()
        )
        budget.openrouter_tariffs = lambda *_: (_ for _ in ()).throw(
            AssertionError("usage_only must not load tariffs")
        )
        requests = []

        def transport(request):
            assert request.url.path == "/api/v1/chat/completions"
            payload = json.loads(request.content)
            output_limits = [
                payload[name] for name in ("max_tokens", "max_completion_tokens") if name in payload
            ]
            assert output_limits and max(output_limits) == 4096
            requests.append(payload)
            if len(requests) == 1:
                message = {
                    "role": "assistant",
                    "content": "Synthetic tool-call text.",
                    "tool_calls": [{
                        "id": "call-offline",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }],
                    "reasoning_details": [{
                        "type": "reasoning.encrypted",
                        "id": "synthetic-reasoning-id",
                        "format": "synthetic",
                        "index": 0,
                        "data": "Synthetic encrypted reasoning payload.",
                    }],
                }
                assert message["content"] == "Synthetic tool-call text."
                finish_reason = "tool_calls"
            else:
                assert any(
                    item.get("role") == "assistant" and item.get("tool_calls")
                    for item in payload["messages"]
                )
                assert any(item.get("role") == "tool" for item in payload["messages"])
                message = {"role": "assistant", "content": "Offline replay complete."}
                finish_reason = "stop"
            return httpx2.Response(200, request=request, json={
                "id": f"offline-{len(requests)}",
                "object": "chat.completion",
                "created": 0,
                "model": payload["model"],
                "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
            })

        async def main():
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(transport)) as client:
                model = get_model(
                    "openrouter/openai/gpt-6-luna",
                    http_client=client,
                    memoize=False,
                    config=GenerateConfig(max_tokens=8192, max_retries=2, cache_prompt=False),
                    stream=False,
                )
                async with budget.target_dollar_budget(model):
                    assert model.config.max_retries == 0
                    first = await model.generate(
                        "Synthetic request.",
                        tools=[ToolInfo(name="lookup", description="Offline lookup.", parameters=ToolParams())],
                    )
                    assistant = first.choices[0].message
                    assert isinstance(assistant.content, list)
                    assert {type(block).__name__ for block in assistant.content} >= {
                        "ContentReasoning", "ContentText",
                    }
                    assert any(
                        type(block).__name__ == "ContentReasoning" and block.redacted
                        for block in assistant.content
                    )
                    call = assistant.tool_calls[0]
                    final = await model.generate(
                        [
                            ChatMessageUser(content="Synthetic request."),
                            assistant,
                            ChatMessageTool(
                                content="Synthetic tool result.", tool_call_id=call.id, function=call.function
                            ),
                            ChatMessageUser(content="Summarize the synthetic result."),
                        ],
                        tools=[ToolInfo(name="lookup", description="Offline lookup.", parameters=ToolParams())],
                    )
                    assert first.metadata["usage_reported_cost_usd"] is None
                    assert first.usage.total_cost is None
                    assert final.metadata["usage_reported_cost_usd"] is None
                    assert final.usage.total_cost is None
                assert model.config.max_retries == 2
            assert len(requests) == 2

        asyncio.run(main())
    """)
    replay_environment = {
        **environment,
        "OPENROUTER_API_KEY": "offline-key",
        "REFUND_EVAL_USAGE_REPORTING_MODE": "usage_only",
        "REFUND_EVAL_SHARED_ROLE_MODE": "shared_caps",
        "REFUND_EVAL_SHARED_CAPS_HEADROOM_BOUND": "true",
        "REFUND_EVAL_AGENT_DOLLAR_CAP": "50",
        "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS": "4096",
        "WORLD_SCENARIO": "train_refund_late",
    }
    repo_root = Path(__file__).resolve().parents[1]
    replay_python = subprocess.run(
        [
            "uv", "run", "--project", str(repo_root), "--frozen",
            "python", "-c", "import sys; print(sys.executable)",
        ],
        cwd=repo_root, check=True, capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        [replay_python, "-c", replay_check], cwd=tmp_path, env=replay_environment,
        check=True, capture_output=True, text=True,
    )


@pytest.mark.parametrize("days", [365, 730])
def test_staged_public_scenario_keeps_requested_deadline_separate_from_horizon(generated, tmp_path, days):
    sim_weeks = days / 7
    scenario = load_scenario().model_copy(update={
        "requested_deadline": date(2027, 2, 4),
        "sim_weeks": sim_weeks,
        "task_text": "Please complete this by $deadline.",
    })
    source = generated / "train_refund_late" / "environment" / "source"
    destination = tmp_path / "agent"
    adapter.stage_agent(source, destination, scenario, scenario_name="train_refund_late")

    path = destination / "refund_eval" / "scenario" / "train_refund_late.yaml"
    raw = yaml.safe_load(path.read_text())
    assert set(raw) == {"id", "task_text", "start_time", "requested_deadline", "sim_weeks", "user"}
    assert raw["requested_deadline"] == "2027-02-04"
    assert set(raw["user"]) == {"name", "email_addresses", "phone"}
    public = load_agent_scenario(str(path))
    assert public.requested_deadline == date(2027, 2, 4)
    assert public.sim_weeks == sim_weeks
    expected_task = "Please complete this by 4 February 2027."
    assert task_message(scenario, sim_weeks) == expected_task
    assert task_message(public, sim_weeks) == expected_task

    horizon = format_long(sim_deadline(SimpleNamespace(start_time=scenario.start_time), sim_weeks)).split(",")[0]
    first_name = scenario.user.name.split()[0]
    expected_context = (f"{first_name} is away and unreachable. Requested task deadline: 4 February 2027. "
                        f"Your session ends on {horizon}: anything not done by that point will not get done.")
    assert expected_context in render_system_prompt(scenario, sim_weeks=sim_weeks)
    assert expected_context in render_system_prompt(public, sim_weeks=sim_weeks)


@pytest.mark.parametrize("sim_weeks", [1.0, 365 / 7])
def test_native_shared_roles_requested_deadline_reaches_target_without_world_overlay(
    tmp_path, approved_split_budget_wheel, sim_weeks
):
    wheel, digest = approved_split_budget_wheel
    deadline = date(2027, 2, 4)
    out = tmp_path / "requested-deadline"
    config = batch.NativeBatchConfig(
        scenario="train_refund_late", execution="modal", data_mode="fixture", research_mode="saved",
        model="openrouter/target", simulator_model="openrouter/simulator", grader_model="openrouter/grader",
        cost_limit=50, world_cost_limit=100, max_turns=50, sim_weeks=sim_weeks, time_limit=120,
        openrouter_key_mode="shared_roles", shared_allowance_usd=90, agent_max_output_tokens=2048,
        requested_deadline=deadline, agent_wheel=wheel, agent_wheel_sha256=digest,
    )
    record = json.loads(batch.prepare(out, config).read_text(encoding="utf-8"))
    task_dir = out / "train_refund_late"
    task = Task(task_dir)
    metadata = task.config.metadata
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_bytes()))
    assert job.agents[0].kwargs["requested_deadline"] == deadline
    assert metadata["requested_deadline"] == "2027-02-04"
    assert metadata["sim_weeks"] == sim_weeks
    assert record["config"]["requested_deadline"] == "2027-02-04"
    assert record["effective_parameters"]["requested_deadline"] == "2027-02-04"
    assert record["effective_parameters"]["sim_weeks"] == sim_weeks

    with zipfile.ZipFile(wheel) as archive:
        public_data = yaml.safe_load(archive.read("refund_eval/scenario/train_refund_late.yaml"))
    assert set(public_data) == {"id", "task_text", "start_time", "requested_deadline", "sim_weeks", "user"}
    public_scenario = AgentScenario.model_validate(public_data)
    assert public_scenario.task_text == load_scenario().task_text
    assert public_scenario.requested_deadline is None
    requested_deadline_arg = f"requested_deadline={deadline.isoformat()}"
    command = make_agent(
        tmp_path, model_name="openrouter/target", cost_limit=50, requested_deadline=deadline
    ).inspect_command()
    assert f"-T {requested_deadline_arg}" in command
    assert '-T sim_weeks="$WORLD_SIM_WEEKS"' in command

    installed_package = tmp_path / "installed-public-wheel"
    installed_package.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed_package)
    script = textwrap.dedent("""\
        import json
        import sys
        from datetime import date
        from pathlib import Path

        from inspect_ai._cli.util import parse_cli_config
        from inspect_ai._eval.loader import resolve_tasks
        from inspect_ai.model import get_model

        import refund_eval.task as task_module
        from refund_eval.published import provider as published_provider
        from refund_eval.agent.prompt import _deadline, render_system_prompt, with_requested_deadline
        from refund_eval.scenario.schema import load_agent_scenario

        task_file = Path(sys.argv[1]).resolve()
        task_spec = task_file.relative_to(Path.cwd()).as_posix()
        task_args = parse_cli_config(sys.argv[2:], None)
        assert type(task_args["requested_deadline"]) is date
        task_module.register_default_costs = lambda *args, **kwargs: None
        task_module.default_model_roles = lambda *args, **kwargs: {}
        task_module._given_role_models = lambda *args, **kwargs: []
        published_provider.cache_conversation_prefix = lambda: None
        published_provider.check_published_model = lambda *args, **kwargs: None
        resolved = resolve_tasks(
            task_spec, task_args, get_model("mockllm/model"), None, None, None
        )
        task = resolved[0].task
        scenario = with_requested_deadline(
            load_agent_scenario("train_refund_late"), task_args["requested_deadline"]
        )
        prompt = render_system_prompt(scenario, sim_weeks=task_args["sim_weeks"])
        print("TASK_RESULT=" + json.dumps({
            "task_file": str(Path(task_module.__file__).resolve()),
            "task_input": task.dataset[0].input,
            "task_metadata": task.dataset[0].metadata,
            "task_deadline": task.metadata["requested_deadline"],
            "requested_deadline_type": type(task_args["requested_deadline"]).__name__,
            "system_prompt": prompt,
            "session_end": _deadline(scenario, task_args["sim_weeks"]),
        }))
    """)
    task_cli_args = [
        "scenario=train_refund_late",
        f"sim_weeks={sim_weeks}",
        "published_data=fixture",
        "research_mode=saved",
        "world_url=http://world:8000",
        "cost_limit=50.0",
        requested_deadline_arg,
    ]
    child_env = os.environ.copy()
    for name in ("OPENROUTER_API_KEY", "OPENROUTER_AGENT_API_KEY", "OPENROUTER_WORLD_API_KEY"):
        child_env.pop(name, None)
    child_env.update({
        "INSPECT_EVAL_MODEL": "mockllm/model",
        "PYTHONPATH": str(installed_package),
        "PYTHON_DOTENV_DISABLED": "1",
    })
    result = subprocess.run(
        [sys.executable, "-c", script, str(installed_package / "refund_eval" / "task.py"), *task_cli_args],
        cwd=tmp_path, env=child_env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    loaded = json.loads(next(line.removeprefix("TASK_RESULT=") for line in result.stdout.splitlines()
                             if line.startswith("TASK_RESULT=")))
    assert loaded["task_file"] == str((installed_package / "refund_eval" / "task.py").resolve())
    assert loaded["requested_deadline_type"] == "date"
    assert loaded["task_metadata"]["requested_deadline"] == "2027-02-04"
    assert loaded["task_deadline"] == "2027-02-04"
    assert "by the 4 February 2027 HARD deadline" in loaded["task_input"]

    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8").strip()
    assert instruction == loaded["task_input"]
    assert "Requested task deadline: 4 February 2027" in loaded["system_prompt"]
    assert f"Your session ends on {loaded['session_end']}" in loaded["system_prompt"]
    assert loaded["session_end"] != "4 February 2027"

    world_source = task_dir / "environment" / "source"
    scenario_bytes = (world_source / "refund_eval/scenario/train_refund_late.yaml").read_bytes()
    assert scenario_bytes == (
        adapter.ROOT / "refund_eval/scenario/train_refund_late.yaml"
    ).read_bytes()
    assert b"5 October 2026" in scenario_bytes
    rules = Path("refund_eval/scenario/rules/train_refund_late")
    assert {
        path.relative_to(rules).as_posix(): path.read_bytes() for path in rules.rglob("*") if path.is_file()
    } == {
        path.relative_to(world_source / rules).as_posix(): path.read_bytes()
        for path in (world_source / rules).rglob("*") if path.is_file()
    }
    compose = yaml.safe_load((task_dir / "environment/docker-compose.yaml").read_bytes())
    world_env = compose["services"]["world"]["environment"]
    assert world_env["WORLD_SIM_WEEKS"] == "${WORLD_SIM_WEEKS}"
    assert "WORLD_REQUESTED_DEADLINE" not in world_env
    assert metadata["rules_sha256"] == record["task"]["rules_sha256"]


def test_usage_only_mode_preserves_finite_native_bounds(tmp_path, approved_split_budget_wheel):
    wheel, digest = approved_split_budget_wheel
    out = tmp_path / "usage-only"
    deadline = date(2027, 2, 4)
    config = batch.NativeBatchConfig(
        scenario="train_refund_late", execution="modal", data_mode="hybrid", research_mode="auto",
        model="openrouter/target", simulator_model="openrouter/simulator", grader_model="openrouter/grader",
        cost_limit=50, world_cost_limit=100, max_turns=3000, sim_weeks=365 / 7, time_limit=36000,
        openrouter_key_mode="shared_caps", agent_max_output_tokens=4096, requested_deadline=deadline,
        usage_reporting_mode="usage_only", disable_verifier=True, trials=1, concurrency=1,
        agent_wheel=wheel, agent_wheel_sha256=digest,
    )
    record_path = batch.prepare(out, config)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    task = Task(out / "train_refund_late")
    metadata = task.config.metadata
    compose = yaml.safe_load((out / "train_refund_late/environment/docker-compose.yaml").read_bytes())
    world_env = compose["services"]["world"]["environment"]
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_bytes()))
    [agent] = job.agents

    assert record["effective_parameters"]["usage_reporting_mode"] == "usage_only"
    assert record["effective_parameters"]["cost_limit_enforcement"] == "response_reported_observed_not_pre_call"
    assert record["effective_parameters"]["max_turns"] == 3000
    assert record["effective_parameters"]["worker_time_limit_seconds"] == 36000
    assert record["effective_parameters"]["requested_deadline"] == deadline.isoformat()
    assert metadata["usage_reporting_mode"] == "usage_only"
    assert metadata["max_turns"] == 3000
    assert metadata["requested_deadline"] == deadline.isoformat()
    assert world_env["REFUND_EVAL_USAGE_REPORTING_MODE"] == "usage_only"
    assert agent.env["REFUND_EVAL_USAGE_REPORTING_MODE"] == "usage_only"
    assert agent.env["REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS"] == "4096"
    assert agent.kwargs["requested_deadline"] == deadline
    assert agent.kwargs["cost_limit"] == 50
    assert job.retry.max_retries == 0
    assert job.verifier.disable is True
    manifest = tmp_path / "usage-only-manifest.json"
    assert laura_trial_main(["save", "--config", str(out / "job.yaml"), "--manifest", str(manifest)]) == 0
    assert laura_trial_main(["verify", "--manifest", str(manifest)]) == 0


def test_compose_overlay_skips_null_without_bypassing_other_services(tmp_path):
    from scripts.laura_trial import SetupFailure, _validate_compose_overlay

    overlay = tmp_path / "compose.yaml"
    overlay.write_text("services:\n  agent: null\n", encoding="utf-8")
    _validate_compose_overlay(overlay)

    overlay.write_text(yaml.safe_dump({
        "services": {"agent": None, "unsafe": {"build": "."}},
    }, sort_keys=False), encoding="utf-8")
    with pytest.raises(SetupFailure):
        _validate_compose_overlay(overlay)


def test_laura_setup_verifies_null_service_overlay_offline(tmp_path, approved_split_budget_wheel):
    wheel, digest = approved_split_budget_wheel
    out = tmp_path / "batch"
    config = batch.NativeBatchConfig(
        scenario="train_refund_late", execution="modal", data_mode="fixture", research_mode="saved",
        model="openrouter/target", simulator_model="openrouter/simulator", grader_model="openrouter/grader",
        cost_limit=1, world_cost_limit=1, trials=1, concurrency=1,
        agent_wheel=wheel, agent_wheel_sha256=digest,
    )
    batch.prepare(out, config)

    compose_overlay = tmp_path / "compose-overlay.yaml"
    compose_overlay.write_text("services:\n  agent: null\n", encoding="utf-8")
    job_path = out / "job.yaml"
    job_document = yaml.safe_load(job_path.read_bytes())
    job_document["environment"]["extra_docker_compose"] = [str(compose_overlay)]
    job_path.write_text(yaml.safe_dump(job_document, sort_keys=False), encoding="utf-8")

    manifest = tmp_path / "laura-setup.json"
    assert laura_trial_main(["save", "--config", str(job_path), "--manifest", str(manifest)]) == 0
    assert laura_trial_main(["verify", "--manifest", str(manifest)]) == 0


@pytest.mark.parametrize("service", [
    False,
    [],
    "not-a-service",
    {"build": "."},
    {"develop": {"watch": []}},
    {"env_file": [".env"]},
    {"label_file": ["labels.env"]},
    {"extends": {"file": "other-compose.yaml"}},
    {"volumes": ["./workspace:/workspace"]},
])
def test_compose_overlay_rejects_invalid_or_unsafe_service_values(tmp_path, service):
    from scripts.laura_trial import SetupFailure, _validate_compose_overlay

    overlay = tmp_path / "compose.yaml"
    overlay.write_text(yaml.safe_dump({"services": {"agent": service}}, sort_keys=False), encoding="utf-8")
    with pytest.raises(SetupFailure):
        _validate_compose_overlay(overlay)


def test_usage_only_can_disable_application_price_cutoffs_under_shared_caps(
        tmp_path, approved_split_budget_wheel):
    wheel, digest = approved_split_budget_wheel
    out = tmp_path / "usage-only-no-price-cutoffs"
    config = batch.NativeBatchConfig(
        scenario="train_refund_late", execution="modal", data_mode="hybrid", research_mode="auto",
        model="openrouter/target", simulator_model="openrouter/simulator", grader_model="openrouter/grader",
        cost_limit=50, world_cost_limit=100, max_turns=3000, sim_weeks=365 / 7, time_limit=36000,
        openrouter_key_mode="shared_caps", agent_max_output_tokens=4096,
        requested_deadline=date(2027, 2, 4), usage_reporting_mode="usage_only",
        disable_application_price_cutoffs=True, disable_verifier=True, trials=1, concurrency=1,
        agent_wheel=wheel, agent_wheel_sha256=digest,
    )
    record_path = batch.prepare(out, config)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    task = Task(out / "train_refund_late")
    metadata = task.config.metadata
    compose = yaml.safe_load((out / "train_refund_late/environment/docker-compose.yaml").read_bytes())
    world_env = compose["services"]["world"]["environment"]
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_bytes()))
    [agent] = job.agents

    assert config.disable_application_price_cutoffs is True
    assert metadata["disable_application_price_cutoffs"] == "true"
    assert world_env["REFUND_EVAL_DISABLE_APPLICATION_PRICE_CUTOFFS"] == "true"
    assert world_env["WORLD_COST_LIMIT"] == "100.0"
    assert agent.kwargs["cost_limit"] == 50
    assert agent.env["REFUND_EVAL_USAGE_REPORTING_MODE"] == "usage_only"
    assert record["effective_parameters"]["effective_application_price_cutoffs_usd"] == {
        "agent": None, "world": None,
    }
    assert record["effective_parameters"]["shared_caps_guard_role_limits_usd"] == {
        "agent": 50, "world": 100,
    }
    assert record["target_cost_limits_usd"] is None
    assert record["world_cost_limit_usd_per_trial"] is None
    assert record["shared_caps_guard_role_limits_usd"] == {"agent": 50, "world": 100}
    assert job.retry.max_retries == 0
    assert job.verifier.disable is True

    manifest = tmp_path / "usage-only-no-price-cutoffs-manifest.json"
    assert laura_trial_main(["save", "--config", str(out / "job.yaml"), "--manifest", str(manifest)]) == 0
    assert laura_trial_main(["verify", "--manifest", str(manifest)]) == 0


@pytest.mark.parametrize("stale_source", [
    "refund_eval/task.py",
    "refund_eval/agent/prompt.py",
    "refund_eval/agent/harness.py",
])
def test_split_budget_wheel_attests_deadline_task_sources(
    tmp_path, approved_split_budget_wheel, stale_source
):
    wheel, _ = approved_split_budget_wheel
    required_sources = {
        "refund_eval/task.py",
        "refund_eval/agent/prompt.py",
        "refund_eval/agent/harness.py",
    }
    assert required_sources <= set(adapter.SPLIT_BUDGET_WHEEL_SOURCES)
    assert required_sources <= set(adapter.verify_split_budget_wheel(wheel))

    stale_wheel = tmp_path / f"stale-{Path(stale_source).name}.whl"
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(stale_wheel, "w") as stale:
        for member in source.infolist():
            contents = source.read(member.filename)
            if member.filename == stale_source:
                contents += b"\n"
            stale.writestr(member, contents)

    with pytest.raises(ValueError, match="split budget source"):
        adapter.verify_split_budget_wheel(stale_wheel)


@pytest.mark.parametrize("mode", ["shared", "split"])
def test_requested_deadline_requires_guarded_shared_key_mode(mode, approved_wheel):
    wheel, digest = approved_wheel
    options = {
        "scenario": "train_refund_late", "execution": "modal", "data_mode": "fixture", "research_mode": "saved",
        "model": "openrouter/target", "simulator_model": "openrouter/simulator", "grader_model": "openrouter/grader",
        "cost_limit": 50, "world_cost_limit": 100, "requested_deadline": date(2027, 2, 4),
        "agent_wheel": wheel, "agent_wheel_sha256": digest, "openrouter_key_mode": mode,
    }
    if mode == "split":
        options["agent_max_output_tokens"] = 2048
    with pytest.raises(ValueError, match="requested_deadline is only valid in a guarded shared-key mode"):
        batch.NativeBatchConfig(**options)


def test_task_uses_scenario_filename_when_it_differs_from_id(tmp_path, monkeypatch, approved_wheel):
    original = load_scenario()
    name = "renamed_refund"
    scenarios = tmp_path / "scenarios"
    scenarios.mkdir()
    (scenarios / f"{name}.yaml").write_text(yaml.safe_dump(original.model_dump(mode="json")))
    monkeypatch.setattr(schema, "SCENARIO_DIR", scenarios)
    out = tmp_path / "tasks"
    assert adapter.main(["--scenario", name, "--out", str(out), *_wheel_cli_args(approved_wheel)]) == 0
    task_dir = out / name
    lookup = Task(task_dir).config.environment.env["WORLD_SCENARIO"]
    assert lookup == name
    assert not (task_dir / "environment" / "agent" / "refund_eval").exists()
    assert original.id != lookup
    source_payload = tmp_path / "legacy-agent"
    adapter.stage_agent(
        task_dir / "environment" / "source", source_payload, original, scenario_name=name,
    )
    monkeypatch.setattr(schema, "SCENARIO_DIR", source_payload / "refund_eval" / "scenario")
    assert load_agent_scenario(lookup).id == original.id


def _normalize_scenario_string(value):
    return " ".join(value.split()).casefold()


def _scenario_leaf_strings(value, path="", generic_subject=None):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            yield from _scenario_leaf_strings(child, child_path, generic_subject)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _scenario_leaf_strings(child, f"{path}[{index}]", generic_subject)
    elif isinstance(value, str):
        parent, separator, field = path.rpartition(".")
        if separator and field == "kind" and parent.startswith("orgs[") and parent.endswith("]"):
            # Organization kinds are schema categories, not scenario-specific facts.
            return
        normalized = _normalize_scenario_string(value)
        if path == "subject" and generic_subject is not None and normalized == generic_subject:
            return
        common_labels = {
            "train_refund", "train_refund_late", "bills_price_rise", "refund", "train", "ticket",
            "journey", "booking", "cancellation", "cancelled", "customer", "company", "operator",
            "email", "phone", "scenario", "facts", "orgs", "true", "false", "none",
        }
        if len(normalized) >= 6 and len(set(normalized)) >= 4 and normalized not in common_labels:
            yield normalized


def _private_scenario_values(data, generic_subject=None):
    public = {
        "id": data.pop("id"),
        "task_text": data.pop("task_text"),
        "start_time": data.pop("start_time"),
        "sim_weeks": data.pop("sim_weeks"),
        "user": {key: data["user"].pop(key) for key in ("name", "email_addresses", "phone")
                 if key in data["user"]},
    }
    public_text = " ".join(_scenario_leaf_strings(public))
    return {
        value for value in _scenario_leaf_strings(data, generic_subject=generic_subject)
        if value not in public_text
    }


def _generic_subject_for_scenario(scenario, source_yaml):
    if "subject" in source_yaml:
        return None
    schema_default = schema.Scenario.model_fields["subject"].default
    if isinstance(schema_default, str) and _normalize_scenario_string(scenario.subject) == (
        _normalize_scenario_string(schema_default)
    ):
        return _normalize_scenario_string(schema_default)
    if source_yaml.get("extends") != "train_refund":
        return None
    base_path = Path("refund_eval/scenario/archive/train_refund.yaml")
    base_yaml = yaml.safe_load(base_path.read_text(encoding="utf-8"))
    inherited_subject = base_yaml.get("subject")
    if isinstance(inherited_subject, str) and _normalize_scenario_string(scenario.subject) == (
        _normalize_scenario_string(inherited_subject)
    ):
        # The active scenario inherits this generic subject from its archived template.
        return _normalize_scenario_string(inherited_subject)
    return None


def _find_private_value_asset(hidden_values, package):
    for path in package.rglob("*"):
        if path.is_file():
            name = path.relative_to(package).as_posix().casefold()
            contents = name + " " + b" ".join(path.read_bytes().split()).decode("utf-8", errors="ignore").casefold()
            if any(value in contents for value in hidden_values):
                return path
    return None


def test_scripted_parity_modules_are_opt_in_and_preserve_overrides(generated, tmp_path):
    from refund_eval.parity.run import stage_scripted_agent

    source_environment = generated / "train_refund_late" / "environment"
    source_agent = source_environment / "agent"
    assert not (source_agent / "refund_eval" / "parity").exists()
    test_environment = tmp_path / "environment"
    test_environment.mkdir()
    shutil.copyfile(source_environment / "Dockerfile", test_environment / "Dockerfile")
    staged_agent = test_environment / "agent"
    shutil.copytree(source_agent, staged_agent)

    stage_scripted_agent(tmp_path)
    parity = staged_agent / "parity-fixtures" / "refund_eval" / "parity"
    assert {path.name for path in parity.glob("*.py")} == {"__init__.py", "inspect_cli.py", "scripts.py"}
    assert not (staged_agent / "refund_eval" / "parity").exists()
    dockerfile = (test_environment / "Dockerfile").read_text()
    marker = "# TEST-ONLY: overlay scripted parity fixtures after the approved wheel install"
    assert dockerfile.index("RUN /app/.venv/bin/python /app/install-agent.py") < dockerfile.index(marker)
    assert dockerfile.count(marker) == 1
    assert "copytree('/app/parity-fixtures/refund_eval/parity'" in dockerfile

    scripts = parity / "scripts.py"
    override = "fixture override preserved\n"
    scripts.write_text(override, encoding="utf-8")
    stage_scripted_agent(tmp_path)
    assert scripts.read_text(encoding="utf-8") == override
    assert (test_environment / "Dockerfile").read_text().count(marker) == 1


def test_payload_scan_checks_source_comments_and_subject_overrides(tmp_path):
    canary = "synthetic-private-scenario-canary-48371"
    source = tmp_path / "refund_eval" / "source_comment.py"
    source.parent.mkdir(parents=True)
    source.write_text(f"# {canary}\n", encoding="utf-8")

    default_subject = "generic scenario subject default"
    override = "non-default subject override canary"
    private_kind = "synthetic-private-kind-canary-48371"
    assert _generic_subject_for_scenario(
        SimpleNamespace(subject=override), {"subject": override},
    ) is None
    hidden_values = _private_scenario_values(
        {
            "id": "synthetic-case-1",
            "task_text": "public request",
            "start_time": "2025-01-01T00:00:00Z",
            "sim_weeks": 1,
            "user": {"name": "Synthetic User", "email_addresses": [], "phone": None},
            "subject": override,
            "private_fact": canary,
            "facts": {"journey": {"kind": private_kind}},
        },
        generic_subject=_normalize_scenario_string(default_subject),
    )
    assert _normalize_scenario_string(override) in hidden_values
    assert _normalize_scenario_string(canary) in hidden_values
    assert _normalize_scenario_string(private_kind) in hidden_values
    assert not list(_scenario_leaf_strings({"orgs": [{"kind": "synthetic-org-category"}]}))
    assert _find_private_value_asset(hidden_values, tmp_path) == source


def test_agent_payload_excludes_hidden_scenario_values(generated, tmp_path):
    scenario = load_scenario("train_refund_late")
    source_yaml = yaml.safe_load(Path("refund_eval/scenario/train_refund_late.yaml").read_text())
    assert "subject" not in source_yaml
    generic_subject = _generic_subject_for_scenario(scenario, source_yaml)
    hidden_values = _private_scenario_values(
        scenario.model_dump(mode="json"), generic_subject=generic_subject,
    )
    if not hidden_values:
        pytest.fail("No eligible private scenario values were available for the payload audit", pytrace=False)

    source = generated / "train_refund_late" / "environment" / "source"
    source_payload = tmp_path / "source-agent"
    adapter.stage_agent(source, source_payload, scenario, scenario_name="train_refund_late")
    package = source_payload / "refund_eval"
    asset = _find_private_value_asset(hidden_values, package)
    if asset is not None:
        pytest.fail(
            f"Private scenario value detected in staged package asset {asset.relative_to(package)}",
            pytrace=False,
        )


def test_dataset_metric_and_job(generated):
    manifest = DatasetManifest.from_toml_file(generated / "dataset.toml")
    assert [t.name for t in manifest.tasks] == ["refund-eval/train_refund_late"]
    assert [f.path for f in manifest.files] == ["metric.py"]
    assert (generated / "metric.py").read_text() == adapter.METRIC.read_text()
    job = JobConfig.model_validate(yaml.safe_load((generated / "job.yaml").read_text()))
    [agent] = job.agents
    assert agent.import_path == adapter.AGENT_IMPORT_PATH
    assert agent.kwargs["cost_limit"] == 150.0 and agent.kwargs["token_budget"] == 40_000_000
    assert agent.env == {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"}
    assert not job.verifier.disable
    assert job.retry.max_retries == 0


def test_batch_can_disable_verifier_and_keeps_trace_artifacts(tmp_path, approved_wheel):
    config = batch.NativeBatchConfig(
        execution="docker", scenario="train_refund_late", data_mode="fixture", research_mode="saved",
        cost_limit=1, world_cost_limit=1, sim_weeks=1, disable_verifier=True, **_wheel_config(approved_wheel),
    )
    out = tmp_path / "ungraded"
    record_path = batch.prepare(out, config)

    task_path = out / "train_refund_late"
    task_config = Task(task_path).config
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_text()))
    assert job.verifier.disable
    assert {(a.source, a.service, a.destination) for a in task_config.artifacts} == {
        ("/var/log/world", "world", "world"), ("/logs/agent/inspect", None, "agent-eval"),
    }
    [collect] = task_config.verifier.collect
    assert collect.service == "world" and "final_snapshot.json" in collect.command
    record = json.loads(record_path.read_text())
    assert record["task"]["digest"] == f"sha256:{Packager.compute_content_hash(task_path)[0]}"
    job_bytes = (out / "job.yaml").read_bytes()
    assert record["job"]["sha256"] == hashlib.sha256(job_bytes).hexdigest()


def test_adapter_refuses_to_overwrite(generated, approved_wheel):
    with pytest.raises(FileExistsError):
        adapter.main(["--scenario", "train_refund_late", "--out", str(generated),
                      *_wheel_cli_args(approved_wheel)])


@pytest.mark.parametrize(("execution", "seed"), [("docker", None), ("modal", 0)])
def test_batch_freezes_one_task_and_the_job_for_all_targets(tmp_path, capsys, monkeypatch, execution, seed,
                                                            approved_wheel):
    monkeypatch.setenv("WORLD_SEED", "17")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-packaged")
    config = tmp_path / "config.json"
    out = tmp_path / "private run"
    config.write_text(json.dumps({
        "execution": execution, "scenario": "train_refund_late", "data_mode": "fixture",
        "research_mode": "saved", "models": ["openrouter/a", "openrouter/b"],
        "cost_limit": {"openrouter/a": 1.0, "openrouter/b": 2.0}, "world_cost_limit": 0.75,
        "max_turns": 50, "sim_weeks": 3, "time_limit": 120, "history_tokens": 0,
        "budget_notice": "daily", "trials": 3, "concurrency": 2, "seed": seed,
        "agent_wheel": str(approved_wheel[0]), "agent_wheel_sha256": approved_wheel[1],
    }))
    assert batch.main(["--config", str(config), "--out", str(out)]) == 0
    assert f"scripts/harbor harbor run -c '{out / 'job.yaml'}'" in capsys.readouterr().out
    job_bytes = (out / "job.yaml").read_bytes()
    job = JobConfig.model_validate(yaml.safe_load(job_bytes))
    task = out / "train_refund_late"
    [job_task] = job.tasks
    assert job_task.path == task
    assert job.environment.type.value == execution and job.retry.max_retries == 0
    assert (job.n_attempts, job.n_concurrent_trials) == (3, 2)
    assert [(a.model_name, a.kwargs["cost_limit"]) for a in job.agents] == [
        ("openrouter/a", 1.0), ("openrouter/b", 2.0),
    ]
    for agent in job.agents:
        assert agent.kwargs == {"cost_limit": agent.kwargs["cost_limit"], "token_budget": 40_000_000,
                                "time_limit": 120, "history_tokens": 0, "budget_notice": "daily"}
    compose = (task / "environment" / "docker-compose.yaml").read_text()
    world = yaml.safe_load(compose)["services"]["world"]["environment"]
    assert world["WORLD_SEED"] == ("" if seed is None else "0")
    assert world["WORLD_COST_LIMIT"] == "0.75"
    assert world["WORLD_MAX_TURNS"] == "50" and world["WORLD_RESEARCH_MODE"] == "saved"
    meta = Task(task).config.metadata
    assert (meta["sim_weeks"], meta["data_mode"]) == (3.0, "fixture")
    record_text = (out / "run.json").read_text()
    record = json.loads(record_text)
    assert record["total_trials"] == 6 and record["config"]["seed"] == seed
    assert record["world_cost_limit_usd_per_trial"] == record["config"]["world_cost_limit"] == 0.75
    assert "limits" not in record
    assert record["seed_policy"] == meta["seed_policy"]
    assert record["task"]["digest"] == f"sha256:{Packager.compute_content_hash(task)[0]}"
    assert record["task"]["rules_sha256"] == meta["rules_sha256"]
    assert record["task"]["source_sha256"] == meta["source_sha256"]
    assert record["task"]["environment_version"] == meta["environment_version"]
    assert record["task"]["environment_fingerprint"] == meta["environment_fingerprint"]
    assert record["task"]["agent_wheel_filename"] == meta["agent_wheel_filename"] == approved_wheel[0].name
    assert record["task"]["agent_wheel_sha256"] == meta["agent_wheel_sha256"] == approved_wheel[1]
    assert record["job"]["sha256"] == hashlib.sha256(job_bytes).hexdigest()
    assert all("test-key-never-packaged" not in artifact for artifact in (record_text, job_bytes.decode(), compose))
    assert not (out / "jobs").exists()
    with pytest.raises(FileExistsError, match="empty output directory"):
        batch.prepare(out, batch.NativeBatchConfig.model_validate_json(config.read_bytes()))


def test_native_suite_prepares_ten_distinct_tasks(tmp_path, approved_suite_wheel):
    suite = json.loads((adapter.ROOT / "docs/harbor-long-horizon-suite.json").read_text())["suite"]
    config = _native_suite_config(suite, approved_suite_wheel)
    baseline = next(entry for entry in suite if entry["task_id"] == "rail_original_baseline")
    assert baseline["task_text"] == task_message(load_scenario("train_refund_late"), 12)

    out = tmp_path / "prepared-suite"
    record_path = batch.prepare(out, config)
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_bytes()))
    raw_job = yaml.safe_load((out / "job.yaml").read_bytes())
    assert raw_job["agents"][0]["kwargs"]["forward_task_instruction"] is True
    expected = {entry["task_id"]: entry for entry in suite}
    assert len(job.tasks) == len(expected) == 10
    assert len({task.path for task in job.tasks}) == 10
    for job_task in job.tasks:
        metadata = Task(job_task.path).config.metadata
        entry = expected[metadata["task_id"]]
        assert metadata["scenario"] == entry["scenario"]
        task_env = tomllib.loads((job_task.path / "task.toml").read_text())["environment"]["env"]
        assert task_env["WORLD_TASK_ID"] == metadata["task_id"]
        assert (job_task.path / "instruction.md").read_text() == entry["task_text"] + "\n"
    dataset = tomllib.loads((out / "dataset.toml").read_text())
    assert len(dataset["tasks"]) == 10
    record = json.loads(record_path.read_text())
    assert record["total_trials"] == 10
    assert record["effective_parameters"]["task_count"] == 10
    assert record["effective_parameters"]["attempts_per_task"] == 1
    assert {task["task_id"] for task in record["tasks"]} == set(expected)
    assert {task["scenario"] for task in record["tasks"]} == {"train_refund_late", "bills_price_rise"}


@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("path", "task_id"),
        ("duplicate_id", "unique"),
        ("duplicate_text", "unique"),
        ("blank", "blank"),
        ("unknown_scenario", "(?i)unknown.*scenario|scenario.*unknown"),
    ],
)
def test_native_suite_rejects_invalid_entries(case, error, approved_wheel):
    suite = json.loads((adapter.ROOT / "docs/harbor-long-horizon-suite.json").read_text())["suite"]
    if case == "path":
        suite[0]["task_id"] = "../outside"
    elif case == "duplicate_id":
        suite[1]["task_id"] = suite[0]["task_id"]
    elif case == "duplicate_text":
        suite[1]["task_text"] = suite[0]["task_text"]
    elif case == "blank":
        suite[0]["task_text"] = " \n "
    else:
        suite[0]["scenario"] = "unknown_scenario"

    with pytest.raises(ValueError, match=error):
        _native_suite_config(suite, approved_wheel)


def test_native_suite_rejects_live_research_for_late_refund_entry(approved_wheel):
    suite = json.loads((adapter.ROOT / "docs/harbor-long-horizon-suite.json").read_text())["suite"]
    config = _native_suite_config(suite, approved_wheel)
    config_data = config.model_dump(mode="python", exclude_none=True)
    config_data["scenario"] = "bills_price_rise"
    config_data["research_mode"] = "live"

    with pytest.raises(ValueError, match="train_refund_late.*research_mode"):
        batch.NativeBatchConfig.model_validate(config_data)


def test_batch_rejects_untrackable_inputs_before_writing(tmp_path, approved_wheel):
    config = {"scenario": "bills_price_rise", "execution": "docker", **_wheel_config(approved_wheel)}
    with pytest.raises(ValueError, match="world_cost_limit"):
        batch.NativeBatchConfig(**config)
    assert batch.NativeBatchConfig(**config, world_cost_limit=1).time_limit == 36000
    with pytest.raises(ValueError, match="concurrency cannot exceed"):
        batch.prepare(tmp_path / "private", batch.NativeBatchConfig(**config, world_cost_limit=1, concurrency=2))
    assert not (tmp_path / "private").exists()
    with pytest.raises(ValueError, match="Git-ignored"):
        batch.prepare(adapter.ROOT / "trackable-native-batch",
                      batch.NativeBatchConfig(**config, world_cost_limit=1))
    locked = batch.NativeBatchConfig(**config, world_cost_limit=1, identity_lock="unsupported")
    with pytest.raises(ValueError, match="Native Harbor does not support exact browser artifact locks"):
        batch.prepare(tmp_path / "locked-native", locked)
    assert not (tmp_path / "locked-native").exists()


def test_split_batch_rejects_bills_scenario_before_preparation(approved_split_budget_wheel):
    wheel, digest = approved_split_budget_wheel
    with pytest.raises(ValueError, match="split mode supports only train_refund_late"):
        batch.NativeBatchConfig(
            scenario="bills_price_rise", execution="modal", data_mode="fixture", research_mode="saved",
            model="openrouter/target", simulator_model="openrouter/simulator", grader_model="openrouter/grader",
            cost_limit=50, world_cost_limit=100, trials=1, concurrency=1, time_limit=120,
            openrouter_key_mode="split", agent_max_output_tokens=2048,
            agent_wheel=wheel, agent_wheel_sha256=digest,
        )


def test_batch_rejects_wrong_wheel_digest_before_staging(tmp_path, monkeypatch, approved_wheel):
    path, digest = approved_wheel
    config = batch.NativeBatchConfig(
        scenario="train_refund_late", execution="docker", cost_limit=1, world_cost_limit=1,
        agent_wheel=path, agent_wheel_sha256=("0" if digest[0] != "0" else "1") + digest[1:],
    )
    monkeypatch.setattr(adapter, "stage_agent", lambda *args, **kwargs: pytest.fail("agent was staged"))
    out = tmp_path / "wrong-wheel"
    with pytest.raises(ValueError, match="SHA256 does not match"):
        batch.prepare(out, config)
    assert not out.exists()


def test_split_batch_prints_guarded_capture_verify_and_launch_recipe(
    tmp_path, capsys, approved_split_budget_wheel
):
    import shlex

    wheel, digest = approved_split_budget_wheel
    config_path = tmp_path / "split-config.json"
    out = tmp_path / "split-batch"
    config_path.write_text(json.dumps({
        "scenario": "train_refund_late", "execution": "modal", "data_mode": "fixture",
        "research_mode": "saved", "model": "openrouter/target", "simulator_model": "openrouter/sim",
        "grader_model": "openrouter/grade", "cost_limit": 50, "world_cost_limit": 100,
        "trials": 1, "concurrency": 1, "time_limit": 120,
        "openrouter_key_mode": "split", "agent_max_output_tokens": 2048,
        "agent_wheel": str(wheel), "agent_wheel_sha256": digest,
    }))

    assert batch.main(["--config", str(config_path), "--out", str(out)]) == 0

    manifest = out / "laura-setup.json"
    commands = (
        ["scripts/harbor", "python", "-m", "scripts.laura_trial", "save", "--config",
         str(out / "job.yaml"), "--manifest", str(manifest)],
        ["scripts/harbor", "python", "-m", "scripts.laura_trial", "verify", "--manifest", str(manifest)],
        ["scripts/harbor", "python", "-m", "scripts.laura_trial", "run", "--manifest", str(manifest),
         "--job-name", "laura-native-split-batch"],
    )
    output = capsys.readouterr().out
    assert all(shlex.join(command) in output for command in commands)
    assert "OPENROUTER_AGENT_API_KEY" in output and "OPENROUTER_WORLD_API_KEY" in output
    assert "scripts/harbor harbor run" not in output


def test_split_native_batch_routes_agent_and_world_to_distinct_key_aliases(
    tmp_path, monkeypatch, approved_split_budget_wheel
):
    wheel, digest = approved_split_budget_wheel
    monkeypatch.setenv("OPENROUTER_AGENT_API_KEY", "agent-fake-never-packaged")
    monkeypatch.setenv("OPENROUTER_WORLD_API_KEY", "world-fake-never-packaged")
    out = tmp_path / "split-batch"
    config = batch.NativeBatchConfig(
        scenario="train_refund_late", execution="modal", data_mode="fixture", research_mode="saved",
        model="openrouter/target", simulator_model="openrouter/simulator", grader_model="openrouter/grader",
        cost_limit=50, world_cost_limit=100, trials=1, concurrency=1, time_limit=120,
        openrouter_key_mode="split", agent_max_output_tokens=2048,
        agent_wheel=wheel, agent_wheel_sha256=digest,
    )

    record_path = batch.prepare(out, config)
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_bytes()))
    [agent] = job.agents
    compose = yaml.safe_load((out / "train_refund_late" / "environment" / "docker-compose.yaml").read_text())
    world_env = compose["services"]["world"]["environment"]
    record = json.loads(record_path.read_text())

    assert agent.import_path == adapter.AGENT_IMPORT_PATH
    assert agent.env == {
        "OPENROUTER_API_KEY": "${OPENROUTER_AGENT_API_KEY}",
        "REFUND_EVAL_AGENT_DOLLAR_CAP": "50.0",
        "REFUND_EVAL_AGENT_MAX_OUTPUT_TOKENS": "2048",
    }
    assert "OPENROUTER_WORLD_API_KEY" not in agent.env
    assert world_env["OPENROUTER_API_KEY"] == "${OPENROUTER_WORLD_API_KEY}"
    assert world_env["WORLD_STRICT_BUDGET"] == "true"
    assert "OPENROUTER_AGENT_API_KEY" not in world_env["OPENROUTER_API_KEY"]
    assert record["effective_parameters"]["openrouter_key_mode"] == "split"
    assert record["effective_parameters"]["agent_max_output_tokens"] == 2048
    assert record["effective_parameters"]["agent_key_ceiling_usd"] == 50
    assert record["effective_parameters"]["world_key_ceiling_usd"] == 100
    assert "agent-fake-never-packaged" not in (out / "job.yaml").read_text()
    assert "world-fake-never-packaged" not in compose["services"]["world"]["environment"]["OPENROUTER_API_KEY"]

    from harbor.utils.env import resolve_env_vars

    from refund_eval.realism.track import SOURCE_MANIFEST, freeze_source_manifest
    from scripts.guarded_harbor import (
        LauncherFailure,
        _has_role_key_reference,
        _split_compose_values,
        run_guarded_job,
        validate_job_config,
    )
    from scripts.offline_spend_guard import SplitThresholds

    task_path = out / "train_refund_late"
    assert resolve_env_vars({"OPENROUTER_API_KEY": agent.env["OPENROUTER_API_KEY"]}) == {
        "OPENROUTER_API_KEY": "agent-fake-never-packaged"
    }
    assert resolve_env_vars({"OPENROUTER_API_KEY": world_env["OPENROUTER_API_KEY"]}) == {
        "OPENROUTER_API_KEY": "world-fake-never-packaged"
    }
    assert _has_role_key_reference({"OTHER": "$OPENROUTER_WORLD_API_KEY"})
    assert _has_role_key_reference({"OTHER": "${OPENROUTER_AGENT_API_KEY:-fallback}"})
    assert validate_job_config(job) is None

    compose_path = task_path / "environment" / "docker-compose.yaml"
    original_compose = compose_path.read_bytes()
    list_compose = yaml.safe_load(original_compose)
    world_values = list_compose["services"]["world"]["environment"]
    list_compose["services"]["world"]["environment"] = [
        f"{name}={value}" for name, value in world_values.items()
    ]
    compose_path.write_text(yaml.safe_dump(list_compose, sort_keys=False), encoding="utf-8")
    parsed_values = _split_compose_values(task_path)
    assert parsed_values["WORLD_STRICT_BUDGET"] == "true"
    assert Decimal(parsed_values["WORLD_COST_LIMIT"]) == Decimal(100)
    assert parsed_values["WORLD_SIMULATOR_MODEL"] == "openrouter/simulator"
    assert parsed_values["WORLD_GRADER_MODEL"] == "openrouter/grader"
    assert parsed_values["WORLD_SIM_WEEKS"] == "${WORLD_SIM_WEEKS}"
    assert int(parsed_values["WORLD_MAX_TURNS"]) == 1000

    malformed = yaml.safe_load(original_compose)
    malformed["services"]["world"]["environment"] = ["OPENROUTER_API_KEY"]
    compose_path.write_text(yaml.safe_dump(malformed, sort_keys=False), encoding="utf-8")
    with pytest.raises(LauncherFailure, match="Compose environment is invalid"):
        _split_compose_values(task_path)

    for override, value in (
        ("image", "untrusted/image"),
        ("command", ["/bin/true"]),
        ("entrypoint", ["/bin/true"]),
        ("env_file", ["untrusted.env"]),
        ("extends", {"service": "main"}),
        ("secrets", ["runtime-key"]),
    ):
        modified = yaml.safe_load(original_compose)
        modified["services"]["world"][override] = value
        compose_path.write_text(yaml.safe_dump(modified, sort_keys=False), encoding="utf-8")
        with pytest.raises(LauncherFailure, match="Compose environment is invalid"):
            _split_compose_values(task_path)
    modified = yaml.safe_load(original_compose)
    modified["services"]["other"] = {}
    compose_path.write_text(yaml.safe_dump(modified, sort_keys=False), encoding="utf-8")
    with pytest.raises(LauncherFailure, match="Compose environment is invalid"):
        _split_compose_values(task_path)
    modified = yaml.safe_load(original_compose)
    modified["services"]["main"]["environment"] = {"EXTRA_KEY": "$OPENROUTER_WORLD_API_KEY"}
    compose_path.write_text(yaml.safe_dump(modified, sort_keys=False), encoding="utf-8")
    with pytest.raises(LauncherFailure, match="Compose environment is invalid"):
        _split_compose_values(task_path)
    modified = yaml.safe_load(original_compose)
    modified["services"]["world"]["environment"]["EXTRA_KEY"] = "${OPENROUTER_AGENT_API_KEY:-fallback}"
    compose_path.write_text(yaml.safe_dump(modified, sort_keys=False), encoding="utf-8")
    with pytest.raises(LauncherFailure, match="Compose environment is invalid"):
        _split_compose_values(task_path)
    modified = yaml.safe_load(original_compose)
    modified["secrets"] = {"runtime-key": {"file": "secret.txt"}}
    compose_path.write_text(yaml.safe_dump(modified, sort_keys=False), encoding="utf-8")
    with pytest.raises(LauncherFailure, match="Compose environment is invalid"):
        _split_compose_values(task_path)
    compose_path.write_bytes(original_compose)

    for mutate in (
        lambda raw: raw["environment"].update(extra_docker_compose=["overlay.yaml"]),
        lambda raw: raw["environment"].update(kwargs={"secrets": ["named-secret"]}),
        lambda raw: raw["agents"][0]["env"].update(EXTRA_KEY="$OPENROUTER_WORLD_API_KEY"),
        lambda raw: raw["environment"].setdefault("env", {}).update(
            EXTRA_KEY="${OPENROUTER_AGENT_API_KEY:-fallback}"
        ),
        lambda raw: raw["verifier"].setdefault("env", {}).update(EXTRA_KEY="$OPENROUTER_WORLD_API_KEY"),
    ):
        raw = job.model_dump(mode="python")
        mutate(raw)
        with pytest.raises(LauncherFailure):
            validate_job_config(JobConfig.model_validate(raw))

    task_toml_path = task_path / "task.toml"
    original_task_toml = task_toml_path.read_text(encoding="utf-8")
    task_toml_path.write_text(
        original_task_toml.replace(
            "[environment.env]\n", '[environment.env]\nEXTRA_KEY = "$OPENROUTER_AGENT_API_KEY"\n', 1
        ),
        encoding="utf-8",
    )
    with pytest.raises(LauncherFailure, match="split task base environment"):
        validate_job_config(job)
    task_toml_path.write_text(original_task_toml, encoding="utf-8")

    world_dockerfile = task_path / "environment" / "world" / "Dockerfile"
    original_world_dockerfile = world_dockerfile.read_bytes()
    world_dockerfile.write_bytes(original_world_dockerfile + b"\n")
    with pytest.raises(LauncherFailure, match="build/runtime paths"):
        validate_job_config(job)
    world_dockerfile.write_bytes(original_world_dockerfile)

    world_source = task_path / "environment" / "source"
    stale_module = world_source / "refund_eval" / "models" / "budget.py"
    stale_module.write_bytes(stale_module.read_bytes() + b"\n# stale world budget module\n")
    freeze_source_manifest(world_source)
    current_task = Task(task_path)
    old_source_sha = current_task.config.metadata["source_sha256"]
    source_manifest = json.loads((world_source / SOURCE_MANIFEST).read_text())
    task_toml = (task_path / "task.toml").read_text()
    task_toml = task_toml.replace(old_source_sha, source_manifest["source_sha256"])
    (task_path / "task.toml").write_text(task_toml)
    record["task"]["source_sha256"] = source_manifest["source_sha256"]
    record["task"]["digest"] = f"sha256:{Packager.compute_content_hash(task_path)[0]}"
    record_path.write_text(json.dumps(record))
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_bytes()))

    class ProbeGuard:
        started = False

        async def start(self):
            self.started = True

    probe_guard = ProbeGuard()

    async def fail_job(_):
        pytest.fail("stale split task reached Harbor job creation")

    with pytest.raises(LauncherFailure, match="split world source does not attest"):
        asyncio.run(run_guarded_job(
            job,
            SplitThresholds(Decimal(50), Decimal(100)),
            guard=probe_guard,
            job_factory=fail_job,
        ))
    assert probe_guard.started is False


def test_split_native_task_prebuilt_image_is_rejected_before_guard_or_job(
    tmp_path, monkeypatch, approved_split_budget_wheel
):
    from contextlib import nullcontext

    from scripts import guarded_harbor
    from scripts.guarded_harbor import LauncherFailure, run_guarded_job
    from scripts.offline_spend_guard import SplitThresholds

    wheel, digest = approved_split_budget_wheel
    out = tmp_path / "split-prebuilt-image"
    config = batch.NativeBatchConfig(
        scenario="train_refund_late", execution="modal", data_mode="fixture", research_mode="saved",
        model="openrouter/target", simulator_model="openrouter/simulator", grader_model="openrouter/grader",
        cost_limit=50, world_cost_limit=100, trials=1, concurrency=1, time_limit=120,
        openrouter_key_mode="split", agent_max_output_tokens=2048,
        agent_wheel=wheel, agent_wheel_sha256=digest,
    )
    batch.prepare(out, config)
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_bytes()))
    task_config = out / "train_refund_late" / "task.toml"
    original = task_config.read_text(encoding="utf-8")
    task_config.write_text(
        original.replace(
            "[environment]\n",
            '[environment]\ndocker_image = "registry.example/prebuilt:latest"\n',
            1,
        ),
        encoding="utf-8",
    )

    class ProbeGuard:
        started = False

        async def start(self):
            self.started = True

        async def ensure_healthy(self):
            return None

        def attach_job_task(self, _task):
            return None

        async def cleanup_bound(self):
            return None

        async def close(self):
            return None

    guard = ProbeGuard()
    job_calls = []

    async def create_job(_config):
        job_calls.append(True)
        raise RuntimeError("prebuilt task reached Harbor job creation")

    monkeypatch.setattr(guarded_harbor, "install_modal_hooks", lambda *_args, **_kwargs: nullcontext())
    try:
        asyncio.run(run_guarded_job(
            job,
            SplitThresholds(Decimal(50), Decimal(100)),
            guard=guard,
            job_factory=create_job,
        ))
    except RuntimeError as error:
        failure = error
    else:
        failure = None

    assert not guard.started and job_calls == [], (
        f"split prebuilt image reached guard or job creation: started={guard.started}, jobs={len(job_calls)}"
    )
    assert isinstance(failure, LauncherFailure) and "prebuilt" in str(failure)


@pytest.mark.parametrize(("change", "expected_error"), [
    ({"agent_max_output_tokens": None}, "split mode requires agent_max_output_tokens"),
    ({"agent_max_output_tokens": True}, "agent_max_output_tokens"),
    ({"execution": "docker"}, "split mode requires Modal execution"),
    ({"trials": 2}, "split mode requires one target attempt and concurrency one"),
    ({"concurrency": 2}, "split mode requires one target attempt and concurrency one"),
    ({"trial_spend_cap_usd": 150, "key_spend_cap_usd": 200},
     "split mode does not support combined planning caps"),
])
def test_split_native_config_rejects_unbounded_or_ambiguous_modes_before_preparation(
    tmp_path, change, expected_error, approved_split_budget_wheel
):
    wheel, digest = approved_split_budget_wheel
    config = {
        "scenario": "train_refund_late", "execution": "modal", "data_mode": "fixture",
        "research_mode": "saved", "model": "openrouter/target", "simulator_model": "openrouter/sim",
        "grader_model": "openrouter/grade", "cost_limit": 50, "world_cost_limit": 100,
        "trials": 1, "concurrency": 1, "openrouter_key_mode": "split", "agent_max_output_tokens": 2048,
        "agent_wheel": wheel, "agent_wheel_sha256": digest,
    } | change

    baseline = batch.NativeBatchConfig(**(config | {key: value for key, value in {
        "agent_max_output_tokens": 2048, "execution": "modal", "trials": 1, "concurrency": 1,
        "trial_spend_cap_usd": None, "key_spend_cap_usd": None,
    }.items()}))
    assert baseline.openrouter_key_mode == "split"
    with pytest.raises((ValueError, TypeError), match=expected_error):
        batch.NativeBatchConfig(**config)
    assert not (tmp_path / "split-invalid").exists()


SPEND_CAPPED = {"execution": "docker", "scenario": "train_refund_late", "models": ["openrouter/a", "openrouter/b"],
                "cost_limit": {"openrouter/a": 1.0, "openrouter/b": 2.0}, "world_cost_limit": 0.5,
                "max_turns": 40, "time_limit": 600, "trials": 2, "concurrency": 2,
                "trial_spend_cap_usd": 2.5, "key_spend_cap_usd": 10}


def test_batch_freezes_spend_caps_and_horizons(tmp_path, approved_wheel):
    config = {**SPEND_CAPPED, **_wheel_config(approved_wheel)}
    record = json.loads(batch.prepare(tmp_path / "run", batch.NativeBatchConfig(**config)).read_text())
    assert record["limits"] == {"max_turns": 40, "time_limit_seconds": 600,
                                "trial_spend_cap_usd": 2.5, "key_spend_cap_usd": 10}
    compose = (tmp_path / "run" / "train_refund_late" / "environment" / "docker-compose.yaml").read_text()
    world = yaml.safe_load(compose)["services"]["world"]["environment"]
    assert world["WORLD_MAX_TURNS"] == "40"


def test_explicit_total_time_limit_is_rejected_and_omitted_behavior_is_unchanged(
    tmp_path, approved_wheel
):
    values = {
        "scenario": "train_refund_late",
        "execution": "modal",
        "data_mode": "fixture",
        "research_mode": "saved",
        "model": "openrouter/a",
        "models": ["openrouter/a"],
        "cost_limit": 1,
        "world_cost_limit": 1,
        "time_limit": 120,
        "total_time_limit_seconds": 30,
        **_wheel_config(approved_wheel),
    }
    out = tmp_path / "explicit-total"
    with pytest.raises(ValueError, match="strict_total_deadline_unsupported_by_modal"):
        batch.prepare(out, batch.NativeBatchConfig(**values))
    assert not out.exists()

    omitted = {**values, "total_time_limit_seconds": None}
    omitted_out = tmp_path / "omitted-total"
    omitted_record = json.loads(
        batch.prepare(omitted_out, batch.NativeBatchConfig(**omitted)).read_text(encoding="utf-8")
    )
    omitted_job = JobConfig.model_validate(yaml.safe_load((omitted_out / "job.yaml").read_bytes()))
    omitted_task = Task(omitted_out / "train_refund_late")
    assert omitted_job.agents[0].kwargs["time_limit"] == 120
    assert omitted_task.config.agent.timeout_sec == 132
    assert omitted_record["effective_parameters"]["worker_time_limit_seconds"] == 120
    assert omitted_record["effective_parameters"]["requested_total_time_limit_seconds"] is None
    assert omitted_record["effective_parameters"]["effective_enforced_total_time_limit_seconds"] is None
    assert omitted_record["effective_parameters"]["total_time_limit_enforcement"] == "not_requested"


def test_batch_spend_caps_fit_exactly_in_decimal_dollars(tmp_path, approved_wheel):
    config = {**SPEND_CAPPED, "models": ["openrouter/a"], "cost_limit": 0.1, "world_cost_limit": 0.2,
              "trials": 3, "concurrency": 1, "trial_spend_cap_usd": 0.3, "key_spend_cap_usd": 0.9,
              **_wheel_config(approved_wheel)}
    batch.prepare(tmp_path / "run", batch.NativeBatchConfig(**config))


@pytest.mark.parametrize(("change", "error"), [
    ({"key_spend_cap_usd": None}, "together"),
    ({"max_turns": None}, "explicit max_turns"),
    ({"time_limit": None}, "explicit time_limit"),
    ({"trial_spend_cap_usd": 2.4}, "exceeds trial_spend_cap_usd for: openrouter/b"),
    ({"key_spend_cap_usd": 9.99}, "4 attempts"),
])
def test_batch_rejects_spend_caps_that_do_not_fit(tmp_path, change, error, approved_wheel):
    config = {key: value for key, value in {
        **SPEND_CAPPED, **_wheel_config(approved_wheel), **change,
    }.items() if value is not None}
    with pytest.raises(ValueError, match=error):
        batch.prepare(tmp_path / "run", batch.NativeBatchConfig(**config))
    assert not (tmp_path / "run").exists()


# --------------------------------------------------------------------------- installed agent


WORLD = [MCPServerConfig(name="world", transport="streamable-http", url="http://world:8000/mcp")]


def make_agent(tmp_path, **kwargs):
    return RefundInstalledAgent(logs_dir=tmp_path / "train_refund__abc" / "agent", model_name=kwargs.pop(
        "model_name", "openrouter/anthropic/claude-opus-5.5"), mcp_servers=WORLD, **kwargs)


def test_inspect_command(tmp_path):
    command = make_agent(tmp_path, cost_limit=150, token_budget=40_000_000, history_tokens=60000, time_limit=36000,
                         reasoning=True).inspect_command()
    run, convert = command.split("; status=$?; ")
    assert run.startswith('mkdir -p /logs/agent/inspect; REFUND_EVAL_KILL_FILE=/app/.context/KILL '
                          'REFUND_EVAL_TARGET_CHARGES_DIR=/logs/agent/target-charge-receipts '
                          '/app/.venv/bin/inspect eval '
                          '"$(/app/.venv/bin/python -c \'import os, refund_eval.task; '
                          'print(os.path.relpath(refund_eval.task.__file__, os.getcwd()))\')" ')
    for part in ("--model openrouter/anthropic/claude-opus-5.5", "--max-retries 0", "-M max_retries=0",
                 "-M strict_tools=false", "-M reasoning_enabled=true",
                 "-T world_url=http://world:8000", '-T scenario="$WORLD_SCENARIO"', '-T sim_weeks="$WORLD_SIM_WEEKS"',
                 '-T published_data="$WORLD_DATA_MODE"', "-T history_tokens=60000", "-T token_budget=40000000",
                 "-T cost_limit=150.0", "-T time_limit=36000", "--log-dir /logs/agent/inspect",
                 "--log-format eval", "--metadata harbor_trial=train_refund__abc", "> /logs/agent/inspect.log 2>&1"):
        assert part in run
    assert convert.startswith("/app/.venv/bin/python -m refund_eval.harbor.atif /logs/agent/inspect "
                              "--out /logs/agent/trajectory.json")
    assert convert.endswith("exit $status")
    assert "-T task_id" not in run and "-T task_text" not in run
    default_with_instruction = make_agent(tmp_path).inspect_command("a Harbor instruction")
    assert "-T task_id" not in default_with_instruction and "-T task_text" not in default_with_instruction
    mock = make_agent(tmp_path, model_name="mockllm/model").inspect_command()
    assert "-M " not in mock and "-T cost_limit" not in mock and "-T token_budget" not in mock
    assert "REFUND_EVAL_TARGET_CHARGES_DIR" not in mock


def test_inspect_command_forwards_task_instruction_safely(tmp_path):
    import shlex

    instruction = "Check Laura's dates; do not expand $WORLD_SCENARIO."
    suite_agent = make_agent(tmp_path, forward_task_instruction=True)
    command = suite_agent.inspect_command(instruction + "\n")
    assert f"-T task_text={shlex.quote(instruction)}" in command
    assert '-T task_id="$WORLD_TASK_ID"' in command
    with pytest.raises(ValueError, match="Suite task forwarding"):
        suite_agent.inspect_command()


def test_options_are_validated(tmp_path):
    with pytest.raises(ValueError):
        make_agent(tmp_path, cost_limit=0)
    with pytest.raises(ValueError):
        make_agent(tmp_path, max_turns=10)  # a world setting, fixed in the task


async def test_native_status_pins_world_identity_and_retains_last_good(tmp_path):
    from refund_eval.harbor.luna_status import NativeStatus
    from refund_eval.world_service.status import world_status

    source = tmp_path / "world-status.json"
    source.write_text(json.dumps(world_status({"world": {"turn": 1}}, "train_refund__abc__env")))

    class StatusEnvironment:
        async def service_download_file(self, remote, destination, *, service):
            assert service == "world" and remote == "/var/log/world/luna-world-status.json"
            shutil.copyfile(source, destination)

    agent = make_agent(tmp_path)
    observer = NativeStatus(agent.logs_dir, agent.model_name)
    assert await observer.safe_observe(StatusEnvironment())
    output = agent.logs_dir.parent / "luna-source.json"
    last = output.read_bytes()
    assert json.loads(last)["turns"] == 1
    source.write_text(json.dumps(world_status({"world": {"turn": 2}}, "different_trial__env")))
    assert not await observer.safe_observe(StatusEnvironment())
    assert output.read_bytes() == last  # no renewed heartbeat for an unavailable/wrong-run source
    assert not (agent.logs_dir / "luna-source.json").exists()


class Environment:
    def __init__(self, on_run=None):
        self.commands, self.on_run, self.default_user = [], on_run, None

    async def exec(self, command, **kwargs):
        self.commands.append((command, kwargs.get("cwd")))
        if "inspect eval" in command and self.on_run is not None:
            await self.on_run(self)
        return SimpleNamespace(return_code=0, stdout="", stderr="")


async def test_run_executes_inspect_in_app_and_forwards_the_kill_switch(tmp_path, monkeypatch):
    kill_file = tmp_path / "KILL"
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(kill_file))

    async def run(environment):
        kill_file.touch()  # the operator stops the batch mid-trial
        while not any("touch /app/.context/KILL" in c for c, _ in environment.commands):
            await asyncio.sleep(0.05)

    environment = Environment(on_run=run)
    agent = make_agent(tmp_path, model_name="mockllm/model")
    await asyncio.wait_for(agent.run("ignored", environment, AgentContext()), timeout=10)
    [(command, cwd)] = [c for c in environment.commands if "inspect eval" in c[0]]
    assert cwd == "/app" and "world_url=http://world:8000" in command
    assert [c for c, _ in environment.commands if "touch " in c] == ["mkdir -p /app/.context && touch /app/.context/KILL"]


async def test_cancel_during_observer_cleanup_is_not_suppressed(tmp_path, monkeypatch):
    from refund_eval.harbor import installed_agent

    started, closing = asyncio.Event(), asyncio.Event()

    class ClosingObserver:
        def __init__(self, *_args):
            pass

        async def watch(self, _environment):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                closing.set()
                await asyncio.Event().wait()

        async def safe_observe(self, _environment):
            pass

    monkeypatch.setattr(installed_agent, "NativeStatus", ClosingObserver)

    async def execute(_environment):
        await started.wait()

    run = asyncio.create_task(make_agent(tmp_path).run("ignored", Environment(on_run=execute), AgentContext()))
    await asyncio.wait_for(closing.wait(), timeout=5)
    run.cancel()  # Harbor cancellation after Inspect exits, during observer teardown
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=5)


def test_installed_task_detects_forwarded_kill_file(tmp_path, monkeypatch):
    from refund_eval import killswitch
    from refund_eval.harbor import installed_agent

    app = tmp_path / "app"
    installed = app / ".venv/lib/site-packages/refund_eval"
    installed.mkdir(parents=True)
    stop = app / ".context/KILL"
    stop.parent.mkdir()
    stop.touch()
    monkeypatch.setattr(installed_agent, "APP", str(app))
    monkeypatch.setattr(killswitch, "PROJECT_ROOT", installed.parent)
    monkeypatch.chdir(installed)
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.delenv("REFUND_EVAL_KILL_FILE", raising=False)
    assert killswitch.kill_reason() is None
    command = make_agent(tmp_path).inspect_command()
    assignment = command.split("; ", 1)[1].split()[0]
    key, value = assignment.split("=", 1)
    monkeypatch.setenv(key, value)
    assert killswitch.kill_reason() == f"kill file {stop}"


async def test_kill_switch_blocks_a_new_trial(tmp_path, monkeypatch):
    monkeypatch.setenv("REFUND_EVAL_KILL", "1")
    environment = Environment()
    with pytest.raises(RuntimeError, match="kill switch"):
        await make_agent(tmp_path).run("ignored", environment, AgentContext())
    assert environment.commands == []


def test_world_url_comes_from_the_tasks_mcp_server(tmp_path):
    agent = RefundInstalledAgent(logs_dir=tmp_path, model_name="mockllm/model", mcp_servers=[
        MCPServerConfig(name="world", transport="streamable-http", url="http://world-b:9100/mcp")])
    assert agent.world_url() == "http://world-b:9100"
    assert RefundInstalledAgent(logs_dir=tmp_path, model_name="mockllm/model").world_url() == "http://world:8000"


def test_scripted_parity_agent_runs_inspect_through_its_script(tmp_path):
    from refund_eval.parity.harbor_agent import ScriptedRefundAgent

    command = ScriptedRefundAgent(logs_dir=tmp_path / "t__abc" / "agent", model_name="mockllm/model",
                                  mcp_servers=WORLD, script="turn_cap", cost_limit=1).inspect_command()
    assert ('/app/.venv/bin/python -m refund_eval.parity.inspect_cli turn_cap eval '
            '"$(/app/.venv/bin/python -c \'import os, refund_eval.task; '
            'print(os.path.relpath(refund_eval.task.__file__, os.getcwd()))\')" ') in command
    assert "/app/.venv/bin/inspect eval" not in command and "-T cost_limit=1.0" in command
    with pytest.raises(ValueError, match="mockllm"):
        ScriptedRefundAgent(logs_dir=tmp_path, model_name="openrouter/a/b", mcp_servers=WORLD,
                            script="turn_cap").inspect_command()


# --------------------------------------------------------------------------- ATIF


def fake_log():
    """An Inspect log's shape, as `atif.convert` reads it, without Inspect."""
    now = datetime(2026, 11, 16, tzinfo=UTC)
    usage = SimpleNamespace(input_tokens=100, input_tokens_cache_read=700, input_tokens_cache_write=None,
                            output_tokens=50, reasoning_tokens=None, total_cost=0.25,
                            model_dump=lambda mode=None: {"input_tokens": 100})
    call = SimpleNamespace(id="call_1", function="search_threads", arguments={"query": "is:unread"})
    reasoning = SimpleNamespace(type="reasoning", reasoning="Check the inbox.", redacted=False)
    text = SimpleNamespace(type="text", text="Checking.")
    messages = [
        SimpleNamespace(role="system", content="You are an assistant."),
        SimpleNamespace(role="user", content="Get me a refund."),
        SimpleNamespace(role="assistant", id="m1", content=[reasoning, text], tool_calls=[call]),
        SimpleNamespace(role="tool", tool_call_id="call_1", content="No new mail.", error=None),
        SimpleNamespace(role="assistant", id="m2", content="Waiting.", tool_calls=None),
    ]

    def model_event(message_id):
        output = SimpleNamespace(choices=[1], message=SimpleNamespace(id=message_id), model="openrouter/x/y",
                                 usage=usage)
        return SimpleNamespace(event="model", output=output, timestamp=now, model="openrouter/x/y")

    clock = SimpleNamespace(event="info", source=atif.SIM_CLOCK_SOURCE,
                            data={"sim_time": "2026-09-25T09:00:00+01:00", "turn": 0, "stop_reason": None})
    sample = SimpleNamespace(messages=messages, events=[clock, model_event("m1"), model_event("m2")],
                             model_usage={"openrouter/x/y": usage}, uuid="sample-1", error=None, limit=None)
    return SimpleNamespace(samples=[sample], status="success", error=None,
                           eval=SimpleNamespace(eval_id="e1", task="impossible_refund", model="openrouter/x/y"))


def test_atif_output_is_valid_for_harbor(tmp_path):
    trajectory = atif.convert(fake_log(), agent_version="1.0.0", log_name="trial.eval")
    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(trajectory))
    validator = TrajectoryValidator()
    assert validator.validate(str(path)), validator.get_errors()
    model = Trajectory.model_validate(trajectory)
    agent_step = model.steps[2]
    assert agent_step.reasoning_content == "Check the inbox." and agent_step.message == "Checking."
    assert agent_step.observation.results[0].source_call_id == "call_1"
    assert agent_step.extra == {"sim_time": "2026-09-25T09:00:00+01:00", "sim_turn": 0}
    assert model.final_metrics.total_cost_usd is None and model.final_metrics.total_prompt_tokens == 800
    assert model.final_metrics.extra["inspect_estimated_cost_usd"] == 0.25
    assert agent_step.metrics.extra["inspect_estimated_cost_usd"] == 0.25

    agent = make_agent(tmp_path)
    agent.logs_dir.mkdir(parents=True)
    shutil.copy(path, agent.logs_dir / "trajectory.json")
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens, context.cost_usd) == (
        800, 700, 50, None)
    assert context.model_usage[agent.model_name].cost_usd is None
    assert context.metadata["inspect_log"] == "trial.eval" and "target model only" in context.metadata["cost_scope"]


def test_openrouter_context_reads_charge_receipts_without_trajectory(tmp_path):
    from refund_eval.harbor.target_charges import SOURCE, TARGET_CHARGES_DIRECTORY

    agent = make_agent(tmp_path)
    directory = agent.logs_dir / TARGET_CHARGES_DIRECTORY
    directory.mkdir(parents=True)
    (directory / "attempt.json").write_text(json.dumps({
        "schema_version": 1, "attempt_id": "attempt", "status": "response",
        "generation_id": "gen-offline-1", "source": SOURCE, "actual_cost_usd": "0.012",
    }))
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.cost_usd == 0.012
    assert context.metadata["target_charges"]["attempt_count"] == 1

    (directory / "interrupted.json").write_text(json.dumps({
        "schema_version": 1, "attempt_id": "interrupted", "status": "pending",
        "generation_id": None, "source": None, "actual_cost_usd": None,
    }))
    agent.populate_context_post_run(context)
    assert context.cost_usd is None
    assert context.metadata["target_charges"]["unknown_attempt_count"] == 1


@pytest.mark.parametrize(("retained_receipts", "second_charge", "expected"), [
    (0, "0.008", 0.02),
    (1, "0.008", 0.02),
    (1, None, None),
])
def test_openrouter_context_preserves_trajectory_summary_after_filtering(
    tmp_path, retained_receipts, second_charge, expected,
):
    from refund_eval.harbor.target_charges import SOURCE, TARGET_CHARGES_DIRECTORY

    agent = make_agent(tmp_path)
    directory = agent.logs_dir / TARGET_CHARGES_DIRECTORY
    directory.mkdir(parents=True)
    paths = []
    for number, charge in enumerate(("0.012", second_charge)):
        path = directory / f"attempt-{number}.json"
        path.write_text(json.dumps({
            "schema_version": 1, "attempt_id": path.stem, "status": "response",
            "generation_id": f"gen-offline-{number}", "source": SOURCE if charge is not None else None,
            "actual_cost_usd": charge,
        }))
        paths.append(path)
    trajectory = atif.convert(fake_log(), receipt_dir=directory)
    (agent.logs_dir / "trajectory.json").write_text(json.dumps(trajectory))
    for path in paths[retained_receipts:]:
        path.unlink()

    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.cost_usd == expected
    assert context.model_usage[agent.model_name].cost_usd == expected
    assert context.metadata["target_charges"] == trajectory["extra"]["target_charges"]


def test_openrouter_context_ignores_unmarked_trajectory_cost(tmp_path):
    agent = make_agent(tmp_path)
    agent.logs_dir.mkdir(parents=True)
    trajectory = atif.convert(fake_log())
    del trajectory["extra"]["target_charges"]
    trajectory["final_metrics"]["total_cost_usd"] = 0.25
    (agent.logs_dir / "trajectory.json").write_text(json.dumps(trajectory))

    context = AgentContext()
    agent.populate_context_post_run(context)
    assert context.cost_usd is None
    assert context.model_usage[agent.model_name].cost_usd is None

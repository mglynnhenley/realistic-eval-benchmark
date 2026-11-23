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
import zipfile
from datetime import UTC, date, datetime
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

from refund_eval.agent.prompt import render_system_prompt, task_message
from refund_eval.agent.schedule import format_long, sim_deadline
from refund_eval.harbor import adapter, atif, batch, world_entrypoint
from refund_eval.harbor.installed_agent import RefundAgentOptions, RefundInstalledAgent
from refund_eval.scenario import schema
from refund_eval.scenario.schema import Org, load_agent_scenario, load_scenario
from refund_eval.sim import company_store


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


@pytest.fixture(scope="module")
def approved_wheel(tmp_path_factory):
    return _write_inert_wheel(tmp_path_factory.mktemp("synthetic_agent_wheel"))


def _wheel_config(approved_wheel):
    path, sha256 = approved_wheel
    return {"agent_wheel": path, "agent_wheel_sha256": sha256}


def _wheel_cli_args(approved_wheel):
    path, sha256 = approved_wheel
    return ["--agent-wheel", str(path), "--agent-wheel-sha256", sha256]


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
    assert set(env) == {"WORLD_SCENARIO", "WORLD_SIM_WEEKS", "WORLD_DATA_MODE", "WORLD_TURN_TOKEN"}
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
    assert agent_manifest == world_manifest
    assert agent_manifest["source_sha256"] == Task(task_dir).config.metadata["source_sha256"]


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
    assert job.retry.max_retries == 0


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


def test_total_time_limit_clamps_outer_agent_and_inner_worker_without_omitted_headroom(
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
    record = json.loads(
        batch.prepare(out, batch.NativeBatchConfig(**values)).read_text(encoding="utf-8")
    )
    job = JobConfig.model_validate(yaml.safe_load((out / "job.yaml").read_bytes()))
    task = Task(out / "train_refund_late")
    assert job.agents[0].kwargs["time_limit"] == 30
    assert task.config.agent.timeout_sec == 30
    assert record["effective_parameters"]["worker_time_limit_seconds"] == 30
    assert record["effective_parameters"]["total_time_limit_seconds"] == 30

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
    assert omitted_record["effective_parameters"]["total_time_limit_seconds"] is None


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
                          '/app/.venv/bin/inspect eval '
                          '"$(/app/.venv/bin/python -c \'import os, refund_eval.task; '
                          'print(os.path.relpath(refund_eval.task.__file__, os.getcwd()))\')" ')
    for part in ("--model openrouter/anthropic/claude-opus-5.5", "-M strict_tools=false", "-M reasoning_enabled=true",
                 "-T world_url=http://world:8000", '-T scenario="$WORLD_SCENARIO"', '-T sim_weeks="$WORLD_SIM_WEEKS"',
                 '-T published_data="$WORLD_DATA_MODE"', "-T history_tokens=60000", "-T token_budget=40000000",
                 "-T cost_limit=150.0", "-T time_limit=36000", "--log-dir /logs/agent/inspect",
                 "--log-format eval", "--metadata harbor_trial=train_refund__abc", "> /logs/agent/inspect.log 2>&1"):
        assert part in run
    assert convert.startswith("/app/.venv/bin/python -m refund_eval.harbor.atif /logs/agent/inspect "
                              "--out /logs/agent/trajectory.json")
    assert convert.endswith("exit $status")
    mock = make_agent(tmp_path, model_name="mockllm/model").inspect_command()
    assert "-M " not in mock and "-T cost_limit" not in mock and "-T token_budget" not in mock


def test_options_are_validated(tmp_path):
    with pytest.raises(ValueError):
        make_agent(tmp_path, cost_limit=0)
    with pytest.raises(ValueError):
        make_agent(tmp_path, max_turns=10)  # a world setting, fixed in the task


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
    assert model.final_metrics.total_cost_usd == 0.25 and model.final_metrics.total_prompt_tokens == 800

    agent = make_agent(tmp_path)
    agent.logs_dir.mkdir(parents=True)
    shutil.copy(path, agent.logs_dir / "trajectory.json")
    context = AgentContext()
    agent.populate_context_post_run(context)
    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens, context.cost_usd) == (
        800, 700, 50, 0.25)
    assert context.metadata["inspect_log"] == "trial.eval" and "target model only" in context.metadata["cost_scope"]

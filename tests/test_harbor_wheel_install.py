"""Offline tests for staging and installing the approved native Harbor agent wheel."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tomllib
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

import pytest

pytest.importorskip("harbor.models.task.task")

from harbor.publisher.packager import Packager

from refund_eval.harbor import adapter, batch, installed_agent

pytestmark = pytest.mark.harbor

PAYLOAD = "approved-wheel-fixture-payload-489f33e1"
SCENARIO_TEXT = f'scenario: train_refund_late\nfixture: "{PAYLOAD}"\n'
DIST_NAME = "refund-eval-wheel-fixture"
DIST_VERSION = "0.0.1"
DIST_INFO = "refund_eval_wheel_fixture-0.0.1.dist-info"


@dataclass(frozen=True)
class WheelFixture:
    path: Path
    contents: bytes
    sha256: str


def _build_wheel(directory: Path, *, task_source: str | None = None) -> WheelFixture:
    directory.mkdir(parents=True)
    wheel = directory / "refund_eval_wheel_fixture-0.0.1-py3-none-any.whl"
    task = '''from importlib.resources import files
import json
import os
from pathlib import Path
from sysconfig import get_paths

PAYLOAD = "approved-wheel-fixture-payload-489f33e1"


def main():
    scenario = os.environ["WORLD_SCENARIO"]
    resource = files("refund_eval").joinpath("scenario", f"{scenario}.yaml")
    print(json.dumps({
        "entry_argument": os.environ["WHEEL_TEST_ENTRY_PATH"],
        "module_file": str(Path(__file__).resolve()),
        "payload": PAYLOAD,
        "purelib": get_paths()["purelib"],
        "scenario_file": str(Path(resource).resolve()),
        "scenario_text": resource.read_text(encoding="utf-8"),
        "world_scenario": scenario,
    }))


if __name__ == "__main__":
    main()
'''
    if task_source is not None:
        task = task_source
    members = {
        "refund_eval/__init__.py": b'"""Inert test-only wheel package."""\n',
        "refund_eval/task.py": task.encode(),
        "refund_eval/scenario/__init__.py": b"",
        "refund_eval/scenario/train_refund_late.yaml": SCENARIO_TEXT.encode(),
        "refund_eval/harbor/__init__.py": b"",
        "refund_eval/harbor/atif.py": b'if __name__ == "__main__":\n    pass\n',
        f"{DIST_INFO}/METADATA": (
            "Metadata-Version: 2.1\n"
            f"Name: {DIST_NAME}\n"
            f"Version: {DIST_VERSION}\n"
            "Summary: inert wheel fixture for offline tests\n"
            "\n"
        ).encode(),
        f"{DIST_INFO}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: refund-eval-offline-test\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
    }
    record_rows = []
    for name, content in sorted(members.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        record_rows.append(f"{name},sha256={digest},{len(content)}")
    record_rows.append(f"{DIST_INFO}/RECORD,,")
    members[f"{DIST_INFO}/RECORD"] = ("\n".join(record_rows) + "\n").encode()
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            archive.writestr(name, content)
    contents = wheel.read_bytes()
    return WheelFixture(wheel, contents, hashlib.sha256(contents).hexdigest())


def _config(wheel: WheelFixture, *, digest: str | None = None) -> batch.NativeBatchConfig:
    return batch.NativeBatchConfig(
        execution="docker",
        scenario="train_refund_late",
        data_mode="fixture",
        research_mode="saved",
        model="openrouter/wheel-fixture",
        cost_limit=1.0,
        world_cost_limit=1.0,
        max_turns=1,
        time_limit=30,
        sim_weeks=0.1,
        trials=1,
        concurrency=1,
        agent_wheel=wheel.path,
        agent_wheel_sha256=wheel.sha256 if digest is None else digest,
    )


def _tree_sha256(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in directory.rglob("*") if path.is_file()):
        relative = path.relative_to(directory).as_posix().encode()
        contents = path.read_bytes()
        for value in (relative, contents):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest()


def _make_offline_venv(root: Path, *, uv_marker: Path | None = None) -> tuple[Path, Path, dict[str, str]]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to execute the generated installer")
    environment = root / "install-venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    bin_dir = environment / "bin"
    python = bin_dir / "python"
    uv_dir = Path(uv).resolve().parent
    if uv_marker is not None:
        wrapper_dir = root / "uv-wrapper"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "uv"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"printf x >> {shlex.quote(str(uv_marker))}\n"
            f"exec {shlex.quote(str(Path(uv).resolve()))} \"$@\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        uv_dir = wrapper_dir
    home = root / "home"
    home.mkdir()
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join((str(bin_dir), str(uv_dir), os.defpath)),
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(root),
        "UV_CACHE_DIR": str(root / "uv-cache"),
        "UV_NO_INDEX": "1",
        "UV_OFFLINE": "1",
    }
    return environment, python, env


def _run_installer(agent_dir: Path, root: Path, *, uv_marker: Path | None = None):
    environment, python, env = _make_offline_venv(root, uv_marker=uv_marker)
    cwd = root / "outside-repository"
    cwd.mkdir()
    result = subprocess.run(
        [str(python), "-I", str(agent_dir / "install-agent.py")],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    return result, environment, python, env


def _installed_report(python: Path, env: dict[str, str], cwd: Path) -> dict:
    probe = '''
import importlib.metadata
import importlib.resources
import json
from pathlib import Path
import refund_eval.task

distribution = importlib.metadata.distribution("refund-eval-wheel-fixture")
resource = importlib.resources.files("refund_eval").joinpath("scenario", "train_refund_late.yaml")
direct_url = distribution.read_text("direct_url.json")
print(json.dumps({
    "direct_url": json.loads(direct_url) if direct_url else None,
    "module_file": str(Path(refund_eval.task.__file__).resolve()),
    "payload": refund_eval.task.PAYLOAD,
    "purelib": str(Path(__import__("sysconfig").get_paths()["purelib"]).resolve()),
    "scenario_file": str(Path(resource).resolve()),
    "scenario_text": resource.read_text(encoding="utf-8"),
}))
'''
    result = subprocess.run(
        [str(python), "-I", "-c", probe],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return json.loads(result.stdout)


def _exercise_generated_command(
    monkeypatch: pytest.MonkeyPatch,
    python: Path,
    env: dict[str, str],
    root: Path,
) -> dict:
    root.mkdir(parents=True)
    bin_dir = python.parent
    inspect = bin_dir / "inspect"
    inspect.write_text(
        "#!/usr/bin/env python\n"
        "import os, runpy, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "target = Path(args[args.index('eval') + 1]).resolve()\n"
        "if not target.is_file():\n"
        "    raise SystemExit(f'installed task entrypoint not found: {target}')\n"
        "os.environ['WHEEL_TEST_ENTRY_PATH'] = str(target)\n"
        "runpy.run_path(str(target), run_name='__main__')\n",
        encoding="utf-8",
    )
    inspect.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    app = root / "app"
    app.mkdir()
    logs = root / "agent-logs"
    monkeypatch.setattr(installed_agent, "APP", str(app))
    monkeypatch.setattr(installed_agent, "PYTHON", str(python))
    monkeypatch.setattr(installed_agent, "INSPECT", str(inspect))
    agent = installed_agent.RefundInstalledAgent(
        logs_dir=root / "trial" / "agent",
        model_name="mockllm/wheel-fixture",
        mcp_servers=[],
        environment_logs_dir=PurePosixPath(logs.as_posix()),
    )
    env = {
        **env,
        "WORLD_SCENARIO": "train_refund_late",
        "WORLD_SIM_WEEKS": "0.1",
        "WORLD_DATA_MODE": "fixture",
    }
    result = subprocess.run(
        agent.inspect_command(),
        shell=True,
        cwd=app,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (app / "refund_eval").exists()
    return json.loads((logs / "inspect.log").read_text(encoding="utf-8"))


def test_prepare_stages_and_installs_approved_wheel_offline(tmp_path, monkeypatch):
    wheel = _build_wheel(tmp_path / "input")
    out = tmp_path / "prepared"
    staged_environment = {}
    identity_seen_before_hash = []
    real_stage_agent = adapter.stage_agent
    real_validate_and_index = batch.validate_and_index

    def stage_spy(source, destination, *args, **kwargs):
        staged_environment["source"] = _tree_sha256(source)
        wheel.path.write_bytes(b"changed after the approved wheel was read")
        result = real_stage_agent(source, destination, *args, **kwargs)
        assert staged_environment["source"] == _tree_sha256(source)
        return result

    def validate_spy(output, task, job):
        metadata = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))["metadata"]
        identity_seen_before_hash.append((metadata["agent_wheel_filename"], metadata["agent_wheel_sha256"]))
        return real_validate_and_index(output, task, job)

    monkeypatch.setattr(adapter, "stage_agent", stage_spy)
    monkeypatch.setattr(batch, "validate_and_index", validate_spy)
    record_path = batch.prepare(out, _config(wheel))

    task = out / "train_refund_late"
    agent_dir = task / "environment" / "agent"
    staged_wheel = agent_dir / "wheels" / wheel.path.name
    manifest = json.loads((agent_dir / "agent-wheel.json").read_text(encoding="utf-8"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert "forward_task_instruction" not in (out / "job.yaml").read_text(encoding="utf-8")
    assert tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))["environment"]["env"]["WORLD_TASK_ID"] == ""
    assert identity_seen_before_hash == [(wheel.path.name, wheel.sha256)]
    assert manifest == {"filename": wheel.path.name, "sha256": wheel.sha256}
    assert staged_wheel.read_bytes() == wheel.contents
    assert not (agent_dir / "refund_eval").exists()
    assert not list(agent_dir.rglob("*.yaml"))
    assert record["task"]["agent_wheel_filename"] == wheel.path.name
    assert record["task"]["agent_wheel_sha256"] == wheel.sha256
    assert record["task"]["digest"] == f"sha256:{Packager.compute_content_hash(task)[0]}"

    environment = task / "environment"
    assert staged_environment["source"] == _tree_sha256(environment / "source")
    world_dockerfile = environment / "world" / "Dockerfile"
    expected_world_dockerfile = adapter.TEMPLATE / "environment" / "world" / "Dockerfile"
    assert world_dockerfile.read_bytes() == expected_world_dockerfile.read_bytes()
    agent_dockerfile = environment / "Dockerfile"
    agent_dockerfile_text = agent_dockerfile.read_text(encoding="utf-8")
    agent_dockerfile_lines = agent_dockerfile_text.splitlines()
    install_line = "RUN /app/.venv/bin/python /app/install-agent.py"
    assert agent_dockerfile_lines.count(install_line) == 1
    install_line_index = agent_dockerfile_lines.index(install_line)
    copy_agent_index = next(i for i, line in enumerate(agent_dockerfile_lines) if line.startswith("COPY agent/ /app"))
    assert copy_agent_index < install_line_index
    assert not any(
        re.search(
            r"\buv\s+sync\b|\buv\s+pip\s+install\b.*(?:--editable|(?:^|\s)-e(?:\s|$))"
            r"|\bpip\s+install\b.*(?:--editable|(?:^|\s)-e(?:\s|$))",
            line,
        )
        for line in agent_dockerfile_lines[install_line_index + 1:]
    )

    installed, venv_path, python, env = _run_installer(agent_dir, tmp_path / "install")
    assert installed.returncode == 0, installed.stdout + installed.stderr
    report = _installed_report(python, env, tmp_path / "install" / "outside-repository")
    assert report["payload"] == PAYLOAD
    assert report["scenario_text"] == SCENARIO_TEXT
    assert Path(report["module_file"]).is_relative_to(Path(report["purelib"]))
    assert Path(report["scenario_file"]).is_relative_to(Path(report["purelib"]))
    direct_url = report["direct_url"]
    assert direct_url is not None
    assert direct_url.get("dir_info", {}).get("editable") is not True
    parsed_url = urlsplit(direct_url["url"])
    assert parsed_url.scheme == "file"
    assert Path(url2pathname(unquote(parsed_url.path))).resolve() == staged_wheel.resolve()

    launcher_report = _exercise_generated_command(monkeypatch, python, env, tmp_path / "launch")
    assert launcher_report["payload"] == PAYLOAD
    assert launcher_report["world_scenario"] == "train_refund_late"
    assert launcher_report["scenario_text"] == SCENARIO_TEXT
    assert launcher_report["entry_argument"] == launcher_report["module_file"]
    assert Path(launcher_report["module_file"]).is_relative_to(Path(launcher_report["purelib"]))
    assert Path(launcher_report["scenario_file"]).is_relative_to(Path(launcher_report["purelib"]))
    assert not Path(launcher_report["module_file"]).is_relative_to((environment / "source").resolve())
    assert staged_environment["source"] == _tree_sha256(environment / "source")
    assert world_dockerfile.read_bytes() == expected_world_dockerfile.read_bytes()
    assert not venv_path.joinpath("refund_eval").exists()


def test_prepare_rejects_wrong_wheel_digest_before_staging_or_output_creation(tmp_path, monkeypatch):
    wheel = _build_wheel(tmp_path / "input")
    out = tmp_path / "must-not-exist"
    stage_calls = []
    real_stage_agent = adapter.stage_agent

    def stage_spy(*args, **kwargs):
        stage_calls.append(True)
        return real_stage_agent(*args, **kwargs)

    monkeypatch.setattr(adapter, "stage_agent", stage_spy)
    wrong_digest = ("0" if wheel.sha256[0] != "0" else "1") + wheel.sha256[1:]
    with pytest.raises(ValueError, match="(?i)(wheel|sha256|digest)"):
        batch.prepare(out, _config(wheel, digest=wrong_digest))
    assert stage_calls == []
    assert not out.exists()


@pytest.mark.parametrize(
    "task_source",
    [
        "def impossible_refund(*, task_text=None):\n    return None\n",
        "def impossible_refund(*, task_id=None):\n    return None\n",
    ],
)
def test_prepare_rejects_suite_wheel_missing_task_override_support(tmp_path, task_source):
    wheel = _build_wheel(tmp_path / "legacy", task_source=task_source)
    config_data = _config(wheel).model_dump(mode="python", exclude_none=True)
    config_data["suite"] = json.loads((adapter.ROOT / "docs/harbor-long-horizon-suite.json").read_text())["suite"]
    config = batch.NativeBatchConfig.model_validate(config_data)
    out = tmp_path / "must-not-exist"

    with pytest.raises(ValueError, match="optional task_text and task_id"):
        batch.prepare(out, config)
    assert not out.exists()


def test_installer_rejects_tampered_staged_wheel_before_uv_install(tmp_path):
    wheel = _build_wheel(tmp_path / "input")
    out = tmp_path / "prepared"
    batch.prepare(out, _config(wheel))
    agent_dir = out / "train_refund_late" / "environment" / "agent"
    staged_wheel = agent_dir / "wheels" / wheel.path.name
    staged_wheel.write_bytes(staged_wheel.read_bytes() + b"tampered after staging")

    uv_marker = tmp_path / "uv-was-invoked"
    result, venv_path, _, _ = _run_installer(agent_dir, tmp_path / "install", uv_marker=uv_marker)
    assert result.returncode != 0
    assert not uv_marker.exists()
    assert not list(venv_path.rglob(DIST_INFO))

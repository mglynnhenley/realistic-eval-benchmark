"""Run actual cleanup dispatch in Harbor's lean Python with an offline Modal stub."""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from refund_eval.realism import autonomous

ROOT = Path(__file__).resolve().parents[1]


def offline_harbor(tmp_path, mode):
    stub = tmp_path / "offline"
    stub.mkdir()
    audit = tmp_path / "sdk-audit.jsonl"
    (stub / "sitecustomize.py").write_text(f'''
import importlib.util
import json
import sys
from pathlib import Path
audit = Path({str(audit)!r})
missing = {{name: importlib.util.find_spec(name) is None for name in ("numpy", "inspect_ai", "inspect_scout")}}
with audit.open("a") as stream:
    stream.write(json.dumps({{"event": "startup", "executable": sys.executable, "missing": missing}}) + "\\n")
class NoHeavyImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in missing:
            with audit.open("a") as stream:
                stream.write(json.dumps({{"event": "forbidden_import", "module": fullname}}) + "\\n")
            raise ImportError("Forbidden heavy cleanup dependency: " + fullname)
sys.meta_path.insert(0, NoHeavyImports())
''')
    (stub / "modal.py").write_text(f'''
import json
from pathlib import Path
AUDIT = Path({str(audit)!r})
MODE = {mode!r}
def record(event, **kwargs):
    with AUDIT.open("a") as stream:
        stream.write(json.dumps({{"event": event, **kwargs}}) + "\\n")
class Sandbox:
    object_id = "synthetic-owned-sandbox"
    def __init__(self, foreign=False): self.foreign = foreign
    @staticmethod
    def list(*, tags):
        record("list", tags=tags)
        assert tags == {{"harbor.managed": "true", "harbor.session_id": "task__own__env"}}
        if MODE == "list_error": raise RuntimeError("synthetic-provider-body-must-stay-private")
        return [Sandbox(), Sandbox(True)] if MODE == "foreign" else [Sandbox()]
    def get_tags(self):
        record("get_tags", foreign=self.foreign)
        return {{"harbor.managed": "true", "harbor.session_id": "task__other__env" if self.foreign else "task__own__env"}}
    def terminate(self):
        assert not self.foreign
        record("terminate", object_id=self.object_id)
    def wait(self, *, raise_on_termination):
        assert raise_on_termination is False
        record("wait", object_id=self.object_id)
        if MODE == "wait_error": raise RuntimeError("synthetic-provider-body-must-stay-private")
''')
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(stub), "UV_PYTHON": "3.14",
           "PYTHON_DOTENV_DISABLED": "1", "MODAL_CONFIG_PATH": "/dev/null"}
    return env, audit


@pytest.mark.parametrize("mode", ["success", "foreign", "list_error", "wait_error"])
@pytest.mark.parametrize("old_candidate", [False, True])
def test_actual_cleanup_uses_lean_harbor_and_trusted_worker(tmp_path, mode, old_candidate):
    env, audit = offline_harbor(tmp_path, mode)
    directory = tmp_path / "run"
    trial = directory / "batches/baseline/trials/jobs/run/task__own"
    trial.mkdir(parents=True)
    (trial / "config.json").write_text(json.dumps({"trial_name": trial.name,
        "trials_dir": str(trial.parent), "environment": {"type": "modal"}}))
    candidate = tmp_path / "old-candidate"
    (candidate / "scripts").mkdir(parents=True)
    trap = candidate / "scripts/harbor"
    trap.write_text("#!/bin/sh\necho 'Old candidate launcher must not execute' >&2\nexit 89\n")
    trap.chmod(0o700)
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        # Actual production command and lean environment; only Modal is offline.
        process = subprocess.run(argv, cwd=candidate if old_candidate else ROOT,
                                 env=env, capture_output=True, text=True, timeout=90, check=False)
        (tmp_path / "cleanup-subprocess.log").write_text(process.stdout + process.stderr)
        return {"returncode": process.returncode}

    result = autonomous.cleanup_job(SimpleNamespace(directory=directory, command=command))
    assert len(calls) == 1
    assert calls[0][:3] == [str(ROOT / "scripts/harbor"), "python",
                            str(ROOT / "refund_eval/realism/modal_cleanup.py")]
    assert result["sessions"] == ["task__own__env"]
    assert result["unresolved"] == ([] if mode == "success" else ["task__own__env"])
    assert result["terminated"] == (["synthetic-owned-sandbox"] if mode in {"success", "foreign"} else [])
    assert "synthetic-provider-body" not in json.dumps(result)
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    assert events[0]["event"] == "startup" and all(events[0]["missing"].values())
    assert not any(event["event"] == "forbidden_import" for event in events)
    assert [event["tags"] for event in events if event["event"] == "list"] == [
        {"harbor.managed": "true", "harbor.session_id": "task__own__env"}]
    assert sum(event["event"] == "terminate" for event in events) == (mode != "list_error")
    assert json.loads((directory / "modal-cleanup.json").read_text()) == result


@pytest.mark.parametrize("payload", [None, {"sessions": None}, {"sessions": "task__own__env"},
                                    {"sessions": [None]}, {"sessions": ["../other__env"]},
                                    {"sessions": ["task__own__env", "task__own__env"]}])
def test_standalone_rejects_invalid_targets_before_modal(tmp_path, payload):
    env, audit = offline_harbor(tmp_path, "success")
    targets, output = tmp_path / "targets.json", tmp_path / "receipt.json"
    targets.write_text(json.dumps(payload))
    process = subprocess.run([str(ROOT / "scripts/harbor"), "python",
                              str(ROOT / "refund_eval/realism/modal_cleanup.py"),
                              "--cleanup-modal", str(targets), str(output)],
                             cwd=tmp_path, env=env, capture_output=True, text=True, timeout=90, check=False)
    assert process.returncode != 0 and "Invalid job sandbox identity" in process.stderr
    assert not output.exists()
    events = [json.loads(line) for line in audit.read_text().splitlines()]
    assert [event["event"] for event in events] == ["startup"]
    assert all(events[0]["missing"].values())

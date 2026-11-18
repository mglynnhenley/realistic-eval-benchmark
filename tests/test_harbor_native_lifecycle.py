"""Opt-in native Harbor lifecycle checks on local Docker, with no model credentials."""

import json
import os
import signal
import subprocess
import time

import pytest
import yaml

from refund_eval.parity.run import AGENT_IMPORT, HARBOR, free_env

pytestmark = [
    pytest.mark.harbor,
    pytest.mark.skipif(os.environ.get("REFUND_EVAL_NATIVE_DOCKER_SMOKE") != "1",
                       reason="opt in to the free native Docker smoke test"),
]


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False, timeout=15)


@pytest.mark.parametrize("stop", ["timeout", "kill"])
def test_native_docker_recovers_artifacts_and_deletes_trial_resources(tmp_path, stop):
    tasks = tmp_path / "tasks"
    kill_file = tmp_path / "KILL"
    env = free_env(REFUND_EVAL_FAKE_WORLD_MODELS="1", REFUND_EVAL_KILL_FILE=str(kill_file))
    env.pop("VIRTUAL_ENV", None)
    subprocess.run([str(HARBOR), "python", "-m", "refund_eval.harbor.adapter", "--out", str(tasks),
                    "--scenario", "train_refund_late", "--data-mode", "fixture", "--research-mode", "saved",
                    "--world-cost-limit", "1", "--max-turns", "1000", "--sim-weeks", "12",
                    "--agent-timeout", "15" if stop == "timeout" else "120"], env=env, check=True,
                   capture_output=True, text=True, timeout=120)
    scripts = tasks / "train_refund_late" / "environment" / "agent" / "refund_eval" / "parity" / "scripts.py"
    text = scripts.read_text()
    start = "class TurnCap(Script):\n    def step(self, n, messages):\n"
    assert text.count(start) == 1
    scripts.write_text(text.replace(start, start + "        if n == 2:\n            import time\n            time.sleep(25)\n"))
    job_path = tasks / "job.yaml"
    job = yaml.safe_load(job_path.read_text())
    job["n_attempts"] = job["n_concurrent_trials"] = 1
    job["agents"] = [{"import_path": AGENT_IMPORT, "model_name": "mockllm/model",
                      "kwargs": {"script": "turn_cap", "cost_limit": 1.0}}]
    job_path.write_text(yaml.safe_dump(job))
    job_dir = tasks / "jobs" / job["job_name"]
    log_path = tmp_path / "harbor.log"
    with log_path.open("w") as log, subprocess.Popen(
        [str(HARBOR), "harbor", "run", "-y", "-c", str(job_path)], env=env, stdout=log, stderr=subprocess.STDOUT,
    ) as process:
        try:
            if stop == "kill":
                deadline = time.monotonic() + 160
                while process.poll() is None and time.monotonic() < deadline:
                    trial = next(job_dir.glob("train_refund_late__*"), None)
                    if trial is not None:
                        sidecar = f"{trial.name.lower()}__env-world-1"
                        turn = docker("exec", sidecar, "grep", "-qE", r'"kind": *"turn"', "/var/log/world/events.jsonl")
                        if turn.returncode == 0:
                            kill_file.touch()
                            break
                    time.sleep(0.25)
                assert kill_file.exists(), log_path.read_text()
            process.wait(timeout=240)
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=60)

    [trial] = job_dir.glob("train_refund_late__*")
    result = json.loads((trial / "result.json").read_text())
    world = trial / "artifacts" / "world"
    state = json.loads((world / "state.json").read_text())
    assert (world / "events.jsonl").stat().st_size > 0
    assert (world / "final_snapshot.json").is_file()
    assert (trial / "agent" / "inspect.log").is_file()
    assert (trial / "verifier" / "reward.json").is_file()
    assert state["store"]["world_model_budget"]["spent_usd"] == 0
    if stop == "timeout":
        assert result["exception_info"]["exception_type"] == "AgentTimeoutError"
    else:
        assert state["stop_reason"] == "kill_switch"

    for project in (f"{trial.name.lower()}__env", f"{trial.name.lower()}__verifier__trial"):
        label = f"label=com.docker.compose.project={project}"
        for command in (("ps", "-aq"), ("volume", "ls", "-q"), ("network", "ls", "-q")):
            listed = docker(*command, "--filter", label)
            assert listed.returncode == 0 and not listed.stdout.strip(), (command, listed.stdout, listed.stderr)

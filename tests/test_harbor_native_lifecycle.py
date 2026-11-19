"""Opt-in native Harbor lifecycle checks on local Docker, with no model credentials."""

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from refund_eval.parity.run import AGENT_IMPORT, HARBOR, free_env, run_harbor
from refund_eval.parity.scripts import SCRIPTS
from refund_eval.world.state import World

pytestmark = [
    pytest.mark.harbor,
    pytest.mark.skipif(os.environ.get("REFUND_EVAL_NATIVE_DOCKER_SMOKE") != "1",
                       reason="opt in to the free native Docker smoke test"),
]


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=False, timeout=15)


def approved_wheel_args():
    wheel = os.environ.get("REFUND_EVAL_AGENT_WHEEL")
    digest = os.environ.get("REFUND_EVAL_AGENT_WHEEL_SHA256")
    if not wheel or not digest:
        pytest.skip("set approved-wheel environment variables to run the native Docker smoke test")
    return ["--agent-wheel", wheel, "--agent-wheel-sha256", digest]


def inject_installed_parity_scripts(environment: Path, *, needle: str | None = None,
                                    replacement: str | None = None, append: str = "") -> None:
    lines = [
        "from pathlib import Path",
        "import refund_eval.parity.scripts",
        "scripts_path = Path(refund_eval.parity.scripts.__file__)",
        "source = scripts_path.read_text()",
    ]
    if needle is not None:
        lines.extend([
            f"needle = {needle!r}",
            "assert source.count(needle) == 1",
            f"source = source.replace(needle, {replacement!r})",
        ])
    if append:
        lines.append(f"source += {append!r}")
    lines.append("scripts_path.write_text(source)")

    test_hook = environment / "agent" / "lifecycle-test-inject.py"
    test_hook.write_text("\n".join(lines) + "\n")
    dockerfile = environment / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text() + "\nRUN /app/.venv/bin/python /app/lifecycle-test-inject.py\n")


def test_native_docker_refund_from_fixture_web_to_verifier(tmp_path):
    script = SCRIPTS["late_form"]
    tasks = tmp_path / "tasks"
    subprocess.run([str(HARBOR), "python", "-m", "refund_eval.harbor.adapter", "--out", str(tasks),
                    "--scenario", script.scenario, "--data-mode", "fixture", "--research-mode", "saved",
                    "--max-turns", str(script.max_turns), "--sim-weeks", str(script.sim_weeks),
                    *approved_wheel_args()],
                   env=free_env(), check=True, capture_output=True, text=True, timeout=120)
    environment = tasks / script.scenario / "environment"
    inject_installed_parity_scripts(environment, append='''

class SearchAndRefund(LateForm):
    def step(self, n, messages):
        if n == 0:
            return call(1000, "web_search_exa", {"query": "Trainline ticket refund rules",
                                                 "objective": "Find public refund policy"})
        return super().step(n - 1, messages)


SCRIPTS["late_form"] = SearchAndRefund("late_form", "train_refund_late", "Search and submit",
                                     max_turns=16, cost_limit=1.0)
''')
    world_entrypoint = environment / "source" / "refund_eval" / "harbor" / "world_entrypoint.py"
    source = world_entrypoint.read_text()
    start = 'if __name__ == "__main__":'
    assert source.count(start) == 1
    world_entrypoint.write_text(source.replace(start, '''FAKE_ACTION.update(
    decision="approved",
    decision_basis="The scripted approval fixture accepts the claim.",
    body="We approved a £350 refund to your original card.",
    grant_ground="unused_ticket_refund_pre_april_2026",
    payments=[{"kind": "cash_refund", "amount": 350, "via": "original card"}],
    case_status="resolved",
)


''' + start))

    result = run_harbor(script, tmp_path, seed=7)
    trial = Path(result["trial_dir"])
    state = json.loads((trial / "artifacts/world/state.json").read_text())
    world = World.model_validate(state["store"]["world"])
    audit = state["store"]["published_tool_audit"]
    assert any(e.get("event") == "search_boundary" and e.get("backend") == "fixture"
               and e.get("phase") == "result" for e in audit)
    assert any(e.get("event") == "browser_decision" and e.get("action") == "fixture" for e in audit)
    [submission] = world.interceptions
    assert (submission.channel, submission.org) == ("form", "trainline")
    [transfer] = world.outcome.transfers
    assert (transfer.kind, transfer.amount, transfer.currency) == ("cash_refund", 350, "GBP")
    assert world.outcome.cash_refunded(as_of=world.now) == 350
    assert result["stop_reason"] == "refund_reached"
    assert list((trial / "artifacts/agent-eval").glob("*.eval"))
    assert (trial / "artifacts/world/events.jsonl").stat().st_size > 0
    reward = json.loads((trial / "verifier/reward.json").read_text())
    assert reward["task_success"] == reward["event_log_matches_state"] == 1.0


@pytest.mark.parametrize("stop", ["timeout", "kill", "cancel", "cancel_collect_failure"])
def test_native_docker_recovers_artifacts_and_deletes_trial_resources(tmp_path, stop):
    tasks = tmp_path / "tasks"
    kill_file = tmp_path / "KILL"
    env = free_env(REFUND_EVAL_FAKE_WORLD_MODELS="1", REFUND_EVAL_KILL_FILE=str(kill_file))
    env.pop("VIRTUAL_ENV", None)
    subprocess.run([str(HARBOR), "python", "-m", "refund_eval.harbor.adapter", "--out", str(tasks),
                    "--scenario", "train_refund_late", "--data-mode", "fixture", "--research-mode", "saved",
                    "--world-cost-limit", "1", "--max-turns", "1000", "--sim-weeks", "12",
                    "--agent-timeout", "15" if stop == "timeout" else "120",
                    *approved_wheel_args()], env=env, check=True,
                   capture_output=True, text=True, timeout=120)
    environment = tasks / "train_refund_late" / "environment"
    start = "class TurnCap(Script):\n    def step(self, n, messages):\n"
    if stop in {"cancel", "cancel_collect_failure"}:
        injected = ("        if n == 1:\n"
                    "            from pathlib import Path\n"
                    "            import time\n"
                    "            marker = Path('/logs/agent/inspect/model-active')\n"
                    "            marker.touch()\n"
                    "            time.sleep(25)\n"
                    "            marker.with_name('model-completed').touch()\n")
    else:
        injected = "        if n == 2:\n            import time\n            time.sleep(25)\n"
    inject_installed_parity_scripts(environment, needle=start, replacement=start + injected)
    job_path = tasks / "job.yaml"
    if stop in {"cancel", "cancel_collect_failure"}:
        task_path = tasks / "train_refund_late" / "task.toml"
        task_text = task_path.read_text()
        agent_artifact = '    { source = "/logs/agent/inspect", destination = "agent-eval" },\n'
        assert task_text.count(agent_artifact) == 1
        task_text = task_text.replace(
            agent_artifact,
            agent_artifact
            + '    { source = "/missing/cancel-main", destination = "cancel-main" },\n'
            + '    { source = "/missing/cancel-world", service = "world", destination = "cancel-world" },\n',
        )
        if stop == "cancel_collect_failure":
            collect_start = task_text.index("[[verifier.collect]]")
            command_start = task_text.index('command = "', collect_start)
            command_end = task_text.index("\n", command_start)
            task_text = task_text[:command_start] + 'command = "exit 42"' + task_text[command_end:]
        task_path.write_text(task_text)
    job = yaml.safe_load(job_path.read_text())
    job["n_attempts"] = job["n_concurrent_trials"] = 1
    job["agents"] = [{"import_path": AGENT_IMPORT, "model_name": "mockllm/model",
                      "kwargs": {"script": "turn_cap", "cost_limit": 1.0}}]
    job_path.write_text(yaml.safe_dump(job))
    job_dir = tasks / "jobs" / job["job_name"]
    log_path = tmp_path / "harbor.log"
    cancellation_sent = False
    with log_path.open("w") as log, subprocess.Popen(
        [str(HARBOR), "harbor", "run", "-y", "-c", str(job_path)], env=env, stdout=log, stderr=subprocess.STDOUT,
    ) as process:
        try:
            if stop in {"kill", "cancel", "cancel_collect_failure"}:
                deadline = time.monotonic() + 160
                while process.poll() is None and time.monotonic() < deadline:
                    trial = next(job_dir.glob("train_refund_late__*"), None)
                    if trial is not None:
                        if stop == "kill":
                            sidecar = f"{trial.name.lower()}__env-world-1"
                            turn = docker("exec", sidecar, "grep", "-qE", r'"kind": *"turn"', "/var/log/world/events.jsonl")
                            if turn.returncode == 0:
                                kill_file.touch()
                                break
                        else:
                            main = f"{trial.name.lower()}__env-main-1"
                            active = docker("exec", main, "test", "-f", "/logs/agent/inspect/model-active")
                            if active.returncode == 0:
                                process.send_signal(signal.SIGINT)
                                cancellation_sent = True
                                break
                    time.sleep(0.25)
                if stop == "kill":
                    assert kill_file.exists(), log_path.read_text()
                else:
                    assert cancellation_sent, log_path.read_text()
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
    assert (trial / "agent" / "inspect.log").is_file()
    assert state["store"]["world_model_budget"]["spent_usd"] == 0
    if stop in {"cancel", "cancel_collect_failure"}:
        assert result["exception_info"]["exception_type"] == "CancelledError"
        assert not (trial / "verifier" / "reward.json").exists()
        agent_eval = trial / "artifacts" / "agent-eval"
        assert (agent_eval / "model-active").is_file()
        assert not (agent_eval / "model-completed").exists()
        if stop == "cancel_collect_failure":
            assert not (world / "final_snapshot.json").exists()
            assert "Collect hook in service 'world' exited with code 42" in (
                trial / "trial.log"
            ).read_text()
        else:
            assert (world / "final_snapshot.json").is_file()
        entries = {entry["source"]: entry for entry in json.loads(
            (trial / "artifacts" / "manifest.json").read_text()
        )}
        assert entries["/var/log/world"]["status"] == "ok"
        assert entries["/logs/agent/inspect"]["status"] == "ok"
        assert entries["/missing/cancel-main"]["status"] == "failed"
        assert entries["/missing/cancel-world"]["status"] == "failed"
        assert entries["/missing/cancel-main"]["service"] is None
        assert entries["/missing/cancel-world"]["service"] == "world"
    else:
        assert (world / "final_snapshot.json").is_file()
        assert (trial / "verifier" / "reward.json").is_file()
    if stop == "timeout":
        assert result["exception_info"]["exception_type"] == "AgentTimeoutError"
    elif stop == "kill":
        assert state["stop_reason"] == "kill_switch"

    for project in (f"{trial.name.lower()}__env", f"{trial.name.lower()}__verifier__trial"):
        label = f"label=com.docker.compose.project={project}"
        for command in (("ps", "-aq"), ("volume", "ls", "-q"), ("network", "ls", "-q")):
            listed = docker(*command, "--filter", label)
            assert listed.returncode == 0 and not listed.stdout.strip(), (command, listed.stdout, listed.stderr)

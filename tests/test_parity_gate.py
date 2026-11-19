"""The free parity gate (migration step 19): scripted fixture trials on the old Inspect path and the native
Harbor task's path give the same world, event log and grades, apart from known differences
(`refund_eval.parity.run.KNOWN`).

Needs Docker (the real browser runs in its container on the in-process paths) and skips without it.
The `harbor run --environment docker` path builds the task images and takes about a minute per script
once they are cached; it also needs REFUND_EVAL_PARITY_HARBOR=1. Free: mockllm target, fake world
models, no API keys. The full gate with its report: `uv run python -m refund_eval.parity`.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

from refund_eval.parity import run
from refund_eval.parity.scripts import SCRIPTS
from refund_eval.published.browser_runtimes import DEFAULT_IMAGE


@pytest.mark.parametrize("change", [None, "different", "missing_identity", "wrong_digest", "tampered", "manifest"])
def test_harbor_reuse_checks_approved_wheel_before_launch(tmp_path, monkeypatch, change):
    wheel = tmp_path / "inert-0.0.1-py3-none-any.whl"
    wheel.write_bytes(b"inert wheel bytes, never installed or launched")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    script = SCRIPTS["turn_cap"]
    task = tmp_path / "tasks" / script.scenario
    agent_dir = task / "environment/agent"
    (agent_dir / "wheels").mkdir(parents=True)
    (task / "environment" / "Dockerfile").write_text("FROM python:3.14\n", encoding="utf-8")
    (agent_dir / "wheels" / wheel.name).write_bytes(wheel.read_bytes())
    (task / "task.toml").write_text(
        f'[metadata]\nagent_wheel_filename = "{wheel.name}"\nagent_wheel_sha256 = "{digest}"\n'
    )
    manifest = {"filename": wheel.name, "sha256": digest}
    if change == "different":
        wheel.write_bytes(b"different approved wheel")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    elif change == "missing_identity":
        (task / "task.toml").write_text("[metadata]\n")
    elif change == "wrong_digest":
        digest = "0" * 64
    elif change == "tampered":
        (agent_dir / "wheels" / wheel.name).write_bytes(b"tampered")
    elif change == "manifest":
        manifest["sha256"] = "0" * 64
    (agent_dir / "agent-wheel.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("REFUND_EVAL_AGENT_WHEEL", str(wheel))
    monkeypatch.setenv("REFUND_EVAL_AGENT_WHEEL_SHA256", digest)
    calls = []

    class LaunchIntercepted(Exception):
        pass

    def intercept(command, **kwargs):
        calls.append(command)
        raise LaunchIntercepted

    monkeypatch.setattr(run.subprocess, "run", intercept)
    if change is None:
        with pytest.raises(LaunchIntercepted):
            run.run_harbor(script, tmp_path, 7)
        assert len(calls) == 1 and calls[0][1:3] == ["harbor", "run"]
    else:
        with pytest.raises(ValueError):
            run.run_harbor(script, tmp_path, 7)
        assert not calls


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", DEFAULT_IMAGE], capture_output=True, check=False)
    return probe.returncode == 0


DOCKER = pytest.mark.skipif(not _docker_ready(), reason=f"Needs Docker and the browser image {DEFAULT_IMAGE}")
HARBOR = pytest.mark.skipif(os.environ.get("REFUND_EVAL_PARITY_HARBOR") != "1",
                            reason="Set REFUND_EVAL_PARITY_HARBOR=1 to run the local Harbor Docker trials")


def unexplained(name: str, comparison: dict) -> list:
    return [(key, path, a, b) for key, c in comparison.items() for path, a, b in [*c["world"], *c["events"],
                                                                                    *c["grades"]]
            if run.classify(name, key, path) is None]


def check(name: str, out, paths) -> None:
    for path in paths:
        result = run.spawn(path, SCRIPTS[name], out / path, 7)
        assert result["ok"], result
        assert result["stop_reason"] == SCRIPTS[name].expected_stop, result
    comparison = run.compare(out)
    assert {f"{a}_vs_{b}" for a, b in [("old", "inprocess"), ("old", "harbor"), ("inprocess", "harbor")]
            if a in paths and b in paths} == set(comparison)
    assert unexplained(name, comparison) == []
    for c in comparison.values():
        assert c["event_order_same"]


def test_inprocess_task_args_write_public_agent_scenario(tmp_path):
    from refund_eval.scenario.schema import load_agent_scenario, load_scenario

    script = SCRIPTS["turn_cap"]
    args = run.inprocess_task_args(script, "http://127.0.0.1:8000", tmp_path)
    public = load_agent_scenario(args["scenario"])
    full = load_scenario(script.scenario)
    if args["world_url"] != "http://127.0.0.1:8000":
        pytest.fail("In-process parity lost its world-service URL", pytrace=False)
    if set(public.model_dump(mode="json")) != {"id", "task_text", "start_time", "sim_weeks", "user"}:
        pytest.fail("Parity caller wrote a non-public top-level field", pytrace=False)
    if set(public.model_dump(mode="json")["user"]) != {"name", "email_addresses", "phone"}:
        pytest.fail("Parity caller wrote a non-public user field", pytrace=False)
    if (public.id, public.task_text, public.start_time, public.sim_weeks) != (
        full.id, full.task_text, full.start_time, full.sim_weeks,
    ):
        pytest.fail("Parity caller changed the public scenario inputs", pytrace=False)


@DOCKER
@pytest.mark.parametrize("name", list(SCRIPTS))
def test_in_process_paths_match(name, tmp_path):
    check(name, tmp_path, ["old", "inprocess"])


@DOCKER
@HARBOR
@pytest.mark.parametrize("name", list(SCRIPTS))
def test_harbor_docker_path_matches(name, tmp_path):
    check(name, tmp_path, ["old", "inprocess", "harbor"])


def test_token_ledger_events_are_compared_by_the_calls_they_record():
    def ledger(seq, calls, set_=False):
        payload = ({"path": ["token_ledger"], "value": {"calls": calls}} if set_ else {"append": {"calls": calls}})
        return {"seq": seq, "kind": "field_set" if set_ else "token_ledger_updated", "sim_time": "t", "payload": payload}

    mail = {"kind": "mail_queued", "sim_time": "t", "payload": {"id": "m"}}
    native = [ledger(0, [], set_=True), ledger(1, ["c1"]), {**mail, "seq": 2}]
    remote = [{**mail, "seq": 0}, ledger(1, ["c1"], set_=True)]
    found = run.compare_events(native, remote)
    assert not found["whole_same"] and found["order_same"]
    assert [p for p, _, _ in found["diffs"]] == ["events[token ledger].positions"]
    assert [p for p, _, _ in run.compare_events(native, [*remote, ledger(2, ["c2"])])["diffs"]] == [
        "events[token ledger].calls", "events[token ledger].positions"]
    moved = run.compare_events(native, [{**mail, "payload": {"id": "other"}, "seq": 0}])
    assert ("events[mail_queued #0].payload.id", "m", "other") in moved["diffs"]


def test_gate_fails_when_a_path_run_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "spawn", lambda path, script, out, seed: {"ok": False, "error": "ValueError: no image"})
    monkeypatch.setattr(run, "compare", lambda directory: {})
    out = tmp_path / "parity"
    assert run.main(["--out", str(out), "--scripts", "turn_cap", "--skip-harbor"]) == 1
    results = json.loads((out / "results.json").read_text())
    assert results["passed"] is False
    assert results["failed_runs"] == ["turn_cap/old", "turn_cap/inprocess"]
    assert "**Gate: FAIL.**" in (out / "REPORT.md").read_text()

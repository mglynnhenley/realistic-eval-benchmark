"""Synthetic local processes only: no coordinator, network, provider or ledger."""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[1] / "refund_eval/realism/durable_worker.py"
SPEC = importlib.util.spec_from_file_location("private_durable_worker", HELPER)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)
SAFE_ENV = {"PATH": "/usr/bin:/bin", "PYTHON_DOTENV_DISABLED": "1", "MODAL_CONFIG_PATH": "/dev/null",
            "PYTHONDONTWRITEBYTECODE": "1"}


def wait_for(condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("Synthetic process did not reach expected checkpoint")


def receipt(job, name):
    path = job / name
    return wait_for(lambda: worker.read(path) if path.exists() and path.stat().st_size else None)


def checkpoint(path):
    try:
        return int(path.read_text())
    except (FileNotFoundError, ValueError):
        return 0


@pytest.fixture
def scenario(tmp_path):
    jobs, launchers = [], []

    def create(*, mode="cooperate", ticks=10000, paused=False, bridge=False):
        root = tmp_path / str(len(jobs))
        root.mkdir()
        job, progress, payload = root / "job", root / "checkpoint", root / "payload.py"
        output = root / "fresh-output"
        payload.write_text(
            "import json, os, signal, sys, time\n"
            f"sys.path.insert(0, {str(HELPER.parent)!r})\n"
            "from durable_worker import check_pause\n"
            f"mode, progress, ticks = {mode!r}, {str(progress)!r}, {ticks!r}\n"
            "if mode == 'import':\n"
            "    import refund_eval\n"
            "    from pathlib import Path\n"
            "    assert Path(refund_eval.__file__).resolve().parent.parent == Path.cwd()\n"
            "if mode == 'exception': raise RuntimeError('SYNTHETIC_SECRET_EXCEPTION')\n"
            "if mode == 'ignore': signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "if mode == 'bridge':\n"
            f"    os.mkdir({str(output)!r})\n"
            f"    with open({str(output / 'control.json')!r}, 'w') as f:\n"
            "        json.dump({'paused': False, 'owner_field': 'preserved'}, f)\n"
            "for n in range(1, ticks + 1):\n"
            "    if mode == 'cooperate': check_pause()\n"
            "    if mode == 'bridge':\n"
            f"        with open({str(output / 'control.json')!r}) as f: control = json.load(f)\n"
            "        if control['paused']: break\n"
            "    with open(progress, 'w') as f: f.write(str(n))\n"
            "    print('synthetic checkpoint', n, flush=True)\n"
            "    time.sleep(0.02)\n"
        )
        data = {"version": 1, "job_dir": str(job), "cwd": str(root),
                "python_file": {"path": str(payload), "sha256": worker.sha(payload)}, "args": [], "input_pins": [],
                "start_paused": paused, "pause_grace_seconds": 0.2, "term_grace_seconds": 0.15,
                "cooperative_control": {"new_output_dir": str(output), "path": str(output / "control.json")} if bridge else None}
        config = root / "launch.json"
        worker.write(config, data)
        item = {"job": job, "progress": progress, "payload": payload, "config": config, "data": data,
                "sha": worker.sha(config), "root": root, "output": output}
        jobs.append(item)
        return item

    def launch(item, *, keep_launcher=False, extra_env=None):
        # An external launcher is killed in the survival test. The real detached
        # supervisor/task inherit ONLY this credential-free local-test environment.
        script = (
            "import subprocess,sys,time\n"
            f"subprocess.run([sys.executable, {str(HELPER)!r}, 'launch', '--config', {str(item['config'])!r}, "
            f"'--config-sha256', {item['sha']!r}], check=True)\n"
            + ("time.sleep(60)\n" if keep_launcher else "")
        )
        output = (item["root"] / "launcher.log").open("wb")
        process = subprocess.Popen([sys.executable, "-u", "-c", script], env={**SAFE_ENV, **(extra_env or {})},
                                   stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
        output.close()
        launchers.append(process)
        if not keep_launcher:
            assert process.wait(timeout=10) == 0, (item["root"] / "launcher.log").read_text()
        return process

    yield create, launch
    # Cleanup is restricted to synthetic /tmp fixture directories and matching
    # PID/start/boot/session ownership, even after failed assertions.
    for process in launchers:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
    for item in jobs:
        for name in ("task-started.json", "child-dispatched.json", "supervisor-started.json"):
            path = item["job"] / name
            if path.exists():
                saved = worker.read(path)["process"]
                if worker.alive(saved):
                    if name == "supervisor-started.json":
                        os.kill(saved["pid"], signal.SIGKILL)
                    else:
                        worker.signal_owned(saved, signal.SIGKILL)


def test_launcher_group_kill_preserves_worker_checkpoints_log_and_exit(scenario):
    create, launch = scenario
    item = create(ticks=35)
    launcher = launch(item, keep_launcher=True)
    task = receipt(item["job"], "task-started.json")["process"]
    supervisor = receipt(item["job"], "supervisor-started.json")["process"]
    wait_for(lambda: checkpoint(item["progress"]) >= 2)
    before = checkpoint(item["progress"])
    assert task["session_id"] == task["pid"] == task["pgid"]
    assert supervisor["session_id"] != launcher.pid
    assert task["session_id"] != supervisor["session_id"]
    os.killpg(launcher.pid, signal.SIGKILL)
    assert launcher.wait(timeout=5) == -signal.SIGKILL
    wait_for(lambda: checkpoint(item["progress"]) > before + 2)
    result = receipt(item["job"], "process-ended.json")
    assert result["status"] == "complete" and result["child_exit_observed"]
    assert result["child_returncode"] == 0
    assert checkpoint(item["progress"]) == 35
    assert "synthetic checkpoint 35" in (item["job"] / "worker.log").read_text()
    assert result["worker_log_sha256"] == worker.sha(item["job"] / "worker.log")
    assert not result["own_os_exit_observed"]
    assert (item["job"].stat().st_mode & 0o777) == 0o700
    assert ((item["job"] / "worker.log").stat().st_mode & 0o777) == 0o600


def test_duplicate_launch_refused_during_and_after_completion(scenario):
    create, launch = scenario
    item = create(ticks=18)
    launch(item)
    receipt(item["job"], "task-started.json")
    intent = (item["job"] / "launch-intent.json").read_bytes()
    for completed in (False, True):
        if completed:
            receipt(item["job"], "process-ended.json")
        with pytest.raises(FileExistsError):
            worker.launch(item["config"], item["sha"])
        assert (item["job"] / "launch-intent.json").read_bytes() == intent


def test_cooperative_file_pause_stops_checkpoint_progress(scenario):
    create, launch = scenario
    item = create()
    launch(item)
    wait_for(lambda: checkpoint(item["progress"]) >= 2)
    worker.request_pause(item["job"])
    result = receipt(item["job"], "process-ended.json")
    assert result["status"] == "paused"
    assert receipt(item["job"], "task-ended.json")["outcome"] == "cooperative_pause"
    before = checkpoint(item["progress"])
    time.sleep(0.1)
    assert checkpoint(item["progress"]) == before
    assert not (item["job"] / "term-sent.json").exists()


def test_pause_bridge_preserves_payload_control_fields(scenario):
    create, launch = scenario
    item = create(mode="bridge", bridge=True)
    launch(item)
    wait_for(lambda: checkpoint(item["progress"]) >= 2)
    worker.request_pause(item["job"])
    result = receipt(item["job"], "process-ended.json")
    assert result["status"] == "paused"
    bridged = worker.read(item["output"] / "control.json")
    assert bridged["paused"] and bridged["owner_field"] == "preserved"
    assert bridged["durable_pause_source"] == str(item["job"] / "control.json")


def test_existing_bridge_directory_is_never_modified(scenario):
    create, _ = scenario
    item = create(bridge=True)
    item["output"].mkdir()
    control = item["output"] / "control.json"
    control.write_text('{"historical":true}')
    with pytest.raises(ValueError, match="existing/frozen"):
        worker.launch(item["config"], item["sha"])
    assert control.read_text() == '{"historical":true}' and not item["job"].exists()


def test_supervisor_signal_receipt_and_cooperative_pause(scenario):
    create, launch = scenario
    item = create()
    launch(item)
    wait_for(lambda: checkpoint(item["progress"]) >= 2)
    saved = receipt(item["job"], "supervisor-started.json")["process"]
    assert worker.alive(saved)
    os.kill(saved["pid"], signal.SIGTERM)
    result = receipt(item["job"], "process-ended.json")
    assert result["status"] == "paused"
    assert receipt(item["job"], "signals/000.json")["name"] == "SIGTERM"
    assert not (item["job"] / "term-sent.json").exists()


def test_child_sigkill_observed_without_fabricated_finalizer(scenario):
    create, launch = scenario
    item = create()
    launch(item)
    saved = receipt(item["job"], "task-started.json")["process"]
    worker.signal_owned(saved, signal.SIGKILL)
    result = receipt(item["job"], "process-ended.json")
    assert result["child_returncode"] == -signal.SIGKILL and result["child_signal"] == "SIGKILL"
    assert result["child_exit_observed"] and not result["task_finalizer_present"]
    assert not (item["job"] / "task-ended.json").exists()
    assert result["remote_state"] == "not_established_by_local_exit"


def test_both_processes_killed_stays_unknown_and_never_replays(scenario):
    create, launch = scenario
    item = create()
    launch(item)
    saved = receipt(item["job"], "task-started.json")["process"]
    supervisor = receipt(item["job"], "supervisor-started.json")["process"]
    os.kill(supervisor["pid"], signal.SIGKILL)
    worker.signal_owned(saved, signal.SIGKILL)
    wait_for(lambda: not worker.alive(saved) and not worker.alive(supervisor))
    result = worker.status(item["job"])
    assert result["status"] == "interrupted_or_startup_unknown"
    assert result["remote_state"] == "unknown" and not result["automatic_replay"]
    assert not (item["job"] / "process-ended.json").exists()
    with pytest.raises(FileExistsError):
        worker.launch(item["config"], item["sha"])


def test_supervisor_receipt_does_not_conceal_live_child(scenario):
    create, _ = scenario
    item = create()
    job, token = item["job"], "synthetic-single-use-token"
    job.mkdir(mode=0o700)
    worker.write(job / "launch-intent.json", {"token": token, "helper_sha256": worker.sha(HELPER),
                 "config_path": str(item["config"]), "config_sha256": item["sha"]}, exclusive=True)
    (job / "configuration.json").write_bytes(item["config"].read_bytes())
    worker.write(job / "control.json", {"paused": False, "stop": False}, exclusive=True)
    (job / "worker.log").touch(mode=0o600)
    # Inject a real supervisor write failure after dispatch, without changing
    # the pinned helper bytes or stopping its live synthetic task.
    script = (
        "import pathlib,sys,time\n"
        f"sys.path.insert(0, {str(HELPER.parent)!r})\n"
        "import durable_worker as w\n"
        "original, failed = w.write, False\n"
        "def injected(path, value, **kwargs):\n"
        "    global failed\n"
        "    if pathlib.Path(path).name == 'state.json' and not failed:\n"
        "        failed = True\n"
        f"        marker = pathlib.Path({str(job / 'task-started.json')!r})\n"
        "        deadline = time.monotonic() + 5\n"
        "        while not marker.exists() and time.monotonic() < deadline: time.sleep(.01)\n"
        "        raise OSError('synthetic write failure')\n"
        "    return original(path, value, **kwargs)\n"
        "w.write = injected\n"
        f"raise SystemExit(w.supervise(pathlib.Path({str(job)!r}), {token!r}))\n"
    )
    process = subprocess.run([sys.executable, "-u", "-c", script], env=SAFE_ENV,
                             capture_output=True, check=False, timeout=8, start_new_session=True)
    assert process.returncode == 1
    assert receipt(job, "supervisor-exception.json")["child_may_still_be_running"]
    result = worker.status(job)
    assert result["status"] == "child_active_after_supervisor_failure"
    assert result["child_state_uncertain"] and result["requires_original_operation_reconciliation"]
    assert result["processes"]["task"]["alive_with_matching_start_identity"]


def test_exception_receipt_omits_exception_message(scenario):
    create, launch = scenario
    item = create(mode="exception")
    launch(item)
    result = receipt(item["job"], "process-ended.json")
    assert result["child_returncode"] == 1 and result["status"] == "failed"
    assert receipt(item["job"], "task-exception.json")["error_type"] == "RuntimeError"
    for path in item["job"].rglob("*"):
        if path.is_file():
            assert b"SYNTHETIC_SECRET_EXCEPTION" not in path.read_bytes()


def test_start_paused_has_no_task_dispatch(scenario):
    create, launch = scenario
    item = create(paused=True)
    launch(item)
    result = receipt(item["job"], "process-ended.json")
    assert result["status"] == "paused_before_dispatch" and not result["child_started"]
    assert not item["progress"].exists() and not (item["job"] / "child-launch-intent.json").exists()
    with pytest.raises(FileExistsError):
        worker.launch(item["config"], item["sha"])


def test_source_pin_mismatch_blocks_launch_and_predispatch(scenario):
    create, launch = scenario
    item = create()
    original = item["payload"].read_bytes()
    item["payload"].write_text("raise AssertionError('must not run')")
    with pytest.raises(ValueError, match="Pinned"):
        worker.launch(item["config"], item["sha"])
    assert not item["job"].exists()
    item["payload"].write_bytes(original)
    item["data"]["start_paused"] = True
    worker.write(item["config"], item["data"])
    item["sha"] = worker.sha(item["config"])
    launch(item)
    receipt(item["job"], "process-ended.json")
    item["payload"].write_text("raise AssertionError('must not run')")
    token = worker.read(item["job"] / "launch-intent.json")["token"]
    result = subprocess.run([sys.executable, str(HELPER), "_task", str(item["job"]), token],
                            env=SAFE_ENV, capture_output=True, check=False)
    assert result.returncode != 0
    assert not item["progress"].exists() and not (item["job"] / "task-started.json").exists()


def test_large_pins_not_rehashed_during_control_poll(scenario, monkeypatch):
    create, launch = scenario
    item = create(paused=True)
    large = item["root"] / "large-state.bin"
    large.write_bytes(b"synthetic\x00" * 200000)
    item["data"]["input_pins"] = [{"path": str(large), "sha256": worker.sha(large)}]
    worker.write(item["config"], item["data"])
    item["sha"] = worker.sha(item["config"])
    launch(item)
    receipt(item["job"], "process-ended.json")
    token = worker.read(item["job"] / "launch-intent.json")["token"]
    hashed = []
    actual_sha = worker.sha
    def observed(path):
        hashed.append(Path(path))
        return actual_sha(path)
    monkeypatch.setattr(worker, "sha", observed)
    worker.frozen(item["job"], token, full=False)
    assert large not in hashed and item["payload"] not in hashed
    large.write_bytes(b"changed")
    worker.frozen(item["job"], token, full=False)
    with pytest.raises(ValueError, match="Pinned"):
        worker.frozen(item["job"], token)


def test_changed_config_latches_pause_and_repeated_error_is_safe(scenario):
    create, launch = scenario
    item = create(mode="ignore")
    launch(item)
    wait_for(lambda: checkpoint(item["progress"]) >= 2)
    item["config"].write_text('{}')
    result = receipt(item["job"], "process-ended.json")
    assert result["status"] == "paused" and result["child_returncode"] == -signal.SIGKILL
    assert (item["job"] / "control-or-source-error.json").exists()
    assert (item["job"] / "kill-sent.json").exists()
    assert not (item["job"] / "supervisor-exception.json").exists()


def test_pause_remains_latched_if_control_is_cleared(scenario):
    create, launch = scenario
    item = create(mode="ignore")
    launch(item)
    wait_for(lambda: checkpoint(item["progress"]) >= 2)
    worker.request_pause(item["job"])
    receipt(item["job"], "pause-requested.json")
    worker.write(item["job"] / "control.json", {"paused": False, "stop": False})
    result = receipt(item["job"], "process-ended.json")
    assert result["status"] == "paused" and result["child_returncode"] == -signal.SIGKILL
    assert worker.read(item["job"] / "control.json")["paused"]


def test_pid_start_identity_mismatch_is_not_owned():
    saved = worker.identity(os.getpid())
    assert worker.alive(saved)
    assert not worker.alive({**saved, "start_ticks": saved["start_ticks"] + 1})
    assert not worker.alive({**saved, "boot_id": "different-boot"})


def test_helper_source_mutation_is_detected_without_editing_real_helper(scenario, monkeypatch):
    create, launch = scenario
    item = create(paused=True)
    launch(item)
    receipt(item["job"], "process-ended.json")
    token = worker.read(item["job"] / "launch-intent.json")["token"]
    changed = item["root"] / "changed-helper.py"
    changed.write_text("# different helper bytes\n")
    monkeypatch.setattr(worker, "__file__", str(changed))
    with pytest.raises(ValueError, match="helper source"):
        worker.frozen(item["job"], token, full=False)


def test_exclusive_receipt_publication_preserves_first_bytes(tmp_path):
    path = tmp_path / "exclusive.json"
    worker.write(path, {"first": True}, exclusive=True)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        worker.write(path, {"second": True}, exclusive=True)
    assert path.read_bytes() == before
    assert sorted(item.name for item in tmp_path.iterdir()) == ["exclusive.json"]
    assert (path.stat().st_mode & 0o777) == 0o600


def test_payload_imports_trusted_cwd_and_receipts_never_dump_environment(scenario):
    create, launch = scenario
    item = create(mode="import", ticks=1)
    # Discover the repository from this test file without relying on cwd/env.
    item["data"]["cwd"] = str(next(path for path in HELPER.parents if (path / "refund_eval" / "__init__.py").is_file()))
    worker.write(item["config"], item["data"])
    item["sha"] = worker.sha(item["config"])
    launch(item, extra_env={"OPENROUTER_API_KEY": "SYNTHETIC_CREDENTIAL_VALUE", "UNLISTED_ENV": "SYNTHETIC_UNLISTED_VALUE"})
    result = receipt(item["job"], "process-ended.json")
    assert result["status"] == "complete" and result["child_returncode"] == 0
    for name in ("launch-intent.json", "supervisor-started.json", "task-started.json"):
        environment = worker.read(item["job"] / name)["environment"]
        assert environment["credential_nonempty"]["OPENROUTER_API_KEY"] is True
        assert environment["credential_nonempty"]["ANTHROPIC_API_KEY"] is False
        assert "PATH" in environment["safe_names_present"]
        assert "UNLISTED_ENV" not in environment["safe_names_present"]
    for path in item["job"].rglob("*"):
        if path.is_file():
            assert b"SYNTHETIC_CREDENTIAL_VALUE" not in path.read_bytes()
            assert b"SYNTHETIC_UNLISTED_VALUE" not in path.read_bytes()


def test_exclusive_final_path_absent_until_dump_finishes(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "process-ended.json"
    dump_started, finish_dump = threading.Event(), threading.Event()
    actual_dump = worker.json.dump

    def controlled_dump(value, stream, **kwargs):
        # Simulate a slow writer with real partial bytes visible only at its
        # temporary path. The reader must not see the final receipt yet.
        stream.write('{"partial":')
        stream.flush()
        dump_started.set()
        assert finish_dump.wait(timeout=5)
        stream.seek(0)
        stream.truncate()
        actual_dump(value, stream, **kwargs)

    monkeypatch.setattr(worker.json, "dump", controlled_dump)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.write, path, {"complete": True}, exclusive=True)
        try:
            assert dump_started.wait(timeout=5)
            assert not path.exists()
            temporary_files = list(tmp_path.iterdir())
            assert len(temporary_files) == 1
            assert temporary_files[0].read_bytes() == b'{"partial":'
        finally:
            finish_dump.set()
        future.result(timeout=5)
    assert worker.read(path) == {"complete": True}
    assert list(tmp_path.iterdir()) == [path]


def test_interrupted_dump_never_publishes_partial_receipt(tmp_path, monkeypatch):
    path = tmp_path / "process-ended.json"

    def interrupted_dump(value, stream, **kwargs):
        stream.write('{"partial":')
        stream.flush()
        assert not path.exists()
        raise RuntimeError("synthetic JSON interruption")

    monkeypatch.setattr(worker.json, "dump", interrupted_dump)
    with pytest.raises(RuntimeError, match="synthetic JSON interruption"):
        worker.write(path, {"complete": True}, exclusive=True)
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []

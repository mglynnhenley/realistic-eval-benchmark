"""Single-use detached Python worker/supervisor, independent of model accounting.

Standard library only. Reuses background.py's exclusive ownership, double fork,
setsid and private atomic receipt patterns. There is no retry, ledger API, model
client, dotenv loader, coordinator default, or automatic recovery in this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


def read(path):
    value = json.loads(Path(path).read_bytes())
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object")
    return value


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path, value, *, exclusive=False):
    """Private, fsynced receipts; exclusive intents never overwrite uncertainty."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            # Publish whole bytes atomically while preserving O_EXCL semantics.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)



def identity(pid):
    """Linux boot/start identity prevents treating a reused PID as this worker."""
    stat_text = Path(f"/proc/{pid}/stat").read_text()
    fields = stat_text[stat_text.rfind(")") + 2:].split()
    return {"pid": int(pid), "ppid": int(fields[1]), "pgid": int(fields[2]), "session_id": int(fields[3]),
            "start_ticks": int(fields[19]), "state": fields[0],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "cmdline_sha256": sha(f"/proc/{pid}/cmdline"),
            "cgroup_sha256": sha(f"/proc/{pid}/cgroup")}


def alive(saved):
    try:
        current = identity(saved["pid"])
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return current["state"] != "Z" and all(current[key] == saved[key] for key in
            ("pid", "start_ticks", "boot_id", "pgid", "session_id", "cmdline_sha256"))


def environment_receipt():
    """Allowlisted names and nonempty booleans only; never values or hashes."""
    names = ("PATH", "PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
             "PYTHON_DOTENV_DISABLED", "MODAL_CONFIG_PATH", "DURABLE_JOB_CONTROL")
    credentials = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                   "GOOGLE_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY")
    return {"safe_names_present": [name for name in names if name in os.environ],
            "credential_nonempty": {name: bool(os.environ.get(name)) for name in credentials}}


def validated(config):
    fields = {"version", "job_dir", "cwd", "python_file", "args", "input_pins", "start_paused",
              "pause_grace_seconds", "term_grace_seconds", "cooperative_control"}
    if set(config) != fields or config["version"] != 1:
        raise ValueError("Unknown or missing explicit worker configuration field")
    for field in ("job_dir", "cwd"):
        path = Path(config[field])
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("Canonical absolute worker paths required")
    if not Path(config["cwd"]).is_dir() or type(config["start_paused"]) is not bool:
        raise ValueError("Existing cwd and explicit initial pause required")
    if not isinstance(config["args"], list) or any(not isinstance(arg, str) or "\x00" in arg for arg in config["args"]):
        raise ValueError("Use literal Python argv strings without secret values")
    for field in ("pause_grace_seconds", "term_grace_seconds"):
        if type(config[field]) not in (int, float) or not math.isfinite(config[field]) or not 0 < config[field] <= 3600:
            raise ValueError("Explicit positive bounded graceful-stop periods required")
    if not isinstance(config["input_pins"], list):
        raise TypeError("Explicit input pin list required")
    for item in [config["python_file"], *config["input_pins"]]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("Each pin needs an exact path and raw SHA")
        path = Path(item["path"])
        if not path.is_absolute() or path.resolve() != path or not path.is_file() or path.is_symlink() or sha(path) != item["sha256"]:
            raise ValueError("Pinned Python source/input bytes changed")
    bridge = config["cooperative_control"]
    if bridge is not None:
        if not isinstance(bridge, dict) or set(bridge) != {"new_output_dir", "path"}:
            raise ValueError("Explicit fresh cooperative control bridge required")
        root, path = Path(bridge["new_output_dir"]), Path(bridge["path"])
        if not root.is_absolute() or root.resolve() != root or path != root / "control.json":
            raise ValueError("Cooperative bridge may target only new_output_dir/control.json")
    return config


def frozen(job, token, *, full=True):
    """Full pins before dispatch; only bounded helper/config bytes while polling."""
    intent = read(job / "launch-intent.json")
    for path, limit in ((Path(__file__), 131_072), (Path(intent["config_path"]), 65_536), (job / "configuration.json", 65_536)):
        if path.stat().st_size > limit:
            raise ValueError("Helper/config grew beyond bounded poll size")
    if intent["token"] != token or intent["helper_sha256"] != sha(Path(__file__).resolve()):
        raise ValueError("Worker ownership or helper source changed")
    if sha(intent["config_path"]) != intent["config_sha256"] or sha(job / "configuration.json") != intent["config_sha256"]:
        raise ValueError("Original/frozen worker configuration changed")
    config = read(job / "configuration.json")
    return validated(config) if full else config


class PauseRequested(InterruptedError):
    """Cooperative pause never implies remote cancellation or a known charge."""


def check_pause(control_path=None):
    """Call this from a coordinator's existing pre-dispatch pause/spend guard."""
    path = control_path or os.environ.get("DURABLE_JOB_CONTROL")
    if not path:
        raise ValueError("Explicit durable worker control path required")
    control = read(path)
    if any(type(control.get(key)) is not bool for key in ("paused", "stop")):
        raise ValueError("Explicit boolean pause and stop controls required")
    if control["paused"] or control["stop"]:
        raise PauseRequested("Durable worker pause requested; preserve outstanding operations")


def launch(config_path, expected_sha256):
    config_path = Path(config_path).resolve(strict=True)
    if config_path.stat().st_size > 65_536:
        raise ValueError("Worker configuration exceeds 64 KiB; pin large inputs separately")
    if sha(config_path) != expected_sha256:
        raise ValueError("Raw worker configuration SHA differs")
    config = validated(read(config_path))
    bridge = config["cooperative_control"]
    if bridge and Path(bridge["new_output_dir"]).exists():
        raise ValueError("Never bridge pause into an existing/frozen coordinator output")
    job = Path(config["job_dir"])
    job.mkdir(parents=True, exist_ok=False, mode=0o700)
    token = uuid.uuid4().hex
    write(job / "launch-intent.json", {"at": now(), "token": token, "config_path": str(config_path),
          "config_sha256": expected_sha256, "helper_sha256": sha(Path(__file__).resolve()),
          "launcher": identity(os.getpid()), "environment": environment_receipt(), "no_automatic_replay": True}, exclusive=True)
    # Copy EXACT config bytes: the raw SHA remains authoritative.
    fd = os.open(job / "configuration.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(config_path.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    write(job / "control.json", {"paused": config["start_paused"], "stop": False}, exclusive=True)
    with (job / "worker.log").open("ab", buffering=0) as log:
        os.chmod(log.name, 0o600)
        try:
            subprocess.run([sys.executable, "-u", str(Path(__file__).resolve()), "_bootstrap", str(job), token],
                cwd=config["cwd"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True, check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as error:
            write(job / "launch-unknown.json", {"at": now(), "error_type": type(error).__name__,
                  "no_retry": True, "reason": "Detached spawn may already exist"}, exclusive=True)
    deadline = time.monotonic() + 5
    while not (job / "supervisor-started.json").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return status(job)


def bootstrap(job, token):
    # Same double-fork pattern as background.JobManager, in a fresh interpreter.
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)
        return 0
    os.setsid()
    if os.fork():
        os._exit(0)
    os.execv(sys.executable, [sys.executable, "-u", str(Path(__file__).resolve()), "_supervise", str(job), token])


def bridge_pause(config, job):
    bridge = config["cooperative_control"]
    if bridge is None or not Path(bridge["path"]).exists():
        return False
    path = Path(bridge["path"])
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Cooperative control became a symlink")
    current = read(path)
    if not current.get("paused"):
        write(path, {**current, "paused": True, "durable_pause_source": str(job / "control.json")})
    return True


def supervise(job, token):
    started = identity(os.getpid())
    write(job / "supervisor-started.json", {"at": now(), "process": started, "token": token, "environment": environment_receipt()}, exclusive=True)
    child, child_identity, config, signals = None, None, None, []
    stopping_at, term_at, reason, signal_cursor = None, None, None, 0
    control_error = False
    def requested(signum, frame):
        signals.append({"at": now(), "number": signum, "name": signal.Signals(signum).name})
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, requested)
    outcome = {"status": "supervisor_failed", "child_exit_observed": False}
    try:
        config = frozen(job, token)
        check_pause(job / "control.json")
        write(job / "child-launch-intent.json", {"at": now(), "python_file": config["python_file"],
              "no_replay": True}, exclusive=True)
        with (job / "worker.log").open("ab", buffering=0) as log:
            child = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "_task", str(job), token],
                    cwd=config["cwd"], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    env={**os.environ, "PYTHONUNBUFFERED": "1", "DURABLE_JOB_CONTROL": str(job / "control.json")},
                    start_new_session=True, close_fds=True)
            child_identity = identity(child.pid)
            write(job / "child-dispatched.json", {"at": now(), "process": child_identity}, exclusive=True)
            write(job / "state.json", {"status": "running", "supervisor": started, "child": child_identity})
            while child.poll() is None:
                while signal_cursor < len(signals):
                    write(job / "signals" / f"{signal_cursor:03d}.json", signals[signal_cursor], exclusive=True)
                    signal_cursor += 1
                try:
                    frozen(job, token, full=False)
                    control = read(job / "control.json")
                    if any(type(control.get(key)) is not bool for key in ("paused", "stop")):
                        raise ValueError("Malformed pause control")
                except (OSError, ValueError, TypeError, KeyError) as error:
                    if not control_error:
                        write(job / "control-or-source-error.json", {"at": now(), "error_type": type(error).__name__}, exclusive=True)
                        control_error = True
                    control = {"stop": True}
                stopping = bool(stopping_at is not None or signals or control.get("paused") or control.get("stop"))
                if stopping:
                    if stopping_at is None:
                        stopping_at, reason = time.monotonic(), "signal" if signals else "file_control"
                        write(job / "pause-requested.json", {"at": now(), "reason": reason,
                              "in_flight_costs_and_remote_state": "unknown_until_payload_accounts"}, exclusive=True)
                        # Task-level helper sees the same pause even when it originated as a supervisor signal.
                        write(job / "control.json", {**control, "paused": True})
                    if control.get("paused") is not True:
                        write(job / "control.json", {**control, "paused": True})
                    try:
                        bridge_pause(config, job)
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        if not (job / "pause-bridge-error.json").exists():
                            write(job / "pause-bridge-error.json", {"at": now(), "error_type": type(error).__name__}, exclusive=True)
                    elapsed = time.monotonic() - stopping_at
                    if term_at is None and elapsed >= config["pause_grace_seconds"] and alive(child_identity):
                        write(job / "term-sent.json", {"at": now(), "process": child_identity,
                              "accounting_state": "uncertain_until_original_receipts"}, exclusive=True)
                        signal_owned(child_identity, signal.SIGTERM)
                        term_at = time.monotonic()
                    elif term_at is not None and time.monotonic() - term_at >= config["term_grace_seconds"] and alive(child_identity):
                        write(job / "kill-sent.json", {"at": now(), "process": child_identity}, exclusive=True)
                        signal_owned(child_identity, signal.SIGKILL)
                        term_at = float("inf")
                os.fsync(log.fileno())
                time.sleep(0.025)
            code = child.wait()
            os.fsync(log.fileno())
        final_control = read(job / "control.json")
        pause_observed = stopping_at is not None or bool(signals) or bool(final_control.get("paused") or final_control.get("stop"))
        outcome = {"status": "paused" if pause_observed else "complete" if code == 0 else "failed",
                   "child_exit_observed": True, "child_returncode": code,
                   "child_signal": signal.Signals(-code).name if code < 0 else None,
                   "task_finalizer_present": (job / "task-ended.json").exists()}
    except PauseRequested:
        outcome = {"status": "paused_before_dispatch", "child_exit_observed": False, "child_started": False}
    except BaseException as error:  # noqa: BLE001 -- persist lifecycle uncertainty for every Python exit
        write(job / "supervisor-exception.json", {"at": now(), "error_type": type(error).__name__,
              "child_may_still_be_running": child is not None and child.poll() is None}, exclusive=True)
    finally:
        for index in range(signal_cursor, len(signals)):
            write(job / "signals" / f"{index:03d}.json", signals[index], exclusive=True)
        write(job / "process-ended.json", {**outcome, "at": now(), "supervisor_finalizing": started,
              "own_os_exit_observed": False, "child": child_identity, "worker_log_sha256": sha(job / "worker.log"),
              "remote_state": "not_established_by_local_exit", "automatic_replay": False}, exclusive=True)
        write(job / "state.json", outcome)
    return 0 if outcome["status"] in {"complete", "paused", "paused_before_dispatch"} else 1


class TaskSignal(BaseException):
    def __init__(self, signum):
        self.signum = signum


def task(job, token):
    started, config, observed = identity(os.getpid()), frozen(job, token), []
    write(job / "task-started.json", {"at": now(), "process": started, "python_file": config["python_file"],
          "environment": environment_receipt()}, exclusive=True)
    def interrupted(signum, frame):
        observed.append(signum)
        write(job / "task-signals" / f"{len(observed):03d}.json", {"at": now(), "signal": signum}, exclusive=True)
        raise TaskSignal(signum)
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, interrupted)
    code, outcome = 1, "exception"
    try:
        check_pause(job / "control.json")
        sys.dont_write_bytecode = True
        sys.argv = [config["python_file"]["path"], *config["args"]]
        # Entry-script parent first, then explicit trusted cwd, then the selected
        # interpreter environment. Caller owns dependency/source isolation.
        sys.path.insert(0, config["cwd"])
        sys.path.insert(0, str(Path(config["python_file"]["path"]).parent))
        runpy.run_path(config["python_file"]["path"], run_name="__main__")
        code, outcome = 0, "returned"
    except PauseRequested:
        code, outcome = 0, "cooperative_pause"
    except SystemExit as error:
        code = error.code if type(error.code) is int else 0 if error.code is None else 1
        outcome = "system_exit"
    except TaskSignal as error:
        code, outcome = 128 + error.signum, "catchable_signal"
    except BaseException as error:  # noqa: BLE001 -- task exceptions must leave durable class-only receipts
        # A payload can import this helper under its package name; its exception
        # class then differs from __main__. Confirm the shared control state.
        control = read(job / "control.json")
        if isinstance(error, InterruptedError) and (control.get("paused") or control.get("stop")):
            code, outcome = 0, "cooperative_pause"
        else:
            write(job / "task-exception.json", {"at": now(), "error_type": type(error).__name__,
                  "exception_text_omitted": True}, exclusive=True)
    finally:
        write(job / "task-ended.json", {"at": now(), "process": started, "outcome": outcome, "exit_code": code,
              "os_exit_observed": False, "scope": "python_finalizer", "signals": observed}, exclusive=True)
    return code


def signal_owned(saved, signum):
    """Only this invocation's separate child session/group may receive signals."""
    if saved["pgid"] == saved["pid"] == saved["session_id"] and alive(saved):
        try:
            os.killpg(saved["pgid"], signum)
        except ProcessLookupError:
            pass


def status(job):
    """Read-only observation; absent terminal receipts never invite replay."""
    job = Path(job)
    terminal = job / "process-ended.json"
    identities = {}
    for kind, name in (("supervisor", "supervisor-started.json"), ("task", "task-started.json")):
        path = job / name
        if path.exists():
            saved = read(path)["process"]
            identities[kind] = {"process": saved, "alive_with_matching_start_identity": alive(saved)}
    if "task" not in identities and (job / "child-dispatched.json").exists():
        saved = read(job / "child-dispatched.json")["process"]
        identities["task"] = {"process": saved, "alive_with_matching_start_identity": alive(saved)}
    running = any(row["alive_with_matching_start_identity"] for row in identities.values())
    if terminal.exists():
        receipt = read(terminal)
        task_live = identities.get("task", {}).get("alive_with_matching_start_identity", False)
        uncertain = not receipt.get("child_exit_observed") and receipt.get("status") != "paused_before_dispatch"
        return {**receipt, "job_dir": str(job), "processes": identities,
                "status": "child_active_after_supervisor_failure" if task_live else receipt["status"],
                "child_state_uncertain": uncertain,
                "requires_original_operation_reconciliation": uncertain or receipt["status"] != "complete"}
    return {"status": "running_without_terminal_receipt" if running else "interrupted_or_startup_unknown",
            "job_dir": str(job), "processes": identities, "automatic_replay": False,
            "remote_state": "unknown", "requires_original_operation_reconciliation": not running}


def request_pause(job, *, stop=False):
    job = Path(job)
    control = read(job / "control.json")
    write(job / "control.json", {**control, "paused": True, "stop": stop, "requested_at": now()})
    return status(job)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    command = actions.add_parser("launch")
    command.add_argument("--config", required=True, type=Path)
    command.add_argument("--config-sha256", required=True)
    for action in ("status", "pause", "stop"):
        actions.add_parser(action).add_argument("--job", required=True, type=Path)
    for action in ("_bootstrap", "_supervise", "_task"):
        command = actions.add_parser(action)
        command.add_argument("job", type=Path)
        command.add_argument("token")
    args = parser.parse_args(argv)
    if args.action == "launch":
        result = launch(args.config, args.config_sha256)
    elif args.action == "status":
        result = status(args.job)
    elif args.action in {"pause", "stop"}:
        result = request_pause(args.job, stop=args.action == "stop")
    else:
        return {"_bootstrap": bootstrap, "_supervise": supervise, "_task": task}[args.action](args.job, args.token)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

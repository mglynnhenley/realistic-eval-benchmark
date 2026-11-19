"""Run the parity scripts on three paths and compare them (migration step 19, the free parity gate).

    uv run python -m refund_eval.parity --out .context/parity [--scripts NAME ...] [--seed 7] [--skip-harbor]

For each script (`parity.scripts.SCRIPTS`), with fixture data, research mode `saved`, the world's
fake models and one seed:
  old        the native Inspect path in-process (tools and world in the agent's process; real browser
             in its Docker container); graded by the Inspect scorers
  inprocess  the world service on a thread of its own and the Inspect agent in world-service mode
             (real browser in its Docker container); graded by `grading.verifier`
  harbor     a real local `harbor run --environment docker` on an adapter-generated task: the world
             sidecar (browser as a subprocess in the world container), the scripted installed agent
             in `main` (`parity.harbor_agent`) and the separate verifier's reward.json

Each path runs in its own process. Each writes OUT/<script>/<path>/: world.json, events.jsonl,
reward.json, evidence.json and run.json. Then the final World (without wall-clock fields), the
event log (without wall_time) and the graded keys are compared with the old path's, and
OUT/REPORT.md and OUT/results.json are written. Free: mockllm target, fake world models, no API keys
in any environment launched (they are removed), no Modal.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import fnmatch
import hashlib
import json
import math
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from refund_eval.harbor.world_entrypoint import (
    FAKE_MODELS_ENV,
    FAKE_MODES,
    credentials_set,
)
from refund_eval.parity.scripts import SCRIPTS, Script

REPO = Path(__file__).resolve().parents[2]
HARBOR = REPO / "scripts" / "harbor"
TASK_FILE = "refund_eval/task.py"  # relative to REPO: Inspect rejects absolute task paths on Python 3.14
PATHS = ("old", "inprocess", "harbor")
WALL_CLOCK = {"wall_time"}
PROFILE_KEY = "published_tool_profile"
AGENT_IMPORT = "refund_eval.parity.harbor_agent:ScriptedRefundAgent"
SCORER_GROUPS = ("containment", "resource_use", "permission_checks", "long_horizon_activity")


def free_env(**extra: str) -> dict[str, str]:
    """This environment without credentials (names matched as `world_entrypoint.credentials_set`)."""
    env = {k: v for k, v in os.environ.items() if k not in set(credentials_set(dict(os.environ)))}
    env = {k: v for k, v in env.items() if k not in {"OPENROUTER_API_KEY", "OPEN_ROUTER_KEY",
                                                      "REFUND_EVAL_EXA_API_KEY", "REFUND_EVAL_KILL"}}
    return {**env, **extra, "PYTHON_DOTENV_DISABLED": "1"}


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_events(path: Path, events: list) -> None:
    path.write_text("".join(json.dumps(e, sort_keys=True, default=str) + "\n" for e in events), encoding="utf-8")


def _read_events(path: Path) -> list:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evidence(store: dict) -> dict:
    """Browser containment evidence from a trial's tool profile."""
    profile = store.get(PROFILE_KEY) or {}
    boundary = profile.get("browser_boundary") or {}
    return {"browser_runtime": profile.get("browser_runtime"), "browser_network": profile.get("browser_network"),
            "boundary_mode": boundary.get("mode"), "boundary_network_mode": boundary.get("network_mode"),
            "boundary_verified": boundary.get("verified"),
            "interception_integrity_verified": (boundary.get("interception_integrity") or {}).get("verified")}


def task_args(script: Script, **extra: Any) -> dict:
    args = {"scenario": script.scenario, "sim_weeks": script.sim_weeks, "published_data": "fixture", **extra}
    if script.cost_limit is not None:
        args["cost_limit"] = script.cost_limit
    return {**args, **script.task_args}


def inprocess_task_args(script: Script, world_url: str, out: Path) -> dict:
    from refund_eval.scenario.schema import AgentScenario, load_scenario

    args = task_args(script, world_url=world_url)
    scenario = load_scenario(args["scenario"])
    agent_scenario = AgentScenario.model_validate(scenario.model_dump(mode="json", include={
        "id": True,
        "task_text": True,
        "start_time": True,
        "sim_weeks": True,
        "user": {"name", "email_addresses", "phone"},
    }))
    path = (out / "agent-scenario.yaml").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(agent_scenario.model_dump_json(), encoding="utf-8")
    args["scenario"] = str(path)
    return args


def scripted_model(script: Script):
    from inspect_ai.model import get_model

    return get_model("mockllm/model", custom_outputs=script)


def _eval(script: Script, args: dict, log_dir: Path):
    from inspect_ai import eval as inspect_eval

    cwd = os.getcwd()
    os.chdir(REPO)
    try:
        [log] = inspect_eval(TASK_FILE, task_args=args, model=scripted_model(script), log_dir=str(log_dir),
                             log_format="eval", display="none")
    finally:
        os.chdir(cwd)
    return log


# --------------------------------------------------------------------------- old: native Inspect


def scorer_reward(scores: dict) -> dict[str, float]:
    """The Inspect scorers' values as the verifier's reward keys (as tests/fixtures/verifier.py)."""
    reward: dict[str, float] = {}
    if "containment" in scores:
        reward["containment"] = 1.0 if scores["containment"].value == "C" else 0.0
    for name in SCORER_GROUPS[1:]:
        value = scores[name].value if name in scores else None
        if isinstance(value, dict):
            reward.update({f"{name}.{k}": float(v) for k, v in value.items()
                           if not (isinstance(v, float) and math.isnan(v))})
    return reward


def run_old(script: Script, out: Path, seed: int) -> dict:
    from refund_eval.grading.verifier import check_event_log
    from refund_eval.harbor.world_entrypoint import FakeWorldModels
    from refund_eval.models import client as models
    from refund_eval.scenario.schema import load_scenario
    from refund_eval.world.event_log import EVENTS_KEY
    from refund_eval.world.state import WORLD_KEY, World

    models.install(FakeWorldModels(FAKE_MODES[script.fake_models]))
    log = _eval(script, task_args(script, max_turns=script.max_turns, research_mode="saved", seed=seed,
                                  artifact_dir=str(out / "artifacts")), out / "logs")
    [sample] = log.samples
    store = sample.store
    world, events = store[WORLD_KEY], store.get(EVENTS_KEY) or []
    reward = scorer_reward(sample.scores or {})
    # Not scorer keys: computed from the native trial as the verifier computes them.
    success = load_scenario(script.scenario).task.success_stop_reason
    computed = {"task_success": float(world.get("stop_reason") == success),
                "event_log_matches_state": float(check_event_log(World.model_validate(world), events)["matches"])}
    _write(out / "world.json", world)
    _write_events(out / "events.jsonl", events)
    _write(out / "reward.json", reward)
    _write(out / "computed.json", computed)
    _write(out / "evidence.json", evidence(store))
    return {"status": log.status, "stop_reason": world.get("stop_reason"), "turn": world.get("turn"),
            "limit": sample.limit.type if sample.limit else None, "log": str(Path(log.location).name)}


# --------------------------------------------------------------------------- inprocess: world service on a thread


def run_inprocess(script: Script, out: Path, seed: int) -> dict:
    from refund_eval.grading import verifier
    from refund_eval.harbor.world_entrypoint import FakeWorldModels
    from refund_eval.world_service import server
    from refund_eval.world_service.settings import WorldSettings
    from refund_eval.world_service.trial import WorldTrial

    turn_token, admin_token = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    os.environ["WORLD_TURN_TOKEN"] = turn_token
    world_dir = out / "world"
    settings = WorldSettings(scenario=script.scenario, seed=seed, sim_weeks=script.sim_weeks,
                             max_turns=script.max_turns, research_mode="saved", data_mode="fixture",
                             browser_runtime="docker", output_dir=str(world_dir))
    ready: queue.Queue = queue.Queue()
    stop = threading.Event()
    held: dict = {}

    async def serve() -> None:
        async with (WorldTrial(settings, model_client=FakeWorldModels(FAKE_MODES[script.fake_models])) as trial,
                    server.running(trial, host="127.0.0.1", port=0, admin_port=0, turn_token=turn_token,
                                   admin_token=admin_token) as (port, _)):
            ready.put(f"http://127.0.0.1:{port}")
            while not stop.is_set():
                await asyncio.sleep(0.02)
            held["data"] = trial.data()

    def target() -> None:
        try:
            asyncio.run(serve())
        except BaseException as ex:  # noqa: BLE001 - reported to the caller
            ready.put(ex)

    thread = threading.Thread(target=target, name="world-service")
    thread.start()
    url = ready.get(timeout=600)
    if isinstance(url, BaseException):
        raise url
    try:
        log = _eval(script, inprocess_task_args(script, url, out), out / "logs")
    finally:
        stop.set()
        thread.join(600)
    state = json.loads((world_dir / "state.json").read_text())
    events = _read_events(world_dir / "events.jsonl")
    # The verifier as test.sh runs it: the world's state and events, and the agent's .eval.
    with tempfile.TemporaryDirectory() as grading:
        for name in ("state.json", "events.jsonl"):
            shutil.copy(world_dir / name, grading)
        shutil.copy(log.location, grading)
        code = verifier.main(["--state", f"{grading}/state.json", "--events", f"{grading}/events.jsonl",
                              "--artifacts", grading, "--out", str(out / "verifier" / "reward.json")])
    if code != 0:
        raise RuntimeError(f"verifier exited {code}")
    shutil.copy(out / "verifier" / "reward.json", out / "reward.json")
    _write(out / "world.json", state["store"]["world"])
    _write_events(out / "events.jsonl", events)
    _write(out / "evidence.json", evidence(state["store"]))
    [sample] = log.samples
    return {"status": log.status, "stop_reason": state["stop_reason"], "turn": state["store"]["world"].get("turn"),
            "limit": sample.limit.type if sample.limit else None, "log": Path(log.location).name}


# --------------------------------------------------------------------------- harbor: local Docker


def stage_scripted_agent(task_dir: Path) -> None:
    """Stage parity-only fixtures outside the approved wheel payload for local tests."""
    source = REPO / "refund_eval" / "parity"
    agent = task_dir / "environment" / "agent"
    destination = agent / "parity-fixtures" / "refund_eval" / "parity"
    for filename in ("__init__.py", "inspect_cli.py", "scripts.py"):
        target = destination / filename
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / filename, target)

    marker = "# TEST-ONLY: overlay scripted parity fixtures after the approved wheel install"
    hook = (
        f"{marker}\n"
        'RUN /app/.venv/bin/python -c "import refund_eval; from pathlib import Path; import shutil; '
        "shutil.copytree('/app/parity-fixtures/refund_eval/parity', "
        "Path(refund_eval.__file__).parent / 'parity', dirs_exist_ok=True)\"\n"
    )
    dockerfile = task_dir / "environment" / "Dockerfile"
    content = dockerfile.read_text(encoding="utf-8")
    if marker not in content:
        dockerfile.write_text(content.rstrip() + "\n\n" + hook, encoding="utf-8")


def run_harbor(script: Script, out: Path, seed: int) -> dict:
    import yaml

    from refund_eval.harbor.adapter import read_verified_agent_wheel

    agent_wheel = os.environ.get("REFUND_EVAL_AGENT_WHEEL")
    agent_wheel_sha256 = os.environ.get("REFUND_EVAL_AGENT_WHEEL_SHA256")
    if not agent_wheel or not agent_wheel_sha256:
        raise ValueError("Set REFUND_EVAL_AGENT_WHEEL and REFUND_EVAL_AGENT_WHEEL_SHA256 for the Harbor parity path")
    approved = read_verified_agent_wheel(Path(agent_wheel), agent_wheel_sha256)
    env = free_env(**{FAKE_MODELS_ENV: script.fake_models, "WORLD_SEED": str(seed),
                      "REFUND_EVAL_KILL_FILE": str(out / "HOST_KILL_NEVER_SET")})
    tasks = out / "tasks"
    if not (tasks / script.scenario / "task.toml").is_file():
        subprocess.run([str(HARBOR), "python", "-m", "refund_eval.harbor.adapter", "--scenario", script.scenario,
                        "--out", str(tasks), "--data-mode", "fixture", "--research-mode", "saved",
                        "--max-turns", str(script.max_turns), "--sim-weeks", str(script.sim_weeks),
                        "--agent-wheel", agent_wheel, "--agent-wheel-sha256", agent_wheel_sha256],
                       cwd=REPO, env=env, check=True, capture_output=True, text=True)
    task = tasks / script.scenario
    metadata = tomllib.loads((task / "task.toml").read_text(encoding="utf-8")).get("metadata", {})
    identity = {"filename": approved.filename, "sha256": approved.sha256}
    if (metadata.get("agent_wheel_filename"), metadata.get("agent_wheel_sha256")) != (
        approved.filename, approved.sha256,
    ):
        raise ValueError("Existing Harbor task uses a different or unrecorded agent wheel; use a fresh output directory")
    agent_dir = task / "environment" / "agent"
    manifest = json.loads((agent_dir / "agent-wheel.json").read_text(encoding="utf-8"))
    if manifest != identity:
        raise ValueError("Existing Harbor task agent wheel manifest does not match the approved wheel")
    read_verified_agent_wheel(agent_dir / "wheels" / approved.filename, approved.sha256)
    stage_scripted_agent(task)
    kwargs: dict[str, Any] = {"script": script.name}
    if script.cost_limit is not None:
        kwargs["cost_limit"] = script.cost_limit
    job_name = f"parity-{script.name}-{int(time.time())}"
    job = {"job_name": job_name, "jobs_dir": str(out / "jobs"), "n_attempts": 1, "n_concurrent_trials": 1,
           "retry": {"max_retries": 0}, "environment": {"type": "docker", "delete": True},
           "agents": [{"import_path": AGENT_IMPORT, "model_name": "mockllm/model", "kwargs": kwargs, "env": {}}],
           "tasks": [{"path": str(tasks / script.scenario)}]}
    (out / "job.yaml").write_text(yaml.safe_dump(job, sort_keys=False), encoding="utf-8")
    with (out / "harbor.log").open("w", encoding="utf-8") as harbor_log:
        done = subprocess.run([str(HARBOR), "harbor", "run", "-y", "-c", str(out / "job.yaml")], cwd=REPO, env=env,
                              stdout=harbor_log, stderr=subprocess.STDOUT, check=False, timeout=3 * 3600)
    rewards = sorted((out / "jobs" / job_name).glob("*/verifier/reward.json"))
    if not rewards:
        raise RuntimeError(f"harbor run exited {done.returncode} without a reward.json; see {out / 'harbor.log'}")
    trial = rewards[-1].parent.parent
    world_dir = trial / "artifacts" / "world"
    state = json.loads((world_dir / "state.json").read_text())
    shutil.copy(trial / "verifier" / "reward.json", out / "reward.json")
    shutil.copy(world_dir / "events.jsonl", out / "events.jsonl")
    _write(out / "world.json", state["store"]["world"])
    seed_file = json.loads((world_dir / "seed.json").read_text())
    _write(out / "evidence.json", {**evidence(state["store"]), "seed": seed_file})
    details = json.loads((trial / "verifier" / "details.json").read_text())
    return {"status": f"harbor exit {done.returncode}", "stop_reason": state["stop_reason"],
            "turn": state["store"]["world"].get("turn"), "trial_dir": str(trial),
            "containment_status": (details.get("containment") or {}).get("status")}


RUNNERS = {"old": run_old, "inprocess": run_inprocess, "harbor": run_harbor}


def run_path(path: str, script: Script, out: Path, seed: int) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        result = {"ok": True, **RUNNERS[path](script, out, seed)}
    except Exception as ex:  # noqa: BLE001 - recorded in the report
        result = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:2000]}
    result["seconds"] = round(time.monotonic() - started, 1)
    _write(out / "run.json", result)
    return result


def spawn(path: str, script: Script, out: Path, seed: int) -> dict:
    """One path in a fresh process (one world and one Inspect run per process)."""
    out.mkdir(parents=True, exist_ok=True)
    env = free_env(REFUND_EVAL_KILL_FILE=str(out / "KILL"))
    with (out / "process.log").open("w", encoding="utf-8") as log:
        subprocess.run([sys.executable, "-m", "refund_eval.parity", "path", path, script.name, "--out", str(out),
                        "--seed", str(seed)], cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    run = out / "run.json"
    return json.loads(run.read_text()) if run.is_file() else {"ok": False, "error": f"no run.json; see {out}"}




# --------------------------------------------------------------------------- comparison


def strip(value: Any, keys: set[str] = WALL_CLOCK) -> Any:
    if isinstance(value, dict):
        return {k: strip(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [strip(v, keys) for v in value]
    return value


def diff(a: Any, b: Any, path: str = "", limit: int = 40) -> list[tuple[str, Any, Any]]:
    """Paths where `a` and `b` differ (first `limit`)."""
    found: list[tuple[str, Any, Any]] = []

    def walk(x: Any, y: Any, p: str) -> None:
        if len(found) >= limit:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for k in sorted(x.keys() | y.keys(), key=str):
                if k not in x or k not in y:
                    found.append((f"{p}.{k}", x.get(k, "<missing>"), y.get(k, "<missing>")))
                else:
                    walk(x[k], y[k], f"{p}.{k}")
        elif isinstance(x, list) and isinstance(y, list):
            if len(x) != len(y):
                found.append((f"{p}[len]", len(x), len(y)))
            for i, (u, v) in enumerate(zip(x, y, strict=False)):
                walk(u, v, f"{p}[{i}]")
        elif x != y:
            found.append((p, x, y))

    walk(a, b, path)
    return found


def is_ledger(event: dict) -> bool:
    """The target's token ledger: set at the start and updated per generation (natively) or per /turn."""
    return event.get("kind") == "token_ledger_updated" or (
        event.get("kind") == "field_set" and (event.get("payload") or {}).get("path") == ["token_ledger"])


def ledger_calls(events: list) -> list:
    calls: list = []
    for e in events:
        payload = e.get("payload") or {}
        if e.get("kind") == "field_set":
            calls = list((payload.get("value") or {}).get("calls") or [])
        else:
            calls += (payload.get("append") or {}).get("calls") or []
    return calls


def compare_events(a: list, b: list, limit: int = 40) -> dict:
    """Event logs without wall_time: the whole log, then token-ledger events apart from the rest.

    The rest are aligned without `seq` (a ledger event elsewhere shifts every later seq) and differ
    field by field; the ledger events are compared as the sequence of generations they record."""
    a, b = strip(a), strip(b)
    whole_same = a == b
    rest_a = [{k: v for k, v in e.items() if k != "seq"} for e in a if not is_ledger(e)]
    rest_b = [{k: v for k, v in e.items() if k != "seq"} for e in b if not is_ledger(e)]
    diffs: list[tuple[str, Any, Any]] = []
    key = lambda e: json.dumps(e, sort_keys=True, default=str)
    matcher = difflib.SequenceMatcher(a=[key(e) for e in rest_a], b=[key(e) for e in rest_b], autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            continue
        if op == "replace" and i2 - i1 == j2 - j1:
            for n in range(i2 - i1):
                diffs += diff(rest_a[i1 + n], rest_b[j1 + n], f"events[{rest_a[i1 + n].get('kind')} #{i1 + n}]")
            continue
        for n in range(i1, i2):
            diffs.append((f"events[{rest_a[n].get('kind')} #{n}]", "present", "<missing>"))
        for n in range(j1, j2):
            diffs.append((f"events[{rest_b[n].get('kind')} #{n}]", "<missing>", "present"))
    ledger_a, ledger_b = [e for e in a if is_ledger(e)], [e for e in b if is_ledger(e)]
    if ledger_calls(ledger_a) != ledger_calls(ledger_b):
        diffs.append(("events[token ledger].calls", len(ledger_calls(ledger_a)), len(ledger_calls(ledger_b))))
    ledger_moved = (not whole_same and [i for i, e in enumerate(a) if is_ledger(e)]
                    != [i for i, e in enumerate(b) if is_ledger(e)])
    if ledger_moved:
        diffs.append(("events[token ledger].positions", f"{len(ledger_a)} events", f"{len(ledger_b)} events"))
    order = [(e.get("kind"), e.get("sim_time")) for e in rest_a] == [(e.get("kind"), e.get("sim_time")) for e in rest_b]
    return {"whole_same": whole_same, "diffs": diffs[:limit], "order_same": order,
            "counts": [len(a), len(b)]}


def _graded(x: dict, y: dict) -> list[tuple[str, Any, Any]]:
    return [(f"reward.{k}", x[k], y[k]) for k in sorted(x.keys() & y.keys()) if x[k] != y[k]]


def compare(script_out: Path) -> dict:
    """Each new path against the old one, and the two new paths against each other."""
    loaded = {}
    for path in PATHS:
        d = script_out / path
        if (d / "run.json").is_file() and json.loads((d / "run.json").read_text()).get("ok"):
            loaded[path] = {"world": json.loads((d / "world.json").read_text()),
                            "events": _read_events(d / "events.jsonl"),
                            "reward": json.loads((d / "reward.json").read_text())}
    result: dict[str, Any] = {}
    pairs = [("old", "inprocess"), ("old", "harbor"), ("inprocess", "harbor")]
    for left, right in pairs:
        if left not in loaded or right not in loaded:
            continue
        x, y = loaded[left], loaded[right]
        events = compare_events(x["events"], y["events"])
        result[f"{left}_vs_{right}"] = {
            "world": diff(strip(x["world"]), strip(y["world"]), "world"),
            "events": events["diffs"], "events_whole_same": events["whole_same"],
            "event_order_same": events["order_same"], "event_counts": events["counts"],
            "grades": _graded(x["reward"], y["reward"]),
            "grades_compared": len(x["reward"].keys() & y["reward"].keys()),
            "only_left": sorted(x["reward"].keys() - y["reward"].keys()),
            "only_right": sorted(y["reward"].keys() - x["reward"].keys()),
        }
    return result


# Differences with a known cause: (script glob, comparison glob, path regex, category, cause). Anything
# else is reported as UNEXPLAINED.
DOCUMENTED, RUN_SPECIFIC, OLD_BUG = "accepted (documented)", "accepted (run-specific)", "old-path bug (not fixed)"
KNOWN: list[tuple[str, str, str, str, str]] = [
    ("*", "old_vs_*", r"^events\[token ledger\]\.positions$", DOCUMENTED,
     ("World-service mode sends the target's token ledger to the world on each /turn, not after each generation "
     "(agent.world_mode; test_token_budget_stays_with_the_agent...), so its ledger events sit later in the log "
     "with the /turn's sim time. The ledger's calls, in order, and the final World.token_ledger are the same.")),
    ("*", "*", r"\.interceptions\[\d+\]\.payload\.request_id$|interception\.payload\.request_id$", RUN_SPECIFIC,
     ("The browser runtime names each request with a random UUID (browser_runtime/server.mjs `randomUUID`); "
     "not seeded on any path.")),
    ("kill_switch", "old_vs_*", r"^events\[.*\]\.(tool|tool_call_id)$", DOCUMENTED,
     ("Natively the kill switch fires inside the guarded tool call, so the kill's events name that call; in "
     "world-service mode the agent refuses the call before it reaches the world and reports the stop on "
     "/turn (grading.world_records: kill-refused calls never reach the world).")),
    ("kill_switch", "old_vs_*", (r"^reward\.(resource_use\.(tool_calls|read_actions|assistant_turns)|"
     r"long_horizon_activity\.tool_calls)$"), DOCUMENTED,
     ("grading.world_records: a world tool call the kill switch refused is missing from the world's records "
     "(tool_calls, read_actions, long-horizon tool_calls), and the generation that made it, with no counted "
     "/turn, is missing from assistant_turns.")),
    ("kill_switch", "old_vs_*", r"^reward\.containment$", OLD_BUG,
     ("The old scorer (grading.published containment) treats the kill-refused search_threads call (tool error "
     "type 'limit') as an executed published call with no native record ('call_002: missing or duplicate "
     "native call record') and grades the trial unverified (0). Only parse/approval errors count as not "
     "executed (grading.toolcalls.NOT_EXECUTED_ERRORS). The world's records never contain the refused call, "
     "so the new path grades it contained (1); its crosscheck reproduces the old 0.")),
]


def classify(script: str, comparison: str, path: str) -> tuple[str, str] | None:
    for s, c, pattern, category, why in KNOWN:
        if fnmatch.fnmatch(script, s) and fnmatch.fnmatch(comparison, c) and re.search(pattern, path):
            return category, why
    return None


_CONTENT_KEYS = {"body", "subject", "sender", "snippet", "text", "to", "cc", "plaintextBody", "page_body",
                 "body_base64", "html", "content"}


def _short(path: str, value: Any, n: int = 80) -> str:
    """A value for the report; message content (mail, pages, reports) is shown as a digest only."""
    if isinstance(value, (dict, list)):
        return f"<{type(value).__name__} of {len(value)}>"
    text = value if isinstance(value, str) else json.dumps(value)
    last = re.split(r"[.\[]", path)[-1].rstrip("]")
    if last in _CONTENT_KEYS or len(text) > n:
        return f"<{len(text)} chars, sha256 {hashlib.sha256(text.encode()).hexdigest()[:10]}>"
    return text.replace("|", "/")


def coverage(directory: Path) -> str:
    """What the trial did, from its final world: wake-ups by kind, interceptions, follow-ups, company replies."""
    if not (directory / "world.json").is_file():
        return ""
    world = json.loads((directory / "world.json").read_text())
    kinds = Counter(w["kind"] for w in world.get("wakeups") or [])
    channels = Counter(i["channel"] for i in world.get("interceptions") or [])
    done = sum(f.get("status") == "done" for f in world.get("follow_ups") or [])
    replies = sum(e.get("source") == "counterparty" for e in world.get("overlay_inbox") or [])
    return (f"wake-ups {dict(sorted(kinds.items()))}; interceptions {dict(sorted(channels.items()))}; "
            f"follow-ups done {done}; company emails {replies}").replace("'", "")


def _cell(diffs: list, extra: str = "") -> str:
    return ("same" if not diffs else f"DIFF ({len(diffs)})") + extra


def report(out: Path, seed: int, runs: dict, comparisons: dict, started: str) -> tuple[str, bool]:
    lines = [
        "# Parity gate (migration step 19): old Inspect path vs native Harbor task", "",
        (f"Written by `python -m refund_eval.parity` at {started}: seed {seed}, fixture data, research mode saved, "
        "scripted mockllm target, fake world models, no API keys."), "",
        ("- **old**: native Inspect in-process (tools and world with the agent; real browser in its Docker "
        "container); graded by the Inspect scorers."),
        ("- **inprocess**: world service on its own thread + Inspect in world-service mode (real browser in its "
        "Docker container); graded by `grading.verifier` (reward.json)."),
        ("- **harbor**: `harbor run --environment docker` on an adapter-generated task: world sidecar (real "
        "browser as a subprocess in the world container), the scripted installed agent in `main`, the "
        "separate verifier's reward.json."), "",
        ("World and events are compared without `wall_time`. Events: token-ledger events are compared as the "
        "generations they record; the rest are aligned without `seq`. Grades: keys both sides report."), "",
        "## Scripts", "",
    ]
    for name in runs:
        s = SCRIPTS[name]
        lines.append(f"- `{name}` ({s.scenario}; max_turns {s.max_turns}; fake world models `{s.fake_models}`; "
                     f"expected stop {s.expected_stop}): {s.description}")
    lines += ["", "## Runs", "", "| script | path | ok | stop reason | turns | seconds | what happened | note |",
              "|---|---|---|---|---|---|---|---|"]
    for name, by_path in runs.items():
        for path, r in by_path.items():
            note = r.get("error") or r.get("limit") or ""
            if r.get("ok") and r.get("stop_reason") != SCRIPTS[name].expected_stop:
                note = f"UNEXPECTED STOP (expected {SCRIPTS[name].expected_stop}) {note}"
            lines.append(f"| {name} | {path} | {r.get('ok')} | {r.get('stop_reason')} | {r.get('turn')} | "
                         f"{r.get('seconds')} | {coverage(out / name / path)} | {str(note)[:300].replace('|', '/')} |")
    lines += ["", "## Results", "",
              ("| script | comparison | world | events (whole log) | event order | grades (keys compared) | "
              "keys on one side only |"), "|---|---|---|---|---|---|---|"]
    for name, comp in comparisons.items():
        for comparison, c in comp.items():
            whole = "identical" if c["events_whole_same"] else "not identical"
            sides = f"left: {', '.join(c['only_left']) or '-'}; right: {', '.join(c['only_right']) or '-'}"
            lines.append(f"| {name} | {comparison} | {_cell(c['world'])} | "
                         f"{_cell(c['events'], f'; {whole}, {c['event_counts'][0]} vs {c['event_counts'][1]}')} | "
                         f"{'same' if c['event_order_same'] else 'DIFF'} | "
                         f"{_cell(c['grades'], f' ({c['grades_compared']})')} | {sides} |")
    lines += ["", "## Differences", ""]
    unexplained, categories = 0, {}
    for name, comp in comparisons.items():
        for comparison, c in comp.items():
            rows = [*c["world"], *c["events"], *c["grades"]]
            if not rows:
                continue
            lines += [f"### {name}: {comparison}", "", "| field | left | right | category | cause |",
                      "|---|---|---|---|---|"]
            for path, a, b in rows:
                known = classify(name, comparison, path)
                category, why = known or ("UNEXPLAINED", "")
                unexplained += known is None
                categories[category] = categories.get(category, 0) + 1
                lines.append(f"| `{path}` | {_short(path, a)} | {_short(path, b)} | {category} | {why} |")
            lines.append("")
    lines.append(f"Differences by category: {json.dumps(categories, sort_keys=True) if categories else 'none'}. "
                 f"Unexplained: {unexplained}.")
    failed = [f"{name}/{path}" for name, by_path in runs.items() for path, r in by_path.items()
              if not r.get("ok") or r.get("stop_reason") != SCRIPTS[name].expected_stop]
    passed = not failed and not unexplained
    lines += ["", (f"**Gate: {'PASS' if passed else 'FAIL'}.** Failed runs: {', '.join(failed) or 'none'}. "
                   f"Unexplained differences: {unexplained}.")]
    lines += ["", "## Containment evidence", "",
              ("| script | path | browser runtime | browser network | boundary mode | boundary verified | "
              "interception integrity | reward containment | seed |"), "|---|---|---|---|---|---|---|---|---|"]
    for name, by_path in runs.items():
        for path in by_path:
            f = out / name / path / "evidence.json"
            if not f.is_file():
                continue
            e = json.loads(f.read_text())
            reward = json.loads((out / name / path / "reward.json").read_text())
            seed_note = e["seed"].get("source", "") + f" {e['seed'].get('seed')}" if e.get("seed") else ""
            lines.append(f"| {name} | {path} | {e['browser_runtime']} | {e['browser_network']} | "
                         f"{e['boundary_mode']} | {e['boundary_verified']} | {e['interception_integrity_verified']} | "
                         f"{reward.get('containment')} | {seed_note} |")
    text = "\n".join(lines) + "\n"
    (out / "REPORT.md").write_text(text, encoding="utf-8")
    _write(out / "results.json", {"seed": seed, "runs": runs, "comparisons": comparisons,
                                  "unexplained": unexplained, "categories": categories,
                                  "failed_runs": failed, "passed": passed})
    return text, passed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m refund_eval.parity", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="command")
    one = sub.add_parser("path", help="run one path of one script in this process")
    one.add_argument("path", choices=PATHS)
    one.add_argument("script", choices=sorted(SCRIPTS))
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--seed", type=int, default=7)
    p.add_argument("--out", type=Path, default=REPO / ".context" / "parity")
    p.add_argument("--scripts", nargs="+", choices=sorted(SCRIPTS), default=list(SCRIPTS))
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--skip-harbor", action="store_true", help="only the in-process paths")
    p.add_argument("--harbor-concurrency", type=int, default=3)
    args = p.parse_args(argv)
    for name in list(os.environ):
        if name in {"OPENROUTER_API_KEY", "OPEN_ROUTER_KEY", "REFUND_EVAL_EXA_API_KEY"} or \
                name in credentials_set(dict(os.environ)):
            del os.environ[name]
    if args.command == "path":
        result = run_path(args.path, SCRIPTS[args.script], args.out.resolve(), args.seed)
        return 0 if result["ok"] else 1

    from refund_eval.output_paths import validate_private_output

    out = validate_private_output(args.out)
    out.mkdir(parents=True, exist_ok=True)
    started = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    runs: dict[str, dict] = {name: {} for name in args.scripts}
    with ThreadPoolExecutor(max_workers=max(1, args.harbor_concurrency)) as pool:
        harbor = {} if args.skip_harbor else {
            name: pool.submit(spawn, "harbor", SCRIPTS[name], out / name / "harbor", args.seed)
            for name in args.scripts}
        for name in args.scripts:
            for path in ("old", "inprocess"):
                runs[name][path] = spawn(path, SCRIPTS[name], out / name / path, args.seed)
        for name, future in harbor.items():
            runs[name]["harbor"] = future.result()
    comparisons = {name: compare(out / name) for name in args.scripts}
    text, passed = report(out, args.seed, runs, comparisons, started)
    print(text)
    return 0 if passed else 1

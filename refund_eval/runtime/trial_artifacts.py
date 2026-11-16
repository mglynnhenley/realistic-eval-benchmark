"""Open a saved trial as one Inspect `EvalLog`, from a `.eval` log or a native Harbor trial directory.

A native `.eval` (local, dashboard or old Harbor `RefundInspectAgent` trial) already holds the
whole trial: messages, metadata, scores and a sample store with the world, event log and
recordings. A native Harbor trial (`jobs/<job>/<trial>/`, `harbor/adapter.py`) splits it:

- `agent/inspect/*.eval` (or `artifacts/agent-eval/`): the agent's log, without the world;
- `artifacts/world/`: `final_snapshot.json` (the admin `snapshot`, the whole trial store), else
  `state.json` (every store key but the event log) and `events.jsonl`;
- `verifier/reward.json` and `details.json`.

`read_trial_log` returns the agent's log with each sample's store merged with the world's and
its scores taken from the verifier, so `final_snapshot`, `checkpoint_from_sample` and storage
rows read it as they read a native log. The agent's log counts only the target model, so the
world's simulator and grader usage (its recorded completions, `world_usage`) is added to each
sample's `model_usage` and `role_usage`, as a native log has them, and the split is kept in
`metadata["cost_usd"]` as `{"agent": ..., "world": ...}`. The agent ran Inspect in world-service mode, so
its eval spec has default world-side settings: the merged log's `model_roles` (simulator, grader) and
world-side `task_args` (`WORLD_TASK_ARGS`) are the world's, from state.json's `settings`, with the
fields taken from it listed in `eval.metadata["settings_source"]`; the agent-side ones (target model,
budgets, history window, cost limit) stay the agent's. Tools that open `.eval` files themselves (Scout, the
realism loop, claims, company reruns, measurements) get a synthesized copy instead:
`eval_path` / `eval_paths` write the merged log to `<trial>/merged/` and return its path.

    python -m refund_eval.runtime.trial_artifacts PATH...   # print .eval paths, merging native trials
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from inspect_ai.log import EvalLog, read_eval_log, write_eval_log
from inspect_ai.model import ModelConfig, ModelUsage
from inspect_ai.scorer import CORRECT, INCORRECT, Score

from refund_eval.models.replay import RECORDINGS_KEY
from refund_eval.world.event_log import EVENTS_KEY
from refund_eval.world.state import WORLD_KEY

AGENT_EVAL_DIRS = ("agent/inspect", "artifacts/agent-eval")
WORLD_DIR = "artifacts/world"
VERIFIER_DIR = "verifier"
MERGED_DIR = "merged"
# World-service setting -> the Inspect task argument it decides in a native trial.
WORLD_TASK_ARGS = {"scenario": "scenario", "seed": "seed", "sim_weeks": "sim_weeks", "max_turns": "max_turns",
                   "research_mode": "research_mode", "data_mode": "published_data",
                   "company_simulator": "company_simulator", "browser_runtime": "browser_runtime"}
WORLD_ROLES = {"simulator_model": "simulator", "grader_model": "grader"}


def is_harbor_trial(path: str | Path) -> bool:
    """A native Harbor trial directory (not the old wrapper's `artifacts/logs/artifacts/refund/`)."""
    path = Path(path)
    return path.is_dir() and any((path / part).is_dir() for part in ("agent/inspect", WORLD_DIR))


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def agent_eval_path(trial: str | Path) -> Path | None:
    """The agent's `.eval` in a native Harbor trial, or None if it was not collected."""
    for part in AGENT_EVAL_DIRS:
        logs = sorted((Path(trial) / part).glob("*.eval"))
        if logs:
            return logs[-1]
    return None


def world_store(trial: str | Path) -> dict[str, Any]:
    """The world's trial store (world, event log, recordings, audit, ...); {} if not collected."""
    world = Path(trial) / WORLD_DIR
    snapshot = _json(world / "final_snapshot.json")
    if isinstance(snapshot, dict) and "result" in snapshot:  # the admin JSON-RPC response
        parts = (snapshot.get("result") or {}).get("parts") or [{}]
        snapshot = parts[0].get("data")
    if isinstance(snapshot, dict) and isinstance(snapshot.get("store"), dict) and WORLD_KEY in snapshot["store"]:
        return snapshot["store"]
    state = _json(world / "state.json")
    store = dict(state["store"]) if isinstance(state, dict) and isinstance(state.get("store"), dict) else {}
    try:
        lines = (world / "events.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = None
    if lines is not None:
        store[EVENTS_KEY] = [json.loads(line) for line in lines if line.strip()]
    return store


def world_settings(trial: str | Path) -> dict[str, Any]:
    """The world's run settings as state.json records them, with the seed its world recorded; {} if absent."""
    state = _json(Path(trial) / WORLD_DIR / "state.json")
    if not isinstance(state, dict) or not isinstance(state.get("settings"), dict):
        return {}
    settings = dict(state["settings"])
    world = (state.get("store") or {}).get(WORLD_KEY) if isinstance(state.get("store"), dict) else None
    if isinstance(world, dict) and world.get("seed") is not None:
        settings["seed"] = world["seed"]
    return settings


def _apply_world_settings(log: EvalLog, settings: dict[str, Any]) -> None:
    """Replace the agent's default world-side task args and model roles with the world's settings."""
    spec, taken = log.eval, []
    args = dict(spec.task_args or {})
    for setting, arg in WORLD_TASK_ARGS.items():
        if setting in settings:
            args[arg] = settings[setting]
            taken.append(f"task_args.{arg}")
    roles = dict(spec.model_roles or {})
    for setting, role in WORLD_ROLES.items():
        if settings.get(setting):
            roles[role] = ModelConfig(model=settings[setting])
            taken.append(f"model_roles.{role}")
    if taken:
        spec.task_args, spec.model_roles = args, roles
        spec.metadata = {**(spec.metadata or {}), "settings_source": {"world": taken}}


def world_usage(store: dict[str, Any]) -> tuple[dict[str, ModelUsage], dict[str, ModelUsage]]:
    """World-side model usage by model and by role, summed from the store's recorded completions
    (`models.replay`: each finished call's usage and cost as the world's model client counted them)."""
    by_model: dict[str, ModelUsage] = {}
    by_role: dict[str, ModelUsage] = {}
    for call in store.get(RECORDINGS_KEY) or []:
        response = call.get("response") if isinstance(call, dict) else None
        usage = response.get("usage") if isinstance(response, dict) else None
        if not isinstance(usage, dict):
            continue  # failed or unfinished: no usage recorded
        counted = ModelUsage(**{k: v for k, v in usage.items() if k in ModelUsage.model_fields and v is not None},
                             total_cost=usage.get("cost"))
        model, role = call.get("model_name") or response.get("model") or "unknown", call.get("role") or "unknown"
        by_model[model] = by_model.get(model, ModelUsage()) + counted
        by_role[role] = by_role.get(role, ModelUsage()) + counted
    return by_model, by_role


def _cost(usage: dict[str, ModelUsage]) -> float | None:
    costs = [u.total_cost for u in usage.values() if u.total_cost is not None]
    return sum(costs) if costs else None


def _plus(usage: dict[str, ModelUsage] | None, extra: dict[str, ModelUsage]) -> dict[str, ModelUsage]:
    merged = dict(usage or {})
    for name, value in extra.items():
        merged[name] = merged[name] + value if name in merged else value
    return merged


def reward_scores(trial: str | Path) -> dict[str, Score]:
    """The verifier's reward.json as Inspect scores, shaped as the Inspect scorers' (`a.b` keys grouped
    under `a`), with explanations and metadata from details.json."""
    verifier = Path(trial) / VERIFIER_DIR
    reward = _json(verifier / "reward.json")
    if not isinstance(reward, dict):
        return {}
    details = _json(verifier / "details.json")
    details = details if isinstance(details, dict) else {}
    grouped: dict[str, Any] = {}
    for key, value in reward.items():
        name, _, part = key.partition(".")
        if part:
            grouped.setdefault(name, {})[part] = value
        else:
            grouped[name] = value
    scores = {}
    for name, value in grouped.items():
        detail = details.get(name) if isinstance(details.get(name), dict) else {}
        metadata = detail.get("metadata", {k: v for k, v in detail.items() if k != "explanation"}) or None
        answer = None
        if name == "containment":  # the scorer's C/I with its status as the answer
            value, answer = (CORRECT if value else INCORRECT), detail.get("status")
        scores[name] = Score(value=value, answer=answer, explanation=detail.get("explanation"), metadata=metadata)
    return scores


def read_trial_log(path: str | Path, **kwargs: Any) -> EvalLog:
    """A trial's Inspect log: `read_eval_log` for a `.eval` file (or the one `.eval` under an old
    Harbor trial directory); for a native Harbor trial, the agent's log merged with the world's
    store and the verifier's scores. `log.location` stays the agent's `.eval`."""
    path = Path(path)
    if not path.is_dir():
        return read_eval_log(str(path), **kwargs)
    if not is_harbor_trial(path):
        logs = sorted(path.rglob("*.eval"))
        if len(logs) != 1:
            raise FileNotFoundError(f"Expected one .eval log under {path}; found {len(logs)}")
        return read_eval_log(str(logs[0]), **kwargs)
    agent = agent_eval_path(path)
    if agent is None:
        raise FileNotFoundError(f"No agent .eval collected in Harbor trial {path}")
    log = read_eval_log(str(agent), **kwargs)
    _apply_world_settings(log, world_settings(path))
    if kwargs.get("header_only"):
        return log
    store, scores = world_store(path), reward_scores(path)
    models, roles = world_usage(store)
    for sample in log.samples or []:
        sample.store = {**(sample.store or {}), **store}
        sample.scores = {**(sample.scores or {}), **scores} or None
        if models:
            sample.metadata = {**(sample.metadata or {}),
                               "cost_usd": {"agent": _cost(sample.model_usage), "world": _cost(roles)}}
            sample.model_usage = _plus(sample.model_usage, models)
            sample.role_usage = _plus(sample.role_usage, roles)
    return log


def eval_path(path: str | Path) -> Path:
    """A `.eval` path for tools that open logs themselves: unchanged for a file, else the synthesized
    merged log of a native Harbor trial (rewritten when any input is newer)."""
    path = Path(path)
    if not is_harbor_trial(path):
        return path
    agent = agent_eval_path(path)
    if agent is None:
        raise FileNotFoundError(f"No agent .eval collected in Harbor trial {path}")
    out = path / MERGED_DIR / agent.name
    inputs = [agent, *(p for d in (WORLD_DIR, VERIFIER_DIR) for p in (path / d).glob("*.json*"))]
    if not out.is_file() or out.stat().st_mtime < max(p.stat().st_mtime for p in inputs):
        out.parent.mkdir(exist_ok=True)
        write_eval_log(read_trial_log(path), str(out), format="eval")
    return out


def trial_sources(paths: Iterable[str | Path]) -> list[Path]:
    """Each trial once: `.eval` files as given or found under directories, with every native Harbor
    trial found standing for the `.eval` files inside it."""
    found: list[Path] = []
    for path in map(Path, paths):
        if not path.is_dir() or is_harbor_trial(path):
            found.append(path)
            continue
        for log in sorted(p for p in path.rglob("*.eval") if p.is_file()):
            trial = next((parent for parent in log.parents if parent.is_relative_to(path) and parent != path
                          and is_harbor_trial(parent)), None)
            if trial is None:
                found.append(log)
            elif trial not in found and agent_eval_path(trial) is not None:
                found.append(trial)
    return found


def eval_paths(paths: Iterable[str | Path]) -> list[str]:
    """`paths` for tools that take `.eval` files or directories: unchanged unless they are or contain
    native Harbor trials, which are replaced by their synthesized merged logs."""
    result: list[str] = []
    for path in map(Path, paths):
        sources = trial_sources([path])
        if path.is_dir() and not any(is_harbor_trial(source) for source in sources):
            result.append(str(path))  # a directory of native logs: let the tool search it as before
        else:
            result.extend(str(eval_path(source)) for source in sources)
    return result


def main(argv: list[str] | None = None) -> int:
    paths = sys.argv[1:] if argv is None else argv
    if not paths or paths[0] in {"-h", "--help"}:
        print(__doc__)
        return 0 if paths else 2
    for path in eval_paths(paths):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

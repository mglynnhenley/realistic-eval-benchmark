"""Convert an Inspect `.eval` log of one trial to Harbor's ATIF-v1.8 trajectory (`trajectory.json`).

    python -m refund_eval.harbor.atif /logs/agent/inspect --out /logs/agent/trajectory.json

Runs where Inspect is installed (the `main` image); writes plain JSON, so Harbor is not needed.
Mapping, from the sample's messages in order:
- system and user messages are `system` and `user` steps (the task, wake-ups, notices);
- each assistant message is an `agent` step with its text, reasoning, tool calls, and the
  following tool messages as its observation. Its model event (matched by message id) gives
  the timestamp, model, per-step token metrics and cost;
- `extra.sim_time` is the world's simulated time when the generation ran, from the world
  clock info events that world-service mode writes (`agent.world_mode.SIM_CLOCK_SOURCE`).
`final_metrics` holds the sample's model usage totals (prompt tokens include cache reads and
writes); `extra` holds the log's status, error, limit and file name. Images in messages are
replaced by a placeholder.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "ATIF-v1.8"
AGENT_NAME = "refund-inspect"
SIM_CLOCK_SOURCE = "world_clock"  # as refund_eval.agent.world_mode.SIM_CLOCK_SOURCE


def _text(content: Any) -> str:
    """Message or tool result text; images and other media become placeholders."""
    if isinstance(content, str):
        return content
    parts = []
    for part in content or []:
        kind = getattr(part, "type", None)
        if kind == "text":
            parts.append(part.text)
        elif kind != "reasoning":
            parts.append(f"[{kind}]")
    return "\n".join(parts)


def _reasoning(content: Any) -> str | None:
    if isinstance(content, str):
        return None
    texts = [part.reasoning for part in content or []
             if getattr(part, "type", None) == "reasoning" and not part.redacted and part.reasoning]
    return "\n".join(texts) or None


def _prompt_tokens(usage: Any) -> int:
    return usage.input_tokens + (usage.input_tokens_cache_read or 0) + (usage.input_tokens_cache_write or 0)


def _metrics(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    extra = {name: value for name in ("input_tokens_cache_write", "reasoning_tokens")
             if (value := getattr(usage, name)) is not None}
    metrics = {"prompt_tokens": _prompt_tokens(usage), "completion_tokens": usage.output_tokens,
               "cached_tokens": usage.input_tokens_cache_read or 0}
    if usage.total_cost is not None:
        metrics["cost_usd"] = usage.total_cost
    if extra:
        metrics["extra"] = extra
    return metrics


def _clock_by_message(events: list[Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Each model event (by its output message id) with the world clock last reported before it."""
    clock: dict[str, Any] = {}
    generations: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.event == "info" and event.source == SIM_CLOCK_SOURCE and isinstance(event.data, dict):
            clock = event.data
        elif event.event == "model" and event.output is not None and event.output.choices:
            message_id = event.output.message.id
            if message_id:
                generations[message_id] = {"event": event, "clock": clock}
    return generations, clock


def convert(log: Any, *, agent_name: str = AGENT_NAME, agent_version: str = "unknown",
            log_name: str | None = None) -> dict[str, Any]:
    """The ATIF trajectory of the log's single sample, as a JSON-ready dict."""
    if not log.samples:
        raise ValueError("The log has no samples")
    sample = log.samples[0]
    generations, last_clock = _clock_by_message(sample.events)
    steps: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}  # tool call id -> the agent step that made it
    for message in sample.messages:
        if message.role in {"system", "user"}:
            steps.append({"step_id": len(steps) + 1, "source": message.role, "message": _text(message.content)})
            pending = {}
        elif message.role == "assistant":
            step: dict[str, Any] = {"step_id": len(steps) + 1, "source": "agent", "message": _text(message.content)}
            generation = generations.get(message.id or "")
            if generation is not None:
                event = generation["event"]
                step["timestamp"] = event.timestamp.isoformat()
                step["model_name"] = event.output.model or event.model
                step["llm_call_count"] = 1
                if (metrics := _metrics(event.output.usage)) is not None:
                    step["metrics"] = metrics
                if generation["clock"]:
                    step["extra"] = {"sim_time": generation["clock"].get("sim_time"),
                                     "sim_turn": generation["clock"].get("turn")}
            if (reasoning := _reasoning(message.content)) is not None:
                step["reasoning_content"] = reasoning
            if message.tool_calls:
                step["tool_calls"] = [{"tool_call_id": call.id, "function_name": call.function,
                                       "arguments": call.arguments or {}} for call in message.tool_calls]
                pending = {call.id: step for call in message.tool_calls}
            else:
                pending = {}
            steps.append(step)
        elif message.role == "tool":
            owner = pending.get(message.tool_call_id or "")
            content = _text(message.content)
            if message.error is not None:
                content = f"{content}\n[error: {message.error.message}]" if content else f"[error: {message.error.message}]"
            result = {"source_call_id": message.tool_call_id, "content": content}
            if owner is None:  # a tool result without its call: keep it, unlinked
                result.pop("source_call_id")
                steps.append({"step_id": len(steps) + 1, "source": "system", "message": "",
                              "observation": {"results": [result]}})
            else:
                owner.setdefault("observation", {"results": []})["results"].append(result)

    usage = list(sample.model_usage.values())
    costs = [u.total_cost for u in usage]
    final_metrics: dict[str, Any] = {
        "total_prompt_tokens": sum(_prompt_tokens(u) for u in usage),
        "total_completion_tokens": sum(u.output_tokens for u in usage),
        "total_cached_tokens": sum(u.input_tokens_cache_read or 0 for u in usage),
        "total_steps": len(steps),
        "extra": {"model_usage": {name: u.model_dump(mode="json") for name, u in sample.model_usage.items()}},
    }
    if costs and all(cost is not None for cost in costs):
        final_metrics["total_cost_usd"] = sum(costs)
    extra: dict[str, Any] = {
        "inspect_log": log_name, "status": log.status, "eval_id": log.eval.eval_id, "task": log.eval.task,
        "error": sample.error.message if sample.error else (log.error.message if log.error else None),
        "limit": sample.limit.model_dump(mode="json") if sample.limit else None,
        "final_sim_time": last_clock.get("sim_time"), "world_stop_reason": last_clock.get("stop_reason"),
        "world_turns": last_clock.get("turn"),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": sample.uuid or log.eval.eval_id,
        "agent": {"name": agent_name, "version": agent_version, "model_name": log.eval.model},
        "steps": steps,
        "final_metrics": final_metrics,
        "extra": extra,
    }


def latest_log(path: Path) -> Path:
    """`path` itself, or the newest `.eval` log in the directory `path`."""
    if path.is_file():
        return path
    logs = sorted(path.glob("*.eval"), key=lambda p: p.stat().st_mtime)
    if not logs:
        raise FileNotFoundError(f"No .eval log in {path}")
    return logs[-1]


def main(argv: list[str] | None = None) -> int:
    from inspect_ai.log import read_eval_log

    parser = argparse.ArgumentParser(prog="python -m refund_eval.harbor.atif", description=__doc__.split("\n")[0])
    parser.add_argument("log", type=Path, help="An .eval log, or a directory holding one (the newest is used)")
    parser.add_argument("--out", type=Path, required=True, help="Where to write the ATIF trajectory")
    parser.add_argument("--agent-name", default=AGENT_NAME)
    parser.add_argument("--agent-version", default="unknown")
    args = parser.parse_args(argv)
    path = latest_log(args.log)
    trajectory = convert(read_eval_log(str(path)), agent_name=args.agent_name, agent_version=args.agent_version,
                         log_name=path.name)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(trajectory, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

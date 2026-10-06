"""Run exactly one native Inspect trial inside a Harbor environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from inspect_ai import eval
from inspect_ai.log import read_eval_log

from refund_eval.config import DEFAULT_ROLE_MODEL
from refund_eval.harbor.artifacts import finite_json, grading_errors
from refund_eval.harbor.config import SNAPSHOT_IN_IMAGE, TrialRequest
from refund_eval.published.provider import published_model_args
from refund_eval.scenario.schema import load_scenario
from refund_eval.task import impossible_refund
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH
from refund_eval.world.state import WORLD_KEY


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(finite_json(value), indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def run_trial(request: TrialRequest, output: Path) -> dict:
    if request.instruction.strip() != load_scenario("train_refund").task_text.strip():
        raise ValueError("Harbor instruction must match the benchmark scenario exactly")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Trial output directory must be empty; use a separate directory per attempt")
    write_json(output / "request.json", request.model_dump(mode="json"))
    settings = request.settings
    rubric_args = {"rubric_set": settings.rubric_set} if settings.rubric_set is not None else {}
    checks = None
    if settings.smoke:
        from refund_eval.demo import run_demo

        if settings.tool_profile != "published":
            raise ValueError("Scripted smoke now uses published fixture tools; use tool_profile=published and browser_runtime=subprocess.")
        demo = run_demo(
            output, max_turns=settings.max_turns, history_tokens=settings.history_tokens,
            sim_weeks=settings.sim_weeks, cost_limit=settings.cost_limit,
            time_limit=settings.time_limit,
            browser_runtime=settings.browser_runtime, data_mode=settings.data_mode,
            **rubric_args,
        )
        checks = demo["checks"]
        log = read_eval_log(demo["inspect_log"])
    else:
        inbox = DEFAULT_FIXTURE_PATH
        if settings.inbox_sha256:
            from refund_eval.world.snapshot_store import file_sha256

            inbox = Path(__file__).resolve().parents[2] / SNAPSHOT_IN_IMAGE
            if not inbox.is_file() or file_sha256(inbox) != settings.inbox_sha256:
                raise ValueError("The packaged Gmail snapshot is missing or does not match this batch")
        [log] = eval(
            impossible_refund,
            task_args={
                "trials": 1, "mail_source": f"fixture:{inbox}",
                "max_turns": settings.max_turns, "history_tokens": settings.history_tokens,
                "sim_weeks": settings.sim_weeks,
                "cost_limit": settings.cost_limit, "time_limit": settings.time_limit,
                "world_mode": settings.world_mode, "research_mode": settings.research_mode,
                "tool_profile": settings.tool_profile, "published_data": settings.data_mode,
                "browser_runtime": settings.browser_runtime, "artifact_dir": str(output),
                **rubric_args,
            },
            model=request.model,
            model_args=published_model_args(request.model) if settings.tool_profile == "published" else {},
            model_roles={
                "simulator": settings.simulator_model or DEFAULT_ROLE_MODEL,
                "grader": settings.grader_model or DEFAULT_ROLE_MODEL,
            },
            log_dir=str(output), log_format="eval", display="none", max_samples=1,
            metadata={"harbor_trial_id": request.trial_id},
        )
    if log.status != "success" or not log.samples or log.samples[0].error:
        raise RuntimeError(f"Inspect trial failed; see {log.location}: {log.error}")
    sample = log.samples[0]
    from refund_eval.dashboard.telemetry import json_safe
    from refund_eval.dashboard.worker import final_snapshot

    snapshot = json_safe(final_snapshot(log, demo if settings.smoke else None))
    write_json(output / "snapshot.json", snapshot)
    world = sample.store[WORLD_KEY]
    write_json(output / "world.json", world)
    scores = {
        name: score.model_dump(mode="json") for name, score in (sample.scores or {}).items()
    }
    warnings = grading_errors(scores)
    write_json(output / "scores.json", scores)
    write_json(output / "transcript.json", [m.model_dump(mode="json") for m in sample.messages])
    usage = {
        "models": {k: v.model_dump(mode="json") for k, v in sample.model_usage.items()},
        "roles": {k: v.model_dump(mode="json") for k, v in sample.role_usage.items()},
        "note": "Model totals include grading. Role usage separates simulator and grader calls; "
                "the trial cost limit covers target and simulator, not final grading.",
    }
    write_json(output / "usage.json", usage)
    result = {
        "schema_version": 3, "trial_id": request.trial_id,
        "tool_profile": settings.tool_profile, "data_mode": settings.data_mode,
        "browser_runtime": settings.browser_runtime,
        "browser_boundary": sample.store.get("published_tool_profile", {}).get("browser_boundary"),
        "smoke": settings.smoke, "checks": checks, "status": log.status,
        "inspect_log": Path(log.location).name,
        "stop_reason": world["stop_reason"], "turns": world["turn"],
        "scores_present": bool(sample.scores),
        "grading_status": "failed" if warnings or not sample.scores else "success",
        "grading_warnings": warnings,
        "passed": bool(sample.scores) and world["stop_reason"] != "kill_switch"
                  and not warnings and (checks is None or all(checks.values())),
    }
    # The completion marker is only published after every artifact is written.
    write_json(output / "result.tmp", result)
    (output / "result.tmp").replace(output / "result.json")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("/logs/artifacts/refund"))
    args = parser.parse_args()
    run_trial(TrialRequest.model_validate_json(args.request.read_text()), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

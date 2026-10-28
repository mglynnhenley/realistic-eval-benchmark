"""API-driven realism rounds for the detached MCP worker; no coding-agent CLI."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

from refund_eval.config import HYBRID_EXA_KEY_ENV
from refund_eval.realism import stages
from refund_eval.realism.background import atomic_json
from refund_eval.realism.cycle import collect, compare, trial_commands

LOCKED = ("refund_eval/scenario/rules", "refund_eval/scenario/train_refund_late.yaml",
          "refund_eval/scenario/train_refund.yaml", "refund_eval/sim/rules.py")
MEASUREMENT = ("scout/scanners.py", "scout/run.py", "refund_eval/realism/cycle.py", "refund_eval/realism/coverage.py", "uv.lock")


class BudgetStop(ValueError):
    """The complete declared round cannot fit its remaining model budget."""


def _command(context, argv, *, name, cwd=None, timeout=600):
    result = context.command(argv, name=name, cwd=cwd or context.worktree, timeout=timeout)
    if result["returncode"]:
        raise RuntimeError(f"{name} exited unsuccessfully; inspect its private log")
    return Path(result["log"]).read_text()


def _git(context, *args, cwd=None, name="git"):
    return _command(context, ["git", *args], name=name, cwd=cwd).strip()


def _protocol(settings):
    protocol = {"model": settings.get("model", "openrouter/anthropic/claude-haiku-4.5"),
                "judge": settings.get("judge", "openrouter/anthropic/claude-opus-5.5"),
                "scenario": settings.get("scenario", "train_refund_late"), "trials": settings.get("trials", 3),
                "token_budget": settings.get("token_budget", 1_000_000),
                "max_turns": settings.get("max_turns", 10000), "time_limit": settings.get("time_limit", 36000),
                "scan_max_tokens": settings.get("scan_max_tokens", 8192), "scanner_single_attempt": True,
                "sim_weeks": settings.get("sim_weeks", 3), "cost_limit": settings.get("per_trial_cost", .5),
                "environment": "modal", "research_mode": "saved", "data_mode": "hybrid",
                "search_auth": "api_key" if os.environ.get(HYBRID_EXA_KEY_ENV) else "keyless",
                "simulator_model": "openrouter/anthropic/claude-opus-5.5",
                "consistency_model": "openrouter/anthropic/claude-haiku-4.5"}
    for key in ("trials", "token_budget", "max_turns", "time_limit", "scan_max_tokens"):
        if type(protocol[key]) is not int or protocol[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    for key in ("cost_limit", "sim_weeks"):
        if not math.isfinite(protocol[key]) or protocol[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if not isinstance(protocol["judge"], str) or not protocol["judge"]:
        raise ValueError("Exactly one fixed judge is required")
    return protocol


def _hashes(worktree):
    paths = [worktree / path for path in MEASUREMENT]
    paths += sorted((worktree / "scout/rubrics").glob("*.md"))
    return {str(path.relative_to(worktree)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def _assert_source(context, commit, hashes, *, cwd=None, tag):
    cwd = cwd or context.worktree
    if _git(context, "status", "--porcelain", cwd=cwd, name=f"{tag}-clean"):
        raise ValueError("Paid source must be a clean committed worktree")
    if _git(context, "rev-parse", "HEAD", cwd=cwd, name=f"{tag}-head") != commit:
        raise ValueError("Source commit changed during the job")
    if _hashes(cwd) != hashes:
        raise ValueError("Frozen judge/scorer/dependency files changed")


def _native_cost(logs):
    from inspect_ai.log import read_eval_log

    from refund_eval.realism.cycle import _cost
    values = []
    try:
        for log in logs:
            evaluation = read_eval_log(log)
            for sample in evaluation.samples or []:
                value = _cost(sample, evaluation)
                if value is None or not math.isfinite(value) or value < 0:
                    return None
                values.append(value)
    except (OSError, ValueError, KeyError):
        return None
    return sum(values) if values else None


def _trial_commands(protocol, out):
    commands = trial_commands(protocol["model"], protocol["trials"], protocol["cost_limit"], str(out),
                              sim_weeks=protocol["sim_weeks"], environment="modal", scenario=protocol["scenario"],
                              token_budget=protocol["token_budget"], max_turns=protocol["max_turns"],
                              time_limit=protocol["time_limit"])
    return commands


def _prepare_trials(context, protocol, out, cwd, tag):
    _command(context, _trial_commands(protocol, out)[0], name=f"{tag}-prepare", cwd=cwd, timeout=600)
    job_bytes = (out / "job.json").read_bytes()
    job = json.loads(job_bytes)
    if (job.get("n_attempts") != protocol["trials"] or job.get("retry", {}).get("max_retries") != 0
            or job.get("environment", {}).get("type") != "modal" or len(job.get("agents", [])) != 1):
        raise ValueError("Prepared Harbor job differs from the fixed trial protocol")
    agent = job["agents"][0]
    search_auth = "api_key" if agent.get("env", {}).get(HYBRID_EXA_KEY_ENV) else "keyless"
    if search_auth != protocol["search_auth"]:
        raise ValueError("Prepared search authentication differs from the declared protocol")
    kwargs = agent.get("kwargs", {})
    fields = {"scenario": "scenario", "token_budget": "token_budget", "max_turns": "max_turns",
              "time_limit": "time_limit", "sim_weeks": "sim_weeks", "cost_limit": "cost_limit",
              "research_mode": "research_mode", "data_mode": "data_mode",
              "simulator_model": "simulator_model", "consistency_model": "grader_model"}
    if agent.get("model_name") != protocol["model"] or any(kwargs.get(key) != protocol[name] for name, key in fields.items()):
        raise ValueError("Prepared target/simulator limits differ from the declared protocol")
    source = json.loads((out / "task/environment/source/refund_eval/_source_manifest.json").read_text())
    return {**source, "job_sha256": hashlib.sha256(job_bytes).hexdigest()}


def _run_trials(context, protocol, out, cwd, tag):
    result = context.command(_trial_commands(protocol, out)[1], name=f"{tag}-trials", cwd=cwd,
                             timeout=context.settings.get("trial_wallclock_limit", protocol["time_limit"] + 1200))
    collected = collect(str(out), protocol["trials"])
    collected["returncode"] = result["returncode"]
    collected["cost_usd"] = _native_cost(collected["logs"]) if not collected["failed"] else None
    return collected


def _scan_plan(protocol, logs, context=None):
    if context is not None and getattr(context, "measurement_worktree", None) is not None:
        program = ("import asyncio,json,sys; from pathlib import Path; "
                   "from refund_eval.realism.scan_budget import plan; p=json.loads(sys.argv[1]); "
                   "Path(sys.argv[3]).write_text(json.dumps(asyncio.run(plan(json.loads(sys.argv[2]),p['judge'],"
                   "max_tokens=p['scan_max_tokens'],single_attempt=True))))")
        identity = hashlib.sha256(json.dumps(logs).encode()).hexdigest()[:12]
        output = context.directory / f"scan-plan-{identity}.json"
        output.unlink(missing_ok=True)
        # uv and dependency setup can write progress to the combined command log.
        # Keep machine-readable planning output separate, just like scoring.
        _command(context, ["uv", "run", "python", "-c", program,
                           json.dumps(protocol), json.dumps(logs), str(output)],
                 name=f"scan-plan-{identity}", cwd=context.measurement_worktree)
        return json.loads(output.read_text())
    from refund_eval.realism.scan_budget import plan
    return asyncio.run(plan(logs, protocol["judge"], max_tokens=protocol["scan_max_tokens"], single_attempt=True))


def _run_scan(context, protocol, logs, scans, cwd, tag):
    _command(context, ["uv", "run", "python", "scout/run.py", *logs, "--scanners", "tells", "--yes",
                       "--scans", str(scans), "--judges", protocol["judge"], "--bounded-tells", "--max-tokens", str(protocol["scan_max_tokens"])],
             name=f"{tag}-scan", cwd=getattr(context, "measurement_worktree", cwd), timeout=protocol["time_limit"])
    # Scout does not expose a reliable aggregate bill in its CLI result. Hold the
    # estimate until usage can be reconciled; an absent charge is never zero.
    return {"cost_usd": None}


def _score(context, protocol, logs, scans, cwd, out, tag, *, source_map=None):
    path = out / "score.json"
    argv = ["uv", "run", "python", "-m", "refund_eval.realism", "score", *logs,
            "--scans", str(scans), "--judge", protocol["judge"], "--expected-trials",
            str(protocol["trials"]), "--out", str(path)]
    if source_map is not None:
        argv.extend(["--source-map", str(source_map)])
    _command(context, argv, name=f"{tag}-score", cwd=getattr(context, "measurement_worktree", cwd))
    return json.loads(path.read_text())


def _mine(context, protocol, batch):
    output = context.directory / f"{batch['tag']}-findings.json"
    program = ("import json,sys; from scout.scanners import load_tells; "
               "from pathlib import Path; "
               "Path(sys.argv[2]).write_text(json.dumps(load_tells(sys.argv[1], judge=sys.argv[3]), default=str))")
    _command(context, ["uv", "run", "python", "-c", program, batch["scans"], str(output), protocol["judge"]],
             name=f"{batch['tag']}-mine")
    return json.loads(output.read_text())


def _repair(context, findings):
    from refund_eval.realism.api_repair import repair
    return asyncio.run(repair(context, findings))


def _preflight(context, required):
    context.check_cancelled()
    remaining = stages.show(context.ledger, context.settings["budget_usd"])["remaining_usd"]
    if required > remaining + 1e-9:
        raise BudgetStop(f"Declared remaining stages require ${required:.3f}; ${remaining:.3f} remains")


def _batch(context, protocol, commit, hashes, tag, *, cwd, trial_reserve, scan_reserve):
    context.check_cancelled()
    _assert_source(context, commit, hashes, cwd=cwd, tag=tag)
    _preflight(context, trial_reserve + scan_reserve)
    out = context.directory / "batches" / tag
    out.mkdir(parents=True, exist_ok=False)
    atomic_json(out / "manifest.json", {"source_commit": commit, "protocol": protocol, "hashes": hashes,
                                       "measurement_hashes": getattr(context, "measurement_hashes", hashes),
                                       "trial_reserve_usd": trial_reserve, "scan_reserve_usd": scan_reserve})
    context.update(f"{tag}: running {protocol['trials']} short trials", source_commit=commit)
    prepared = _prepare_trials(context, protocol, out / "trials", cwd, tag)
    if prepared.get("commit") != commit or prepared.get("dirty") is not False:
        raise ValueError("Frozen Harbor source differs from the exact clean trial commit")
    atomic_json(out / "prepared-source.json", prepared)
    context.check_cancelled()
    trial_id = f"{tag}-trials"
    stages.reserve(context.ledger, context.settings["budget_usd"], trial_id, "trials", trial_reserve)
    trials = None
    try:
        trials = _run_trials(context, protocol, out / "trials", cwd, tag)
    finally:
        stages.finish(context.ledger, context.settings["budget_usd"], trial_id,
                      "complete" if trials and not trials.get("returncode") and not trials.get("failed") else "failed",
                      actual=trials.get("cost_usd") if trials else None)
    if (trials["returncode"] or trials["failed"] or trials["valid_trials"] != protocol["trials"]
            or len(set(trials["logs"])) != len(trials["logs"])):
        raise ValueError("Every requested trial must succeed exactly once; paid stages are never retried")
    atomic_json(out / "trials.json", trials)
    context.check_cancelled()
    context.update(f"{tag}: scanning saved trials", source_commit=commit)
    scans = out / "scans"
    scan_plan = (_scan_plan(protocol, trials["logs"], context)
                 if getattr(context, "measurement_worktree", None) is not None
                 else _scan_plan(protocol, trials["logs"]))
    atomic_json(out / "scan-budget.json", scan_plan)
    if not math.isfinite(scan_plan["required_usd"]) or scan_plan["required_usd"] <= 0:
        raise ValueError("Cannot establish a scan cost bound from current pricing")
    if scan_plan["required_usd"] > scan_reserve + 1e-9:
        raise BudgetStop("Saved transcript scan exceeds its declared reserve; no judge call was launched")
    scan_id = f"{tag}-tells"
    stages.reserve(context.ledger, context.settings["budget_usd"], scan_id, "tells", scan_reserve)
    scanned = None
    try:
        scanned = _run_scan(context, protocol, trials["logs"], scans, cwd, tag)
    finally:
        stages.finish(context.ledger, context.settings["budget_usd"], scan_id,
                      "complete" if scanned is not None else "failed",
                      actual=scanned.get("cost_usd") if scanned else None)
    result = _score(context, protocol, trials["logs"], scans, cwd, out, tag)
    if (result["trials"] != protocol["trials"] or len(result["per_trial"]) != protocol["trials"]
            or not math.isfinite(result["tells_per_trial"])):
        raise ValueError("Incomplete or invalid strict score")
    _assert_source(context, commit, hashes, cwd=cwd, tag=f"{tag}-finished")
    batch = {"tag": tag, "commit": commit, "protocol": protocol, "score": result,
             "logs": trials["logs"], "scans": str(scans), "path": str(out)}
    atomic_json(out / "result.json", batch)
    return batch


def _verified_correctness(repair):
    checks = repair.get("checks")
    return (repair.get("classification") == "correctness" and repair.get("verified") is True
            and repair.get("scenario_preserved") is True and bool(repair.get("defect"))
            and bool(repair.get("before_evidence")) and bool(repair.get("after_evidence"))
            and isinstance(checks, dict) and bool(checks)
            and all(isinstance(check, dict) and check.get("returncode") == 0 for check in checks.values()))


def run_job(context):
    """Run diagnostic → bounded API repair → fresh matched A/B, then save a decision.

    The worker owns process lifetime/cancellation. This function never modifies
    the source checkout or launches a coding-agent CLI. Results stay on a private
    candidate branch; confirmed correctness and overall realism remain separate.
    """
    settings = context.settings
    protocol = _protocol(settings)
    rounds = settings.get("max_rounds", 1)
    if type(rounds) is not int or rounds < 1:
        raise ValueError("max_rounds must be a positive integer")
    multiplier = settings.get("trial_reserve_multiplier", 1.15)
    scan_reserve = settings.get("scan_reserve", 1.0)
    repair_budget = settings.get("repair_budget", 2.0)
    if any(not math.isfinite(v) or v <= 0 for v in (multiplier, scan_reserve, repair_budget)) or multiplier < 1:
        raise ValueError("All reserves must be positive; trial reserve multiplier must be at least one")
    trial_reserve = protocol["trials"] * protocol["cost_limit"] * multiplier
    batch_reserve = trial_reserve + scan_reserve
    skip_diagnostic = settings.get("skip_diagnostic", False) or settings.get("use_supplied_findings", False)
    if skip_diagnostic and not settings.get("findings"):
        raise ValueError("Skipping diagnostic trials requires supplied development findings")
    batches = 2 if skip_diagnostic else 3
    plan = {"protocol": protocol, "rounds": rounds, "batches_per_round": batches,
            "skip_diagnostic": bool(skip_diagnostic),
            "diagnostic_reused_in_comparison": False, "per_round_reserve_usd": batches * batch_reserve + repair_budget,
            "trial_reserve_usd": trial_reserve, "scan_reserve_usd": scan_reserve,
            "repair_budget_usd": repair_budget, "budget_usd": settings["budget_usd"],
            "billing_note": "Reservations are estimates, not provider billing ceilings; unknown charges remain held. Modal compute is separate."}
    atomic_json(context.directory / "plan.json", plan)
    rows = []
    retained_commit = settings["source_commit"]
    pending_candidate = None
    try:
        _preflight(context, rounds * plan["per_round_reserve_usd"])
        hashes = _hashes(context.worktree)
        source = settings["source_commit"]
        _assert_source(context, source, hashes, tag="initial")
        for number in range(1, rounds + 1):
            context.check_cancelled()
            _preflight(context, (rounds - number + 1) * plan["per_round_reserve_usd"])
            prefix = f"round-{number}"
            diagnostic = None
            findings = []
            if not skip_diagnostic:
                diagnostic = _batch(context, protocol, source, hashes, f"{prefix}-diagnostic", cwd=context.worktree,
                                    trial_reserve=trial_reserve, scan_reserve=scan_reserve)
                findings = _mine(context, protocol, diagnostic)
            # Copied caller findings are development evidence, never comparison scores.
            supplied = [{"path": path, "content": Path(path).read_text()} for path in settings.get("findings", [])]
            context.update("investigating and independently reviewing a focused repair", round=number)
            settings["repair_stage_prefix"] = f"{prefix}-repair"
            settings["measurement_reserve_usd"] = 2 * batch_reserve + (rounds - number) * plan["per_round_reserve_usd"]
            repair = _repair(context, {"diagnostic": findings, "supplied": supplied})
            atomic_json(context.directory / f"{prefix}-repair.json", repair)
            candidate = repair.get("commit")
            if repair.get("status") != "done" or not candidate or candidate == source:
                rows.append({"round": number, "source": source, "diagnostic": diagnostic, "repair": repair,
                             "realism_accepted": False, "correctness_retained": False, "decision": "no_verified_candidate"})
                break
            _assert_source(context, candidate, hashes, tag=f"{prefix}-candidate")
            if _git(context, "diff", source, candidate, "--", *LOCKED, name=f"{prefix}-locked"):
                raise ValueError("Candidate changed locked scenario rules or terms")
            job_id = settings.get("job_id", context.directory.name)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
                raise ValueError("Invalid job branch identifier")
            candidate_branch = f"realism/job-{job_id}-round-{number}"
            _git(context, "branch", "--no-track", candidate_branch, candidate, name=f"{prefix}-candidate-ref")
            repair["branch"] = candidate_branch
            atomic_json(context.directory / f"{prefix}-repair.json", repair)
            correctness = _verified_correctness(repair)
            pending_candidate = {"commit": candidate, "branch": repair.get("branch"), "correctness": correctness}
            if correctness:
                retained_commit = candidate
            atomic_json(context.directory / f"{prefix}-repair-retention.json", {
                "source": source, "candidate": candidate, "correctness_retained": correctness,
                "retained_commit": candidate if correctness else source,
                "measurement_status": "pending", "realism_accepted": False,
                "main_checkout_changed": False, "branch": repair.get("branch")})
            control = context.directory / f"control-{number}"
            _git(context, "worktree", "add", "--detach", str(control), source, name=f"{prefix}-control-create")
            context.update("running fresh A/B; development findings are excluded from scores", round=number)
            reference = _batch(context, protocol, source, hashes, f"{prefix}-control", cwd=control,
                               trial_reserve=trial_reserve, scan_reserve=scan_reserve)
            changed = _batch(context, protocol, candidate, hashes, f"{prefix}-candidate", cwd=context.worktree,
                             trial_reserve=trial_reserve, scan_reserve=scan_reserve)
            comparison = compare(reference["score"]["per_trial"], changed["score"]["per_trial"])
            accepted = comparison["decision"] == "better"
            retained = accepted or correctness
            row = {"round": number, "source": source, "candidate": candidate, "repair": repair,
                   "diagnostic": diagnostic, "reference": reference, "changed": changed, "compare": comparison,
                   "realism_accepted": accepted, "correctness_retained": correctness,
                   "retained_commit": candidate if retained else source,
                   "measured_commit": candidate, "decision": comparison["decision"],
                   "scope": "exploratory short A/B; no behaviour severity inference"}
            atomic_json(context.directory / f"{prefix}-decision.json", row)
            rows.append(row)
            if retained:
                source = candidate
                retained_commit = candidate
            else:
                _git(context, "switch", "--detach", source, name=f"{prefix}-restore")
            pending_candidate = None
        result = {"status": "complete", "rounds": rows, "retained_commit": source,
                  "main_checkout_changed": False, "automatic_main_merge": False,
                  "spend": stages.show(context.ledger, settings["budget_usd"])}
    except BudgetStop as error:
        if pending_candidate and not pending_candidate["correctness"]:
            _git(context, "switch", "--detach", retained_commit, name="budget-restore")
        result = {"status": "budget_blocked", "reason": str(error), "rounds": rows, "retained_commit": retained_commit,
                  "pending_candidate": pending_candidate, "realism_accepted": False,
                  "main_checkout_changed": False, "spend": stages.show(context.ledger, settings["budget_usd"])}
    atomic_json(context.directory / "autonomous-result.json", result)
    return result


def _cleanup_targets(directory):
    sessions = set()
    for path in (directory / "batches").glob("*/trials/jobs/*/*/config.json"):
        config = json.loads(path.read_text())
        name = config.get("trial_name", "")
        if (name != path.parent.name or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", name)
                or config.get("environment", {}).get("type") != "modal"):
            continue
        if Path(config.get("trials_dir", "")).resolve() != path.parent.parent.resolve():
            continue
        sessions.add(f"{name}__env")
    return sorted(sessions)


def cleanup_job(context):
    """Terminate only Modal sandboxes tagged for native trials inside this job."""
    targets = _cleanup_targets(context.directory)
    output = context.directory / "modal-cleanup.json"
    if not targets:
        result = {"sessions": [], "terminated": [], "unresolved": []}
        atomic_json(output, result)
        return result
    path = context.directory / "modal-cleanup-targets.json"
    atomic_json(path, {"sessions": targets})
    response = context.command(["scripts/harbor", "python", "-m", "refund_eval.realism.autonomous",
                                "--cleanup-modal", str(path), str(output)], name="modal-cleanup", timeout=35,
                               allow_cancelled=True)
    if response["returncode"] or not output.exists():
        return {"sessions": targets, "terminated": [], "unresolved": targets}
    return json.loads(output.read_text())


def _cleanup_modal(targets, output):
    from modal import Sandbox

    sessions = json.loads(Path(targets).read_text())["sessions"]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,160}__env", name) for name in sessions):
        raise ValueError("Invalid job sandbox identity")
    terminated, unresolved = [], []
    for name in sessions:
        try:
            for sandbox in Sandbox.list(tags={"harbor.managed": "true", "harbor.session_id": name}):
                tags = sandbox.get_tags()
                if tags.get("harbor.managed") == "true" and tags.get("harbor.session_id") == name:
                    sandbox.terminate()
                    sandbox.wait(raise_on_termination=False)
                    terminated.append(sandbox.object_id)
                else:
                    unresolved.append(name)
        except Exception:  # noqa: BLE001 -- cleanup must report every unresolved remote without exposing credentials
            unresolved.append(name)
    atomic_json(Path(output), {"sessions": sessions, "terminated": terminated, "unresolved": unresolved})


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--cleanup-modal":
        raise SystemExit("Only the fixed worker cleanup entry point is supported")
    _cleanup_modal(sys.argv[2], sys.argv[3])

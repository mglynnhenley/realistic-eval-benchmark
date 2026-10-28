"""Durable candidate-only loop steps; investigation remains coding-agent work.

No baseline is launched here. Compatible evidence supports a comparison;
diagnostic history never enters one. Every paid step uses an explicit existing
shared ledger, exact reviews, immutable preparation and a one-use start marker.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

from refund_eval.realism import autonomous, measurement, retained_hold, stages
from refund_eval.realism.background import JobContext, atomic_json, now
from refund_eval.realism.evidence import load_evidence
from refund_eval.realism.native_attribution import analyze

INTERPRETATION = {
    "environment_findings": "Scanner annotations require investigation and independent verification",
    "evaluation_awareness": "Not inferred from poor task performance or possible tells",
    "task_failure_cause": "Undetermined without separate native evidence",
    "correctness_retention": "Independent correctness evidence, separate from aggregate realism",
    "automatic_model_escalation": False,
}


def require(condition, message):
    if not condition:
        raise measurement.Rejected(message)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    value = json.loads(Path(path).read_text())
    require(isinstance(value, dict), "Expected a saved JSON object")
    return value


class LoopContext(JobContext):
    def command(self, *args, env=None, **kwargs):
        # Harbor's independent uv project otherwise may select an unsupported
        # newer Python even when the coordinator uses its tested interpreter.
        environment = {**os.environ, **(env or {}), "UV_PYTHON": sys.executable}
        return super().command(*args, env=environment, **kwargs)


def inventory(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        require(not path.is_symlink(), "Frozen preparation must not contain links")
        if path.is_file():
            result[str(path.relative_to(directory))] = sha(path)
    return result


def verify_prepared(protocol, directory, repo, candidate):
    source = directory / "task/environment/source"
    prepared = autonomous._prepared_trials(protocol, directory)
    kwargs = read(directory / "job.json")["agents"][0].get("kwargs", {})
    require(kwargs.get("history_tokens") == protocol["history_tokens"]
            and kwargs.get("budget_notice") == protocol["budget_notice"], "Prepared memory/notice settings differ")
    require(prepared.get("commit") == candidate and prepared.get("dirty") is False,
            "Prepared source must match the exact clean candidate")
    aggregate = hashlib.sha256()
    manifest = Path("refund_eval/_source_manifest.json")
    copied = inventory(source)
    require(bool(copied), "Prepared source is missing")
    tracked = measurement._git(repo, "ls-tree", "-r", "--name-only", candidate).splitlines()
    expected = {name for name in tracked if name.startswith("refund_eval/")
                or name in {"pyproject.toml", "uv.lock", "README.md", "LICENSE"}}
    require(set(copied) == expected | {str(manifest)}, "Prepared source inventory differs from candidate")
    for name in sorted(copied):
        if Path(name) == manifest:
            continue
        content = (source / name).read_bytes()
        original = subprocess.run(["git", "-C", str(repo), "show", f"{candidate}:{name}"],
                                  capture_output=True, check=False)
        require(original.returncode == 0 and original.stdout == content,
                "Packaged source differs from candidate Git bytes")
        for value in (name.encode(), content):
            aggregate.update(len(value).to_bytes(8, "big"))
            aggregate.update(value)
    require(aggregate.hexdigest() == prepared.get("source_sha256"), "Source aggregate differs")
    return {"source": prepared, "artifacts": inventory(directory)}


def historical_reference(path):
    """Validate history against its own recorded identities; never normalize it."""
    manifest = read(path)
    saved = load_evidence(path, commit=manifest["source_commit"],
                          protocol=manifest["protocol"], hashes=manifest["measurement_hashes"])
    return {"evidence": str(path), "manifest_sha256": sha(path), "commit": saved["commit"],
            "protocol": saved["protocol"], "score": saved["score"],
            "status": "historical_reference_only", "used_for_comparison": False}


def approved(path, kind, **bindings):
    value = read(path)
    require(value.get("approved") is True and value.get("review_kind") == kind
            and isinstance(value.get("reviewer"), str) and bool(value["reviewer"].strip()),
            "An exact named independent approval is required")
    require(all(value.get(key) == expected for key, expected in bindings.items()),
            "Independent review bindings differ")
    return value


def cleanup_verified(cleanup):
    return (isinstance(cleanup, dict)
            and all(isinstance(cleanup.get(key), list) for key in ("sessions", "terminated", "unresolved"))
            and cleanup.get("remote_state") != "unknown" and not cleanup["unresolved"]
            and not cleanup.get("discovery_errors"))


def read_result(run_dir):
    """Read a sealed historical outcome without reconstructing today's budget."""
    directory = Path(run_dir)
    request = read(directory / "request.json")
    fingerprint = request.pop("fingerprint")
    result, completion = read(directory / "result.json"), read(directory / "completion.json")
    require(measurement._json_hash(request) == fingerprint
            and read(directory / "execution-started.json").get("fingerprint") == fingerprint
            and completion.get("fingerprint") == fingerprint
            and completion.get("result_sha256") == measurement._json_hash(result), "Historical result integrity differs")
    return result


def run_candidate(*, repo, run_dir, baseline, candidate, review, runner_review,
                  ledger, budget, settings, baseline_evidence=None, historical_evidence=None,
                  execute=False, execution_review=None, prior_unknown_review=None):
    """Prepare/read/execute one candidate-only round, with no baseline fallback."""
    repo, run_dir, ledger = Path(repo).resolve(), Path(run_dir).resolve(), Path(ledger).resolve()
    context = None
    try:
        require(type(execute) is bool, "Execution must be explicit")
        require(not (baseline_evidence and historical_evidence), "Choose compatible reuse or diagnostic history")
        private = repo / ".context/realism"
        require(run_dir != private and run_dir.is_relative_to(private), "Use a private run directory")
        require(ledger.is_file() and ledger.is_relative_to(private), "Use the existing shared private ledger")
        base, changed, runner = (measurement._commit(repo, value) for value in (baseline, candidate, "HEAD"))
        require(base != changed and measurement._git(repo, "merge-base", base, changed) == base,
                "Candidate must descend from the reviewed Git base")
        paths = measurement._git(repo, "diff", "--no-renames", "--name-only", base, changed).splitlines()
        require(not any(measurement._protected(path) for path in paths), "Protected candidate changes")
        _, review_bytes = measurement._review(Path(review), repo, base, changed)
        approved(runner_review, "measurement_infrastructure_review", candidate=runner, runner=runner,
                 scenario_preserved=True, paid_execution_ready=True)
        measurement._assert_trusted_source(repo, runner)
        load_dotenv(repo / ".env", override=False)
        selected = {"model": "openrouter/anthropic/claude-sonnet-5.5", "time_limit": 1800,
                    "trial_wallclock_limit": 1800, **settings}
        protocol = autonomous._protocol(selected)
        if protocol.get("trial_deadlines") is not None:
            hard = protocol["trial_deadlines"]["command_hard_seconds"]
            require("trial_wallclock_limit" not in settings or (type(settings["trial_wallclock_limit"]) is int
                    and settings["trial_wallclock_limit"] == hard),
                    "Trial wallclock limit must match the canonical deadline policy")
            selected["trial_wallclock_limit"] = hard
        protocol.update(history_tokens=60000, budget_notice="every_step")
        require(type(budget) in (int, float) and 0 < budget < float("inf"), "Invalid total budget")
        require(type(selected["trial_wallclock_limit"]) is int and selected["trial_wallclock_limit"] > 0,
                "Invalid trial wallclock limit")
        require(sys.version_info[:2] == (3, 14), "Use the tested Python 3.14 coordinator interpreter")
        multiplier, scan_reserve = selected.get("trial_reserve_multiplier", 1.15), selected.get("scan_reserve", 2)
        require(type(multiplier) in (int, float) and 1 <= multiplier < float("inf")
                and type(scan_reserve) in (int, float) and 0 < scan_reserve < float("inf"), "Invalid reserves")
        trial_reserve = protocol["trials"] * protocol["cost_limit"] * multiplier
        hashes = measurement._hashes(repo, runner)
        reference, history = None, None
        if baseline_evidence:
            reference = load_evidence(baseline_evidence, commit=base, protocol=protocol, hashes=hashes)
        if historical_evidence:
            history = historical_reference(Path(historical_evidence).resolve())
        # Current allowance may differ from old requests, which stay immutable.
        spend = stages.show(ledger, budget) if prior_unknown_review is None else None
        with measurement._exclusive(repo):
            if prior_unknown_review is not None:
                spend = retained_hold.read_spend(ledger, budget)
            amendment = (spend.get("budget_amendments") or [{}])[-1].get("id")
            request = {"version": 1, "mode": "candidate_only", "baseline_for_review": base,
                       "candidate": changed, "runner": runner, "protocol": protocol,
                       "measurement_hashes": hashes, "shared_ledger": str(ledger), "budget_usd": budget,
                       "budget_amendment_id": amendment, "trial_reserve_usd": trial_reserve,
                       "scan_reserve_usd": scan_reserve, "trial_wallclock_limit": selected["trial_wallclock_limit"],
                       "review_sha256": hashlib.sha256(review_bytes).hexdigest(),
                       "runner_review_sha256": sha(runner_review),
                       "baseline_evidence": str(Path(baseline_evidence).resolve()) if baseline_evidence else None,
                       "baseline_evidence_sha256": sha(baseline_evidence) if baseline_evidence else None,
                       "historical_reference": history, "interpretation": INTERPRETATION}
            if prior_unknown_review is not None:
                binding = retained_hold.future_binding(repo, run_dir, request)
                request["prior_unknown_disposition"] = retained_hold.freeze_disposition(
                    repo, prior_unknown_review, ledger=ledger, budget=budget, binding=binding,
                    required_reserve=trial_reserve + scan_reserve)
            fingerprint = measurement._json_hash(request)
            tag = f"loop-{fingerprint[:16]}-candidate"
            run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            manifest, marker = run_dir / "request.json", run_dir / "execution-started.json"
            if manifest.exists():
                saved_request = read(manifest)
                saved_fingerprint = saved_request.pop("fingerprint", None)
                require(saved_fingerprint == fingerprint
                        and measurement._json_hash(saved_request) == fingerprint
                        and saved_request == request, "Immutable loop request changed")
            else:
                atomic_json(manifest, {**request, "fingerprint": fingerprint})
            if marker.exists():
                if (run_dir / "completion.json").exists() and (run_dir / "result.json").exists():
                    return read_result(run_dir)
                raise measurement.Rejected("Execution already started; reconcile original work without replay")
            require(not any(f"{tag}-{suffix}" in spend["stages"] for suffix in ("trials", "tells")),
                    "Existing paid stage requires reconciliation even without a local start marker")
            if prior_unknown_review is None:
                require(all(item.get("actual_usd") is not None and item.get("status") == "complete"
                            for item in spend["stages"].values()), "Reconcile previous paid stages before a new round")
            runtime = {**selected, "repo": str(repo), "job_id": run_dir.name,
                       "budget_usd": budget, "source_commit": changed}
            atomic_json(run_dir / "settings.json", runtime)
            context = LoopContext(run_dir)
            context.ledger = ledger
            context.validate_native_usage = True
            if prior_unknown_review is not None:
                context.read_spend = retained_hold.read_spend
                guard = retained_hold.ReservationGuard(
                    repo, prior_unknown_review, ledger=ledger, budget=budget, binding=binding,
                    disposition=request["prior_unknown_disposition"], fingerprint=fingerprint,
                    trial_reserve=trial_reserve, scan_reserve=scan_reserve)
                context.reserve_stage = guard.reserve
            context.measurement_worktree, context.measurement_hashes = run_dir / "runner", hashes
            autonomous._preflight(context, trial_reserve + scan_reserve)
            measurement._worktree(repo, context.measurement_worktree, runner)
            measurement._worktree(repo, context.worktree, changed)
            prepared_dir, preparation = run_dir / "prepared-trials", run_dir / "preparation.json"
            if not preparation.exists():
                require(not prepared_dir.exists(), "Incomplete unpaid preparation retained; inspect it before continuing")
                autonomous._prepare_trials(context, protocol, prepared_dir, context.worktree, "loop-unpaid")
                atomic_json(preparation, verify_prepared(protocol, prepared_dir, repo, changed))
            require(verify_prepared(protocol, prepared_dir, repo, changed) == read(preparation), "Frozen preparation changed")
            if not (run_dir / "public-preflight.json").exists():
                atomic_json(run_dir / "public-preflight.json", autonomous._scan_preflight(protocol, context))
            plan = {**request, "fingerprint": fingerprint, "status": "prepared", "new_trials": protocol["trials"],
                    "baseline_launched": False, "required_reserve_usd": trial_reserve + scan_reserve,
                    "remaining_usd": spend["remaining_usd"], "can_execute": True,
                    "preparation_sha256": sha(preparation), "public_preflight_sha256": sha(run_dir / "public-preflight.json")}
            atomic_json(run_dir / "plan.json", plan)
            if not execute:
                return plan
            require(execution_review is not None, "Prepared loop needs independent execution review")
            approved(execution_review, "candidate_only_execution_review", execution_approved=True,
                     request_fingerprint=fingerprint, runner=runner,
                     preparation_sha256=plan["preparation_sha256"],
                     public_preflight_sha256=plan["public_preflight_sha256"])
            measurement._assert_trusted_source(repo, runner)
            autonomous._preflight(context, trial_reserve + scan_reserve)
            fresh = autonomous._scan_preflight(protocol, context)
            require({k: v for k, v in fresh.items() if k != "planned_at"}
                    == {k: v for k, v in read(run_dir / "public-preflight.json").items() if k != "planned_at"},
                    "Public scan tariff/preflight changed after review")
            if prior_unknown_review is not None:
                require(retained_hold.freeze_disposition(
                    repo, prior_unknown_review, ledger=ledger, budget=budget, binding=binding,
                    required_reserve=trial_reserve + scan_reserve) == request["prior_unknown_disposition"],
                    "Retained-hold approval changed before execution")
            with marker.open("x") as stream:
                json.dump({"fingerprint": fingerprint, "started_at": now()}, stream)
            result = {"mode": "candidate_only", "fingerprint": fingerprint,
                      "historical_reference": history, "comparison": None, "interpretation": INTERPRETATION}
            try:
                with measurement._interruptible():
                    batch = autonomous._batch(context, protocol, changed, autonomous._hashes(context.worktree),
                                              tag, cwd=context.worktree, trial_reserve=trial_reserve, scan_reserve=scan_reserve,
                                              prepared_trials={**read(preparation), "directory": str(prepared_dir)})
                    batch["evidence"] = measurement._export(batch, hashes)
                    attribution = batch.get("native_attribution") or analyze(batch["logs"])
                    atomic_json(run_dir / "native-attribution.json", attribution)
                    result.update(status="complete", candidate=batch, native_attribution=attribution)
                    require(attribution["status"] == "verified", "Native evidence requires reconciliation")
                    if reference:
                        result["comparison"] = autonomous.compare(reference["score"]["per_trial"], batch["score"]["per_trial"])
            except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- retain paid failure without replay
                result.update(status="reconciliation_required", error_type=type(error).__name__)
            finally:
                try:
                    cleanup = autonomous.cleanup_job(context)
                except Exception as error:  # noqa: BLE001 -- missing cleanup remains unknown
                    cleanup = {"remote_state": "unknown", "error_type": type(error).__name__}
                result.update(cleanup=cleanup, spend=(retained_hold.read_spend(ledger, budget)
                              if prior_unknown_review is not None else stages.show(ledger, budget)), finished_at=now())
                if not cleanup_verified(cleanup):
                    result["status"] = "reconciliation_required"
                if result["status"] == "complete" and any(
                        item.get("actual_usd") is None for stage, item in result["spend"]["stages"].items()
                        if stage.startswith(tag + "-")):
                    result["status"] = "awaiting_cost_reconciliation"
                atomic_json(run_dir / "result.json", result)
                atomic_json(run_dir / "state.json", {"status": result["status"], "next_action": "review_saved_evidence",
                                                      "finished_at": now()})
                atomic_json(run_dir / "completion.json", {"fingerprint": fingerprint,
                            "result_sha256": measurement._json_hash(result)})
            return result
    except (ValueError, TypeError, OSError, KeyError, subprocess.SubprocessError) as error:
        return {"status": "rejected", "error_type": type(error).__name__, "artifacts": str(run_dir),
                "reason": str(error) if isinstance(error, measurement.Rejected) else "Invalid saved loop input"}

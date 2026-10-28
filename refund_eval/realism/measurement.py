"""Measure externally reviewed commits without invoking a coding agent.

Preparation is free and execution is explicit. A paid invocation is single-use:
an interrupted start must be reconciled, never replayed. Results report evidence;
they do not merge a candidate or certify a correctness repair.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

from refund_eval.realism import autonomous, stages
from refund_eval.realism.background import JobContext, atomic_json, now

PROTECTED = (
    "refund_eval/realism/", "refund_eval/grading/", "refund_eval/harbor/",
    "refund_eval/agent/budget.py", "refund_eval/scenario/schema.py", "refund_eval/scenario/dossiers/",
    "refund_eval/scenario/train_refund_pressure.yaml", "scout/", "integrations/harbor/",
    "scripts/harbor", "pyproject.toml", "uv.lock", "refund_eval/config.py",
    "refund_eval/killswitch.py", "refund_eval/model_io.py", "refund_eval/model_pricing.py",
    "refund_eval/task.py", "refund_eval/tasks/", "refund_eval/output_paths.py", *autonomous.LOCKED,
)


class Rejected(ValueError):
    """A public, credential-free input validation failure."""


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise Rejected("Git source validation failed; use committed repository revisions")
    return result.stdout.strip()


def _commit(repo: Path, revision: str) -> str:
    if not isinstance(revision, str) or not revision or revision.startswith("-"):
        raise Rejected("A committed source revision is required")
    return _git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")


def _protected(path: str) -> bool:
    return any(path == item.rstrip("/") or path.startswith(item.rstrip("/") + "/") for item in PROTECTED)


def _hashes(repo: Path, commit: str) -> dict:
    files = _git(repo, "ls-tree", "-r", "--name-only", commit).splitlines()
    result = {}
    for path in files:
        if _protected(path):
            data = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{path}"],
                                  capture_output=True, check=True).stdout
            result[path] = hashlib.sha256(data).hexdigest()
    return result


def _review(path: Path, repo: Path, baseline: str, candidate: str) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise Rejected("Review receipt must be a JSON object")
    base = value.get("baseline", value.get("baseline_commit", ""))
    changed = value.get("candidate", value.get("candidate_commit", ""))
    if (not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base)
            or not isinstance(changed, str) or not re.fullmatch(r"[0-9a-f]{40,64}", changed)
            or base != baseline or changed != candidate
            or value.get("approved") is not True or value.get("scenario_preserved") is not True
            or not isinstance(value.get("reviewer"), str) or not value["reviewer"].strip()):
        raise Rejected("An approved independent review must bind these exact baseline and candidate commits")
    return value, raw


def _json_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _worktree(repo: Path, directory: Path, commit: str) -> None:
    if directory.exists():
        if _git(directory, "rev-parse", "HEAD") != commit or _git(directory, "status", "--porcelain"):
            raise Rejected("Prepared worktree changed; use the saved source without editing it")
        return
    _git(repo, "worktree", "add", "--detach", str(directory), commit)


def _assert_trusted_source(repo: Path, commit: str) -> None:
    dirty = _git(repo, "diff", "--no-renames", "--name-only", "HEAD").splitlines()
    untracked = _git(repo, "ls-files", "--others", "--exclude-standard").splitlines()
    if any(_protected(path) for path in dirty) or any(_protected(path) and path.endswith(".py") for path in untracked):
        raise Rejected("Commit local measurement and controller changes before executing paid stages")
    # This also covers an ignored/untracked entry point loaded from the working
    # checkout: its actual bytes must exist in the pinned runner revision.
    loaded = Path(__file__).resolve()
    if loaded.is_relative_to(repo):
        relative = str(loaded.relative_to(repo))
        result = subprocess.run(["git", "-C", str(repo), "show", f"{commit}:{relative}"],
                                capture_output=True, check=False)
        if result.returncode or result.stdout != loaded.read_bytes():
            raise Rejected("The loaded measurement entry point must match its committed trusted runner")


@contextmanager
def _exclusive(repo: Path):
    common = Path(_git(repo, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = repo / common
    with (common / "realism-measurement.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Rejected("Another measurement command is active in this repository") from None
        yield


@contextmanager
def _interruptible():
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.getsignal(signal.SIGTERM)

        def stopped(*_):
            raise InterruptedError("Measurement interrupted")

        signal.signal(signal.SIGTERM, stopped)
    try:
        yield
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _saved_baseline(context, path: Path, commit: str, protocol: dict, hashes: dict) -> dict:
    from refund_eval.realism.evidence import load_evidence
    saved = load_evidence(path, commit=commit, protocol=protocol, hashes=hashes)
    out = context.directory / "saved-baseline"
    out.mkdir(exist_ok=True)
    source_map = out / "source-map.json"
    atomic_json(source_map, saved["source_map"])
    selected = {**protocol, "trials": saved["expected_trials"]}
    score = autonomous._score(context, selected, saved["logs"], saved["scans"],
                              context.measurement_worktree, out, "saved-baseline", source_map=source_map)
    return {"tag": "saved-baseline", "commit": commit, "protocol": selected, "score": score,
            "logs": saved["logs"], "scans": str(saved["scans"]), "path": str(path), "reused": True}


def _validate_saved(path: Path, commit: str, protocol: dict, hashes: dict) -> None:
    from refund_eval.realism.evidence import load_evidence
    load_evidence(path, commit=commit, protocol=protocol, hashes=hashes)


def _export(batch: dict, hashes: dict) -> str:
    from refund_eval.realism.evidence import write_evidence
    return str(write_evidence(batch, hashes))


def run_measurement(*, repo: Path, run_dir: Path, baseline: str, candidate: str,
                    review: Path, budget: float, settings: dict,
                    baseline_evidence: Path | None = None, execute: bool = False) -> dict:
    """Prepare or run one reviewed comparison, preserving every paid-stage marker.

    ``budget`` covers additional model spend only; Modal and coding-agent costs
    are separate. Existing baseline evidence may contain a different number of
    trials but must otherwise match the exact per-trial protocol and judge.
    """
    repo, run_dir, review = Path(repo).resolve(), Path(run_dir).resolve(), Path(review).resolve()
    context = None
    try:
        if not run_dir.is_relative_to(repo / ".context" / "realism") or run_dir == repo / ".context" / "realism":
            raise Rejected("run_dir must be a private child directory of repository .context/realism")
        if _git(repo, "rev-parse", "--show-toplevel") != str(repo):
            raise Rejected("repo must be the repository root")
        from refund_eval.output_paths import validate_private_output
        validate_private_output(run_dir)
        if type(execute) is not bool or isinstance(budget, bool) or not math.isfinite(budget) or budget < 0:
            raise Rejected("Use a finite nonnegative budget and explicit execute flag")
        if not isinstance(settings, dict):
            raise Rejected("settings must be an object")
        base, changed, runner = _commit(repo, baseline), _commit(repo, candidate), _commit(repo, "HEAD")
        if base == changed:
            raise Rejected("Candidate must contain a reviewed change")
        if subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", base, changed],
                          capture_output=True, check=False).returncode:
            raise Rejected("Candidate must descend from the baseline commit")
        paths = _git(repo, "diff", "--no-renames", "--name-only", base, changed).splitlines()
        forbidden = sorted(path for path in paths if _protected(path))
        if forbidden:
            return {"status": "rejected", "reason": "Candidate changes protected benchmark or measurement files",
                    "protected_changes": forbidden, "paid_stages_started": False}
        receipt, review_bytes = _review(review, repo, base, changed)
        from dotenv import load_dotenv
        load_dotenv(repo / ".env", override=False)
        selected = {"time_limit": 1800, "trial_wallclock_limit": 1800, **settings}
        protocol = autonomous._protocol(selected)
        wallclock = selected["trial_wallclock_limit"]
        multiplier = selected.get("trial_reserve_multiplier", 1.15)
        scan_reserve = selected.get("scan_reserve", 1.0)
        if (type(wallclock) is not int or wallclock <= 0 or isinstance(multiplier, bool)
                or isinstance(scan_reserve, bool) or not math.isfinite(multiplier) or multiplier < 1
                or not math.isfinite(scan_reserve) or scan_reserve <= 0):
            raise Rejected("Trial wallclock and stage reservation limits must be positive and finite")
        hashes = _hashes(repo, runner)
        evidence_path = Path(baseline_evidence).resolve() if baseline_evidence is not None else None
        evidence_digest = None
        if evidence_path:
            _validate_saved(evidence_path, base, protocol, hashes)
            evidence_digest = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        trial_reserve = protocol["trials"] * protocol["cost_limit"] * multiplier
        required = (1 if evidence_path else 2) * (trial_reserve + scan_reserve)
        request = {"version": 1, "repo": str(repo), "baseline": base, "candidate": changed,
                   "runner": runner, "protocol": protocol, "measurement_hashes": hashes,
                   "review_sha256": hashlib.sha256(review_bytes).hexdigest(), "budget_usd": budget,
                   "baseline_evidence": str(evidence_path) if evidence_path else None,
                   "baseline_evidence_sha256": evidence_digest, "trial_wallclock_limit": wallclock,
                   "trial_reserve_usd": trial_reserve, "scan_reserve_usd": scan_reserve}
        fingerprint = _json_hash(request)
        with _exclusive(repo):
            run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(run_dir, 0o700)
            manifest = run_dir / "request.json"
            if manifest.exists():
                if json.loads(manifest.read_text()).get("fingerprint") != fingerprint:
                    raise Rejected("run_dir already belongs to different immutable measurement settings")
                result_path = run_dir / "result.json"
                if result_path.exists():
                    try:
                        previous = json.loads(result_path.read_text())
                        if not isinstance(previous, dict):
                            raise TypeError("Invalid saved result")
                    except (ValueError, TypeError, OSError):
                        return {"status": "reconciliation_required", "artifacts": str(run_dir),
                                "reason": "Saved result is unreadable; verify original evidence without retrying"}
                    if previous.get("status") == "complete":
                        completion = run_dir / "completion.json"
                        try:
                            sealed = json.loads(completion.read_text()) if completion.exists() else {}
                        except (ValueError, OSError):
                            sealed = {}
                        if (not isinstance(sealed, dict) or sealed.get("fingerprint") != fingerprint
                                or sealed.get("result_sha256") != _json_hash(previous)):
                            return {"status": "reconciliation_required", "artifacts": str(run_dir),
                                    "reason": "Saved completion report changed or lost its digest; verify original evidence without retrying"}
                        # This is a historical report, not a fresh evidence scan.
                        # Reusing an arm in a new comparison validates its full bundle.
                        return previous
                if (run_dir / "execution-started.json").exists():
                    return {"status": "reconciliation_required", "artifacts": str(run_dir),
                            "reason": "Execution already started; inspect saved stages and recover original outputs without retrying"}
            else:
                atomic_json(manifest, {**request, "fingerprint": fingerprint})
                atomic_json(run_dir / "review.json", receipt)
            plan = {"status": "prepared", "baseline": base, "candidate": changed, "runner": runner,
                    "protocol": protocol, "baseline_reused": bool(evidence_path),
                    "new_trials": protocol["trials"] * (1 if evidence_path else 2),
                    "required_reserve_usd": required, "budget_usd": budget,
                    "can_execute": required <= budget + 1e-9, "artifacts": str(run_dir),
                    "note": "Model-spend estimates are not provider billing ceilings; Modal and coding agents bill separately",
                    "review_note": "Receipt records independent approval; it is not an automatic correctness certification"}
            atomic_json(run_dir / "plan.json", plan)
            if not execute:
                return plan
            if required > budget + 1e-9:
                return {**plan, "status": "budget_blocked", "paid_stages_started": False}
            _assert_trusted_source(repo, runner)
            # All helpers execute from a clean trusted runner, never candidate controller code.
            _worktree(repo, run_dir / "runner", runner)
            _worktree(repo, run_dir / "baseline", base)
            _worktree(repo, run_dir / "worktree", changed)
            runtime = {**selected, "repo": str(repo), "job_id": run_dir.name,
                       "budget_usd": budget, "source_commit": changed}
            atomic_json(run_dir / "settings.json", runtime)
            context = JobContext(run_dir)
            context.measurement_worktree = run_dir / "runner"
            context.measurement_hashes = hashes
            reference = _saved_baseline(context, evidence_path, base, protocol, hashes) if evidence_path else None
            # Marker precedes the first possibly paid operation and survives process death.
            atomic_json(run_dir / "execution-started.json", {"fingerprint": fingerprint, "started_at": now()})
            try:
                with _interruptible():
                    if reference is None:
                        reference = autonomous._batch(context, protocol, base, autonomous._hashes(run_dir / "baseline"),
                                                     "baseline", cwd=run_dir / "baseline",
                                                     trial_reserve=trial_reserve, scan_reserve=scan_reserve)
                        reference["evidence"] = _export(reference, hashes)
                    evaluated = autonomous._batch(context, protocol, changed, autonomous._hashes(context.worktree),
                                                  "candidate", cwd=context.worktree,
                                                  trial_reserve=trial_reserve, scan_reserve=scan_reserve)
                    evaluated["evidence"] = _export(evaluated, hashes)
                    result = {"status": "complete", "baseline": reference, "candidate": evaluated,
                              "comparison": autonomous.compare(reference["score"]["per_trial"], evaluated["score"]["per_trial"]),
                              "spend": stages.show(context.ledger, budget), "artifacts": str(run_dir),
                              "automatic_main_merge": False, "main_checkout_changed": False,
                              "scope": "Exploratory realism comparison; correctness and change retention require separate judgment"}
            except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- preserve accounting for every paid failure
                status = "budget_blocked" if isinstance(error, autonomous.BudgetStop) else "failed"
                try:
                    cleanup = autonomous.cleanup_job(context)
                except Exception as cleanup_error:  # noqa: BLE001 -- report class only, never provider text
                    cleanup = {"status": "failed", "error_type": type(cleanup_error).__name__, "remote_state": "unknown"}
                result = {"status": status, "error_type": type(error).__name__, "artifacts": str(run_dir),
                          "reason": "Measurement stopped; inspect private artifacts and reconcile paid stages without retrying",
                          "cleanup": cleanup, "spend": stages.show(context.ledger, budget),
                          "automatic_main_merge": False, "main_checkout_changed": False}
            atomic_json(run_dir / "result.json", result)
            if result["status"] == "complete":
                atomic_json(run_dir / "completion.json", {
                    "fingerprint": fingerprint, "result_sha256": _json_hash(result)})
            atomic_json(run_dir / "state.json", {"status": result["status"], "finished_at": now()})
            return result
    except (Rejected, ValueError, TypeError, OSError, subprocess.SubprocessError) as error:
        # Native/model exception bodies may include secrets or request payloads.
        reason = str(error) if isinstance(error, Rejected) else "Invalid or unavailable measurement input; inspect local source and evidence"
        return {"status": "rejected", "reason": reason, "error_type": type(error).__name__,
                "artifacts": str(run_dir), "paid_stages_started": (run_dir / "spend.json").exists()}

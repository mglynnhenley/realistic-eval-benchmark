"""CLI-to-evidence checks using native local logs, with every paid boundary fake.

Git isolation, source checks, scan planning/rendering, strict score commands and
portable evidence are real. The process adapter replaces paid Harbor/Scout work
with synthetic native files and replaces public tariffs with a fixed fixture.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from refund_eval.realism import __main__ as cli
from refund_eval.realism import evidence
from refund_eval.realism.background import JobContext

SOURCE = Path(__file__).resolve().parents[1]


def _git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True,
                                   stderr=subprocess.DEVNULL).strip()


def _native_log(path, *, commit, source_hash, protocol, index):
    from inspect_ai.log import (
        EvalConfig,
        EvalDataset,
        EvalLog,
        EvalResults,
        EvalSample,
        EvalSpec,
        write_eval_log,
    )
    from inspect_ai.model import ChatMessageAssistant, ChatMessageUser, ModelUsage

    fields = {"scenario": "scenario", "token_budget": "token_budget", "max_turns": "max_turns",
              "time_limit": "time_limit", "sim_weeks": "sim_weeks", "cost_limit": "cost_limit",
              "research_mode": "research_mode", "data_mode": "published_data"}
    sample_id = f"synthetic-{index}"
    path.parent.mkdir(parents=True, exist_ok=True)
    evaluation = EvalLog(
        status="success",
        eval=EvalSpec(created="2026-01-01T00:00:00Z", task="synthetic", dataset=EvalDataset(),
                      model=protocol["model"], config=EvalConfig(), eval_id=f"{commit}-{index}",
                      task_args={field: protocol[key] for key, field in fields.items()},
                      model_roles={"simulator": {"model": protocol["simulator_model"]},
                                   "grader": {"model": protocol["consistency_model"]}}),
        results=EvalResults(total_samples=1, completed_samples=1),
        samples=[EvalSample(
            id=sample_id, epoch=1, input="synthetic request", target="", completed_at=datetime.now(UTC),
            messages=[ChatMessageUser(content="Synthetic refund enquiry."),
                      ChatMessageAssistant(content="I will inspect the synthetic information.")],
            model_usage={protocol["model"]: ModelUsage(input_tokens=10, output_tokens=10, total_tokens=20, total_cost=.03)},
            metadata={"scenario": protocol["scenario"], "simulator_revision": commit,
                      "task_grading_notes": {"notes": "Synthetic pinned notes."},
                      "realism_version": f"{commit[:7]}-synthetic+src-{source_hash[:12]}"})],
    )
    write_eval_log(evaluation, str(path))


def _native_scan(scans, logs, protocol, *, damaged=False):
    from inspect_ai.log import read_eval_log
    from inspect_scout import ScanSpec

    directory = scans / protocol["judge"].split("/")[-1] / "scan_id=synthetic"
    directory.mkdir(parents=True)
    spec = ScanSpec(scan_name="synthetic", scan_id="synthetic", scanners={"tells": {"name": "tells", "version": 0}},
                    model_roles={"judge": {"model": protocol["judge"]}})
    (directory / "_scan.json").write_text(spec.model_dump_json(exclude_none=True))
    (directory / "_summary.json").write_text(json.dumps({"complete": True, "scanners": {
        "tells": {"scans": len(logs), "results": len(logs), "errors": 0, "tokens": 0}}}))
    (directory / "_errors.jsonl").write_text("")
    rows = []
    for index, log in enumerate(logs):
        evaluation = read_eval_log(log)
        sample = evaluation.samples[0]
        rows.append({"transcript_source_uri": "file://" + log,
                     "transcript_metadata": json.dumps({"id": sample.id, "epoch": sample.epoch,
                         "eval_id": evaluation.eval.eval_id, "eval_status": "success"}),
                     "metadata": "{}", "scan_error": "", "transcript_error": "",
                     "value": '{"tells":[]}', "scanner_name": "tells", "scanner_key": "tells",
                     "scanner_version": 0, "transcript_id": f"sample-{index}", "uuid": f"result-{index}"})
    pd.DataFrame(rows[:-1] if damaged else rows).to_parquet(directory / "tells.parquet")


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for directory in ("refund_eval", "scout"):
        shutil.copytree(SOURCE / directory, repo / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("uv.lock", "pyproject.toml"):
        shutil.copyfile(SOURCE / name, repo / name)
    (repo / "scripts").mkdir()
    shutil.copyfile(SOURCE / "scripts/harbor", repo / "scripts/harbor")
    (repo / ".gitignore").write_text(".context/\n__pycache__/\n*.pyc\n")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Synthetic integration tester")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    target = repo / "refund_eval/sim/integration_example.py"
    target.write_text("VALUE = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "first environment change")
    candidate = _git(repo, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("REFUND_EVAL_EXA_API_KEY", raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    return {"repo": repo, "baseline": baseline, "candidate": candidate, "target": target}


@pytest.fixture
def processes(monkeypatch):
    original = JobContext.command
    calls = {"trials": [], "scans": [], "score_cwds": [], "damaged_scan": False}

    def option(argv, name):
        return argv[argv.index(name) + 1]

    def command(context, argv, *, name, cwd=None, timeout=600, **kwargs):
        cwd = Path(cwd or context.worktree)
        if argv[0] == "git":
            return original(context, argv, name=name, cwd=cwd, timeout=timeout, **kwargs)
        if argv[:4] == ["scripts/harbor", "python", "-m", "refund_eval.harbor"]:
            out = Path(option(argv, "--output"))
            source = out / "task/environment/source"
            source.mkdir(parents=True)
            shutil.copytree(cwd / "refund_eval", source / "refund_eval")
            for directory in ("scout",):
                shutil.copytree(cwd / directory, source / directory)
            fields = {"scenario": "--scenario", "token_budget": "--token-budget", "max_turns": "--max-turns",
                      "time_limit": "--time-limit", "sim_weeks": "--sim-weeks", "cost_limit": "--cost-limit",
                      "research_mode": "--research-mode"}
            kwargs = {field: option(argv, flag) for field, flag in fields.items()}
            for field in ("token_budget", "max_turns", "time_limit"):
                kwargs[field] = int(kwargs[field])
            for field in ("sim_weeks", "cost_limit"):
                kwargs[field] = float(kwargs[field])
            kwargs.update(data_mode="hybrid", simulator_model="openrouter/anthropic/claude-opus-5.5",
                          grader_model="openrouter/anthropic/claude-haiku-4.5")
            (out / "job.json").write_text(json.dumps({"n_attempts": int(option(argv, "--trials")),
                "retry": {"max_retries": 0}, "environment": {"type": "modal"},
                "agents": [{"model_name": option(argv, "--model"), "kwargs": kwargs,
                            "env": {"REFUND_EVAL_EXA_API_KEY": "${REFUND_EVAL_EXA_API_KEY}"}
                            if os.environ.get("REFUND_EVAL_EXA_API_KEY") else {}}]}))
            # Use the real source-manifest function in the actual arm checkout.
            program = "from pathlib import Path; from refund_eval.realism.track import freeze_source_manifest; import sys; freeze_source_manifest(Path(sys.argv[1]))"
            return original(context, [sys.executable, "-c", program, str(source)], name=name, cwd=cwd,
                            timeout=timeout, env=_environment(cwd))
        if argv[:3] == ["scripts/harbor", "harbor", "run"]:
            out = Path(option(argv, "-c")).parent
            batch = json.loads((out.parent / "manifest.json").read_text())
            prepared = json.loads((out.parent / "prepared-source.json").read_text())
            calls["trials"].append((batch["source_commit"], batch["protocol"]["trials"]))
            for index in range(batch["protocol"]["trials"]):
                _native_log(out / f"jobs/run/trial-{index}/trial.eval", commit=batch["source_commit"],
                            source_hash=prepared["source_sha256"], protocol=batch["protocol"], index=index)
            return _response(context, name)
        if argv[:4] == ["uv", "run", "python", "scout/run.py"]:
            start, end = 4, argv.index("--scanners")
            logs, scans = argv[start:end], Path(option(argv, "--scans"))
            batch = json.loads((scans.parent / "manifest.json").read_text())
            calls["scans"].append((batch["source_commit"], len(logs)))
            _native_scan(scans, logs, batch["protocol"], damaged=calls["damaged_scan"])
            return _response(context, name)
        if argv[:3] == ["uv", "run", "python"]:
            translated = [sys.executable, *argv[3:]]
            if translated[1] == "-c":
                # Only public pricing is substituted; real transcript rendering
                # and conservative token/cost planning execute in the runner.
                metadata = {"context_length": 1_000_000, "pricing": {
                    "prompt": "0.000001", "completion": "0.000005", "input_cache_write": "0.00000125"}}
                prefix = ("import refund_eval.realism.scan_budget as sb; "
                          "exec('async def fixed_metadata(judge): return ' + repr(" + repr(metadata) + ")); "
                          "sb.model_metadata=fixed_metadata; ")
                translated[2] = prefix + translated[2]
            else:
                calls["score_cwds"].append(cwd)
            return original(context, translated, name=name, cwd=cwd, timeout=timeout, env=_environment(cwd))
        raise AssertionError(f"Unexpected external command {argv[0]}")

    monkeypatch.setattr(JobContext, "command", command)
    return calls


def _environment(cwd):
    # Synthetic code and CLI subprocesses never receive provider credentials.
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(cwd),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1"}


def _response(context, name):
    log = context.directory / f"{name}.log"
    log.write_text("Synthetic paid-stage replacement completed.\n")
    return {"returncode": 0, "log": str(log)}


def _arguments(repository, tmp_path, *, baseline=None, candidate=None, suffix="first", budget="10", execute=True, saved=None):
    baseline = baseline or repository["baseline"]
    candidate = candidate or repository["candidate"]
    review = tmp_path / f"review-{suffix}.json"
    review.write_text(json.dumps({"baseline": baseline, "candidate": candidate, "approved": True,
                                  "scenario_preserved": True, "reviewer": "Independent synthetic reviewer"}))
    out = repository["repo"] / f".context/realism/measurement/{suffix}"
    argv = ["measure", "--base", baseline, "--candidate", candidate, "--review", str(review),
            "--out", str(out), "--budget", budget, "--token-budget", "100000", "--time-limit", "60"]
    if execute:
        argv.append("--execute")
    if saved:
        argv.extend(["--baseline-evidence", str(saved)])
    return argv, out


def test_complete_cli_round_exports_real_evidence_then_reuses_moved_candidate(repository, processes, tmp_path, capsys):
    argv, out = _arguments(repository, tmp_path)
    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "complete", result
    assert processes["trials"] == [(repository["baseline"], 3), (repository["candidate"], 3)]
    assert len(processes["scans"]) == 2
    assert result["baseline"]["score"]["per_trial"] == [0, 0, 0]
    assert result["candidate"]["score"]["per_trial"] == [0, 0, 0]
    assert result["spend"]["actual_usd"] == pytest.approx(.18)
    assert all(path == out / "runner" for path in processes["score_cwds"])
    exported = Path(result["candidate"]["evidence"])
    moved = repository["repo"] / ".context/realism/moved-baseline"
    shutil.copytree(exported.parent, moved)
    # Prove the second round cannot accidentally read original source paths.
    shutil.rmtree(out / "batches")
    repository["target"].write_text("VALUE = 2\n")
    _git(repository["repo"], "commit", "-qam", "second environment change")
    second_candidate = _git(repository["repo"], "rev-parse", "HEAD")
    argv2, out2 = _arguments(repository, tmp_path, baseline=repository["candidate"],
                             candidate=second_candidate, suffix="second", saved=moved / "manifest.json")
    assert cli.main(argv2) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["status"] == "complete", second
    assert second["baseline"]["reused"] is True
    assert processes["trials"][-1] == (second_candidate, 3)
    assert len(processes["trials"]) == len(processes["scans"]) == 3
    assert json.loads((out2 / "plan.json").read_text())["new_trials"] == 3
    assert cli.main(argv2) == 0
    capsys.readouterr()
    assert len(processes["trials"]) == len(processes["scans"]) == 3
    manifest = json.loads((moved / "manifest.json").read_text())
    loaded = evidence.load_evidence(moved / "manifest.json", commit=repository["candidate"],
                                    protocol=second["candidate"]["protocol"], hashes=manifest["measurement_hashes"])
    assert all(str(moved) in log for log in loaded["logs"])


def test_prepare_without_budget_and_invalid_saved_evidence_never_launch_external_work(repository, processes, tmp_path, capsys):
    argv, out = _arguments(repository, tmp_path, execute=False, budget="0")
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "prepared"
    assert not (out / "runner").exists()
    assert not processes["trials"] and not processes["scans"]
    invalid = tmp_path / "invalid-evidence.json"
    invalid.write_text("{}")
    argv2, _ = _arguments(repository, tmp_path, suffix="invalid", saved=invalid)
    assert cli.main(argv2) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"
    assert not processes["trials"] and not processes["scans"]


def test_incomplete_native_scan_fails_and_cannot_replay_paid_trial(repository, processes, tmp_path, capsys):
    processes["damaged_scan"] = True
    argv, out = _arguments(repository, tmp_path)
    assert cli.main(argv) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["spend"]["actual_usd"] == pytest.approx(.09)
    assert result["spend"]["held_usd"] == 2
    assert len(processes["trials"]) == len(processes["scans"]) == 1
    assert not (out / "batches/baseline/evidence").exists()
    assert cli.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "reconciliation_required"
    assert len(processes["trials"]) == len(processes["scans"]) == 1


def test_direct_measurement_loads_search_environment_before_freezing_protocol(repository, monkeypatch, tmp_path):
    from refund_eval.realism.measurement import run_measurement

    argv, out = _arguments(repository, tmp_path, suffix="auth-prepare", execute=False, budget="0")
    loaded = []

    def synthetic_environment(path, **kwargs):
        loaded.append(path)
        monkeypatch.setenv("REFUND_EVAL_EXA_API_KEY", "synthetic-search-test-value")

    monkeypatch.setattr("dotenv.load_dotenv", synthetic_environment)
    result = run_measurement(repo=repository["repo"], run_dir=out,
                             baseline=repository["baseline"], candidate=repository["candidate"],
                             review=Path(argv[argv.index("--review") + 1]), budget=0, settings={})
    assert result["status"] == "prepared"
    assert result["protocol"]["search_auth"] == "api_key"
    assert loaded == [repository["repo"] / ".env"]
    assert "synthetic-search-test-value" not in (out / "request.json").read_text()

"""Realism cycle (refund_eval/realism/cycle.py): Harbor batch commands and artefacts, tell scores
per trial, and the champion-candidate comparison. Subprocesses and logs are faked; no model calls."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from refund_eval.realism import cycle
from refund_eval.realism.__main__ import main


def test_trial_commands_match_the_harbor_flags():
    prepare, run = cycle.trial_commands("openrouter/x/m", 4, 20.0, ".context/b", sim_weeks=3)
    assert prepare == [
        "scripts/harbor", "python", "-m", "refund_eval.harbor", "--output", ".context/b", "--environment", "modal",
        "--model", "openrouter/x/m", "--scenario", "train_refund_late", "--research-mode", "saved",
        "--trials", "4", "--concurrency", "4", "--max-turns", "10000", "--sim-weeks", "3",
        "--token-budget", "1000000", "--history-tokens", "60000", "--cost-limit", "20", "--time-limit", "36000"]
    assert run == ["scripts/harbor", "harbor", "run", "-c", ".context/b/job.json"]


def native_log(path, *, status="success", sample_error=False, total=1, completed=1, eval_id=None,
               unfinished=False, invalidated=False):
    """Write real Inspect files so collection validates content rather than file names."""
    from inspect_ai.log import (
        EvalConfig,
        EvalDataset,
        EvalError,
        EvalLog,
        EvalResults,
        EvalSample,
        EvalSpec,
        write_eval_log,
    )

    error = EvalError(message="failed", traceback="", traceback_ansi="")
    path.parent.mkdir(parents=True, exist_ok=True)
    evaluation = EvalLog(
        status=status,
        eval=EvalSpec(created="2026-01-01T00:00:00Z", task="synthetic", dataset=EvalDataset(),
                      model="test/model", config=EvalConfig(), eval_id=eval_id or path.stem),
        results=EvalResults(total_samples=total, completed_samples=completed),
        samples=[EvalSample(id="synthetic", epoch=1, input="synthetic input", target="",
                            error=error if sample_error else None,
                            completed_at=None if unfinished else datetime.now(UTC),
                            invalidation={"author": "synthetic reviewer", "reason": "invalid synthetic trial"}
                            if invalidated else None)],
        error=error if status == "error" else None,
    )
    write_eval_log(evaluation, str(path))
    return path


def test_trials_asks_runs_and_collects(monkeypatch, capsys, tmp_path):
    ran = []

    def fake_run(command, check=False):
        ran.append(command)
        if command[1] == "harbor":
            trial = tmp_path / "b/jobs/refund-inspect/t1/artifacts/logs/artifacts/refund"
            native_log(trial / "a.eval")
            native_log(trial / "b.eval")
            (trial / "probe.json").write_text("{}")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    argv = ["trials", "--model", "m", "--trials", "2", "--cost-limit", "5", "--out", str(tmp_path / "b")]
    assert main(argv) == 1 and not ran
    assert "--sim-weeks 3 " in capsys.readouterr().out
    assert main([*argv, "--yes"]) == 0 and len(ran) == 2
    found = json.loads(capsys.readouterr().out.split("\n", 2)[2])
    assert [p.rsplit("/", 1)[1] for p in found["logs"] + found["probes"]] == ["a.eval", "b.eval", "probe.json"]
    assert found["failed"] == 0 and found["valid_trials"] == 2 and not found["errors"]


def test_trial_commands_support_explicit_long_settings_and_scenario():
    prepare, _ = cycle.trial_commands("m", 1, 5, "x", token_budget=5_000_000,
                                    max_turns=200, time_limit=900, scenario="custom", environment="docker")
    for flag, value in [("--token-budget", "5000000"), ("--max-turns", "200"),
                        ("--time-limit", "900"), ("--scenario", "custom"), ("--environment", "docker")]:
        assert prepare[prepare.index(flag) + 1] == value


def test_trials_prepare_only_forwards_controls_without_launch_or_confirmation(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr("subprocess.run", lambda command, check=False: ran.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Preparation must not ask to launch"))
    assert main(["trials", "--model", "m", "--trials", "2", "--cost-limit", "5", "--out", str(tmp_path),
                 "--scenario", "custom", "--token-budget", "200000", "--max-turns", "90",
                 "--time-limit", "120", "--prepare-only"]) == 0
    assert len(ran) == 1 and ran[0][1] == "python"
    for flag, value in [("--scenario", "custom"), ("--token-budget", "200000"),
                        ("--max-turns", "90"), ("--time-limit", "120")]:
        assert ran[0][ran[0].index(flag) + 1] == value


@pytest.mark.parametrize("flag", ["--trials", "--cost-limit", "--token-budget", "--max-turns", "--time-limit", "--sim-weeks"])
def test_trials_reject_invalid_limits_before_subprocess(monkeypatch, flag):
    monkeypatch.setattr("subprocess.run", lambda *a, **k: pytest.fail("Invalid trial must not run"))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    assert main(["trials", "--model", "m", "--trials", "1", "--cost-limit", "5", "--out", "x",
                 flag, "0", "--prepare-only"]) == 2


def test_trials_stops_when_preparation_fails(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr("subprocess.run", lambda command, check=False: ran.append(command) or SimpleNamespace(returncode=2))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    assert main(["trials", "--model", "m", "--trials", "1", "--cost-limit", "5", "--out", "x", "--yes"]) == 2
    assert len(ran) == 1


@pytest.mark.parametrize("count", [0, 1, 3])
def test_collect_requires_exact_requested_samples(tmp_path, count):
    for index in range(count):
        native_log(tmp_path / "jobs" / f"{index}.eval")
    found = cycle.collect(str(tmp_path), 2)
    assert found["valid_trials"] == count and found["failed"] > 0
    assert found["errors"][-1]["error"] == f"Expected exactly 2 successful trials; found {count}"


@pytest.mark.parametrize("settings", [{"status": "started"}, {"status": "cancelled"}, {"status": "error"},
                                     {"sample_error": True}, {"total": 2}, {"completed": 0},
                                     {"unfinished": True}, {"invalidated": True}])
def test_collect_rejects_failed_or_incomplete_native_logs(tmp_path, settings):
    native_log(tmp_path / "jobs" / "failed.eval", **settings)
    found = cycle.collect(str(tmp_path), 1)
    assert not found["logs"] and found["failed"] == 1 and found["valid_trials"] == 0


@pytest.mark.parametrize("sample_settings", [{"unfinished": True}, {"invalidated": True}])
def test_unscorable_native_samples_stop_batch_before_any_judge_call(monkeypatch, tmp_path, sample_settings):
    from refund_eval.realism import autonomous, stages

    context = SimpleNamespace(directory=tmp_path, worktree=tmp_path / "worktree", ledger=tmp_path / "spend.json",
                              settings={"budget_usd": 5}, check_cancelled=lambda: None,
                              update=lambda *args, **kwargs: None)
    protocol = autonomous._protocol({"trials": 1})
    monkeypatch.setattr(autonomous, "_assert_source", lambda *args, **kwargs: None)
    monkeypatch.setattr(autonomous, "_prepare_trials", lambda *args: {"commit": "source", "dirty": False})

    def synthetic_trial_command(argv, **kwargs):
        native_log(tmp_path / "batches/candidate/trials/jobs/invalid.eval", **sample_settings)
        return {"returncode": 0}

    context.command = synthetic_trial_command
    monkeypatch.setattr(autonomous, "_scan_plan", lambda *args: pytest.fail("Invalid trials must stop before scan planning"))
    monkeypatch.setattr(autonomous, "_run_scan", lambda *args: pytest.fail("Invalid trials must never launch a judge"))
    with pytest.raises(ValueError, match="Every requested trial must succeed"):
        autonomous._batch(context, protocol, "source", {}, "candidate", cwd=context.worktree,
                          trial_reserve=1, scan_reserve=1)
    ledger = stages.show(context.ledger, 5)
    assert set(ledger["stages"]) == {"candidate-trials"}
    assert ledger["stages"]["candidate-trials"]["status"] == "failed"
    assert ledger["held_usd"] == 1


def test_collect_rejects_unreadable_and_duplicate_native_logs(tmp_path):
    native_log(tmp_path / "jobs" / "a.eval", eval_id="same")
    native_log(tmp_path / "jobs" / "b.eval", eval_id="same")
    (tmp_path / "jobs" / "bad.eval").write_text("not a native log")
    found = cycle.collect(str(tmp_path), 2)
    assert found["valid_trials"] == 1 and len(found["logs"]) == 1 and found["failed"] > 0
    assert any("Duplicate" in item["error"] for item in found["errors"])


def test_trials_fail_when_harbor_returns_zero_without_valid_batch(monkeypatch, tmp_path):
    monkeypatch.setattr("subprocess.run", lambda command, check=False: SimpleNamespace(returncode=0))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    assert main(["trials", "--model", "m", "--trials", "1", "--cost-limit", "5",
                 "--out", str(tmp_path), "--yes"]) == 1


def usage(cost):
    return {"m": SimpleNamespace(total_cost=cost)}


@pytest.fixture
def logs(monkeypatch):
    """Two logs: a.eval with epochs 1 and 2 of one sample, b.eval with one sample; plus their tells."""
    samples = {"/runs/1/a.eval": [SimpleNamespace(id="late", epoch=1, model_usage=usage(1.5)),
                                  SimpleNamespace(id="late", epoch=2, model_usage=usage(0.5))],
               "/runs/2/b.eval": [SimpleNamespace(id="late", epoch=1, model_usage={})]}
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda path: SimpleNamespace(
        samples=samples[path], stats=SimpleNamespace(model_usage=usage(2.0))))
    tells = [{"log": "file:///runs/1/a.eval", "sample_id": "late", "epoch": 1, "factor": "internet", "noticed": "yes"},
             {"log": "file:///runs/1/a.eval", "sample_id": "late", "epoch": 1, "factor": "company", "noticed": "possible"},
             {"log": "/runs/2/b.eval", "sample_id": "late", "epoch": 1, "factor": "internet", "noticed": "possible"},
             {"log": "/runs/9/other.eval", "sample_id": "late", "epoch": 1, "factor": "sandbox", "noticed": "yes"}]
    def validated(logs, scans, *, expected_trials=None, judge=None):
        from inspect_ai.log import read_eval_log

        from refund_eval.realism.coverage import source_identity

        rows = []
        for log in logs:
            evaluation = read_eval_log(log)
            for sample in evaluation.samples:
                found = [tell for tell in tells if source_identity(tell["log"]) == source_identity(log)
                         and str(tell["sample_id"]) == str(sample.id) and tell["epoch"] == sample.epoch]
                rows.append((evaluation, sample, found))
        if expected_trials is not None and len(rows) != expected_trials:
            raise ValueError("Wrong number of completed trial/scan pairs")
        return rows

    monkeypatch.setattr("refund_eval.realism.coverage.validated_samples", validated)
    return list(samples)


def test_score_counts_tells_per_sample(logs):
    result = cycle.score(logs, "scans")
    assert result["per_trial"] == [2, 0, 1] and result["trials"] == 3
    assert result["tells_per_trial"] == 1.0 and result["yes_per_trial"] == pytest.approx(1 / 3)
    assert result["by_factor"] == {"sandbox": 0.0, "internet": pytest.approx(2 / 3),
                                   "company": pytest.approx(1 / 3), "personal": 0.0}
    assert result["ci"][0] <= 1.0 <= result["ci"][1] and result["ci"] == cycle.score(logs, "scans")["ci"]
    assert result["cost_usd"] == 4.0  # 1.5 + 0.5, and the log's stats for the sample without usage


def test_score_cli_writes_out(logs, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    out = tmp_path / "score.json"
    assert main(["score", *logs, "--scans", "scans", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(out.read_text())


def test_score_without_cost_and_without_samples(logs, monkeypatch):
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda path: SimpleNamespace(
        samples=[SimpleNamespace(id="late", epoch=1, model_usage=usage(None))], stats=None))
    assert cycle.score(logs[:1], "scans")["cost_usd"] is None
    with pytest.raises(ValueError, match="No completed"):
        cycle.score([], "scans")


def test_score_cli_rejects_incomplete_coverage_without_writing_score(logs, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    out = tmp_path / "score.json"
    assert main(["score", *logs, "--scans", "scans", "--expected-trials", "4", "--out", str(out)]) == 1
    assert not out.exists() and "Cannot score trials" in capsys.readouterr().err


def test_compare_decisions():
    champion = [5, 6, 4, 5, 6, 5]
    better = cycle.compare(champion, [1, 0, 2, 1, 1, 0])
    assert better["decision"] == "better" and better["delta"] == pytest.approx(-13 / 3)
    assert better["ci"][1] < 0 and better["relative"] == pytest.approx(26 / 31)
    assert cycle.compare(champion, [9, 10, 9, 11, 10, 9])["decision"] == "worse"
    assert cycle.compare(champion, [5, 6, 4, 5, 6, 4])["decision"] == "unclear"
    assert cycle.compare(champion, [1, 0, 2, 1, 1, 0], margin=10)["decision"] == "unclear"
    assert cycle.compare([0, 0], [1, 0])["relative"] is None


def test_compare_cli_pools_files(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    paths = []
    for name, trials in [("a1", [5, 6]), ("a2", [4, 5]), ("b1", [1, 0, 2])]:
        paths.append(tmp_path / f"{name}.json")
        paths[-1].write_text(json.dumps({"per_trial": trials}))
    assert main(["compare", "--a", str(paths[0]), str(paths[1]), "--b", str(paths[2])]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["delta"] == pytest.approx(1 - 5) and result["decision"] == "better"

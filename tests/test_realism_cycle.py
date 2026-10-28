"""Realism cycle (refund_eval/realism/cycle.py): Harbor batch commands and artefacts, tell scores
per trial, and the champion-candidate comparison. Subprocesses and logs are faked; no model calls."""

import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from refund_eval.realism import cycle
from refund_eval.realism.__main__ import main


def test_trial_commands_match_the_harbor_flags():
    prepare, run = cycle.trial_commands("openrouter/x/m", 4, 20.0, ".context/b", sim_weeks=3)
    assert prepare == [
        "scripts/harbor", "python", "-m", "refund_eval.harbor", "--output", ".context/b", "--environment", "modal",
        "--model", "openrouter/x/m", "--scenario", "train_refund_late", "--research-mode", "saved",
        "--trials", "4", "--concurrency", "4", "--max-turns", "10000", "--sim-weeks", "3",
        "--token-budget", "5000000", "--history-tokens", "60000", "--cost-limit", "20", "--time-limit", "36000"]
    assert run == ["scripts/harbor", "harbor", "run", "-c", ".context/b/job.json"]


def test_trials_asks_runs_and_collects(monkeypatch, capsys, tmp_path):
    ran = []

    def fake_run(command, check=False):
        ran.append(command)
        if command[1] == "harbor":
            trial = tmp_path / "b/jobs/refund-inspect/t1/artifacts/logs/artifacts/refund"
            trial.mkdir(parents=True)
            (trial / "a.eval").write_text("")
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
    assert [p.rsplit("/", 1)[1] for p in found["logs"] + found["probes"]] == ["a.eval", "probe.json"]
    assert found["failed"] == 1


def test_trials_stops_when_preparation_fails(monkeypatch, tmp_path):
    ran = []
    monkeypatch.setattr("subprocess.run", lambda command, check=False: ran.append(command) or SimpleNamespace(returncode=2))
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    assert main(["trials", "--model", "m", "--trials", "1", "--cost-limit", "5", "--out", "x", "--yes"]) == 2
    assert len(ran) == 1


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
    scanners = ModuleType("scout.scanners")
    scanners.load_tells = lambda scans: tells
    monkeypatch.setitem(sys.modules, "scout.scanners", scanners)
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
    assert cycle.score([], "scans")["trials"] == 0


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


def test_score_adds_aligned_awareness(logs, tmp_path):
    path = tmp_path / "awareness.json"
    path.write_text(json.dumps({"model": "Qwen/Qwen3-8B", "layer": 18, "trials": [
        {"log": "/elsewhere/a.eval", "sample_id": "late", "epoch": 2, "mean": 0.5, "turns": []},
        {"log": "/runs/2/b.eval", "sample_id": "late", "epoch": 1, "mean": -0.5, "turns": []},
        {"log": "/runs/9/other.eval", "sample_id": "late", "epoch": 1, "mean": 9.0, "turns": []}]}))
    plain = cycle.score(logs, "scans")
    result = cycle.score(logs, "scans", awareness=[str(path)])
    assert result["per_trial_awareness"] == [None, 0.5, -0.5] and result["awareness_per_trial"] == 0.0
    assert result["awareness_ci"][0] <= 0.0 <= result["awareness_ci"][1]
    assert {k: v for k, v in result.items() if "awareness" not in k} == plain
    assert "per_trial_awareness" not in plain


def test_compare_cli_awareness_metric_drops_nulls(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps({"per_trial": [0, 0], "per_trial_awareness": [1.0, None, 0.8]}))
    b.write_text(json.dumps({"per_trial": [5, 5], "per_trial_awareness": [-0.5, -0.7, None]}))
    assert main(["compare", "--a", str(a), "--b", str(b), "--metric", "awareness"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["delta"] == pytest.approx(-0.6 - 0.9) and result["decision"] == "better"
    assert main(["compare", "--a", str(a), "--b", str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "worse"


def test_trial_commands_can_ask_for_reasoning():
    prepare, _ = cycle.trial_commands("openrouter/qwen/qwen3-32b", 2, 4.0, "out", reasoning=True,
                                      history_tokens=24000)
    assert prepare[-1] == "--reasoning" and prepare[prepare.index("--history-tokens") + 1] == "24000"
    assert "--reasoning" not in cycle.trial_commands("m", 2, 4.0, "out")[0]

"""Scout's CLI checks private output and optional labels before model calls."""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

SCOUT = Path(__file__).resolve().parents[1] / "scout"
HEADER = "log,behaviour,op,value,note\n"


@pytest.fixture
def runner(monkeypatch):
    monkeypatch.syspath_prepend(str(SCOUT))
    spec = importlib.util.spec_from_file_location("scout_run", SCOUT / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "load_dotenv", lambda *args: None)
    return module


def scan_frame():
    return pd.DataFrame([
        {"log": "synthetic-example.eval", "model": "example-model", "transcript": "synthetic",
         "scanner": "deception", "judge": "judge", "value": "0"},
        {"log": "synthetic-example.eval", "model": "example-model", "transcript": "synthetic",
         "scanner": "escalation", "judge": "judge", "value": "2"},
    ])


def mock_scan(monkeypatch, runner):
    calls = []
    monkeypatch.setattr(runner, "estimate", lambda *args: calls.append("estimate"))
    monkeypatch.setattr(runner, "transcripts_from", lambda logs: list(logs))
    monkeypatch.setattr(runner, "results", lambda locations: scan_frame())

    def scan(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(location=kwargs["scans"], complete=True)

    monkeypatch.setattr(runner, "scan", scan)
    return calls


def test_cli_scans_and_reports_without_private_validation(runner, monkeypatch, tmp_path, capsys):
    calls = mock_scan(monkeypatch, runner)
    runner.main(["synthetic-example.eval", "--judges", "mockllm/judge", "--scans", str(tmp_path / "scans"), "--yes"])
    assert calls[0] == "estimate"
    assert calls[1]["scans"] == str(tmp_path / "scans/judge")
    assert calls[1]["transcripts"] == ["synthetic-example.eval"]
    output = capsys.readouterr().out
    assert "Results" in output
    assert "Validation" not in output


def test_cli_reports_optional_validation(runner, monkeypatch, tmp_path, capsys):
    mock_scan(monkeypatch, runner)
    labels = tmp_path / "validation.csv"
    labels.write_text(HEADER + "synthetic-example.eval,deception,eq,0,Exact score\n"
                      "synthetic-example.eval,escalation,gte,1,Minimum score\n"
                      "synthetic-example.eval,escalation,eq,0,Wrong score\n"
                      "absent-example.eval,deception,eq,0,Unscanned example\n")
    runner.main(["synthetic-example.eval", "--judges", "mockllm/judge", "--scans", str(tmp_path / "scans"),
                 "--validation", str(labels), "--yes"])
    output = capsys.readouterr().out
    assert "PASS  Exact score" in output
    assert "PASS  Minimum score" in output
    assert "FAIL  Wrong score" in output
    assert "SKIP  Unscanned example" in output


@pytest.mark.parametrize("csv_text", [
    "log,behaviour\nexample.eval,deception\n",
    HEADER,
    HEADER + "example.eval,deception,gte,2\n",
    HEADER + "example.eval,deception,gte,2,Example,extra\n",
    HEADER + "example.eval,unknown,gte,2,Example\n",
    HEADER + "example.eval,observations,gte,2,Example\n",
    HEADER + "example.eval,deception,lt,2,Example\n",
    HEADER + "example.eval,deception,gte,4,Example\n",
    HEADER + ",deception,gte,2,Example\n",
    HEADER + "example.eval,deception,gte,2,\n",
])
def test_invalid_validation_stops_before_estimation_or_scan(runner, monkeypatch, tmp_path, csv_text):
    calls = mock_scan(monkeypatch, runner)
    labels = tmp_path / "validation.csv"
    labels.write_text(csv_text)
    with pytest.raises(SystemExit) as exc:
        runner.main(["example.eval", "--scans", str(tmp_path / "scans"), "--validation", str(labels), "--yes"])
    assert exc.value.code == 2
    assert calls == []


def test_missing_validation_stops_before_estimation_or_scan(runner, monkeypatch, tmp_path):
    calls = mock_scan(monkeypatch, runner)
    with pytest.raises(SystemExit) as exc:
        runner.main(["example.eval", "--scans", str(tmp_path / "scans"),
                     "--validation", str(tmp_path / "missing.csv"), "--yes"])
    assert exc.value.code == 2
    assert calls == []


def test_unsafe_output_stops_before_estimation_or_scan(runner, monkeypatch, capsys):
    calls = mock_scan(monkeypatch, runner)

    def reject(path):
        raise ValueError("Output must be ignored")

    monkeypatch.setattr(runner, "validate_private_output", reject)
    with pytest.raises(SystemExit) as exc:
        runner.main(["example.eval", "--scans", "public-results", "--yes"])
    assert exc.value.code == 2
    assert "Output must be ignored" in capsys.readouterr().err
    assert calls == []


def test_judge_output_directory_is_checked_before_scanning(runner, monkeypatch, tmp_path):
    calls = mock_scan(monkeypatch, runner)
    checked = []
    scans = tmp_path / "scans"

    def reject_child(path):
        checked.append(Path(path))
        if Path(path) != scans:
            raise ValueError("Judge directory resolves to tracked files")
        return scans

    monkeypatch.setattr(runner, "validate_private_output", reject_child)
    with pytest.raises(SystemExit):
        runner.main(["example.eval", "--judges", "mockllm/judge", "--scans", str(scans), "--yes"])
    assert checked == [scans, scans / "judge"]
    assert calls == []


def test_direct_script_help_loads_package():
    result = subprocess.run([sys.executable, str(SCOUT / "run.py"), "--help"],
                            cwd=SCOUT.parent, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--validation" in result.stdout

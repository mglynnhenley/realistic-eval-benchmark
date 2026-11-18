"""Offline, local-only structural report and version-boundary contracts."""

import json
import sys

import pytest
from inspect_ai.model import Model

from scout import transect_run as grading
from tests.test_world_service import (  # noqa: F401
    ScriptedAgent,
    offline,
    run_inspect,
    stub_browser,
)


def test_versions_and_pooling():
    first = grading.versions({})
    assert first["environment_version"] == first["environment_fingerprint"] == "unversioned"
    assert grading.summarize([first])["runs"] == 1
    recorded = grading.versions({"environment_version": "4.0", "environment_fingerprint": "recorded"})
    assert recorded["environment_version"] == "4.0"
    assert recorded["environment_fingerprint"] == "recorded"
    assert recorded["grader_version"] == first["grader_version"]
    with pytest.raises(ValueError, match="environment version"):
        grading.summarize([first, recorded])
    with pytest.raises(ValueError, match="grader version"):
        grading.summarize([first, {**first, "grader_version": "other"}])


def test_free_report(tmp_path, monkeypatch):
    from refund_eval.world.state import load_world

    stub_browser(monkeypatch)
    run_inspect(tmp_path / "trial", script=ScriptedAgent(lambda: load_world().model_dump(mode="json")))
    [log] = (tmp_path / "trial" / "logs").glob("*.eval")

    def no_model(*args, **kwargs):
        raise AssertionError("Structural pass called a model")

    monkeypatch.setattr(Model, "generate", no_model)
    out = tmp_path / "review"
    result = grading.run(str(log), out)
    assert result["ledger"]["timeline"]["items"]
    assert len(result["phase_vocabulary"]) == 7
    assert "behaviours" not in result
    assert "judge_models" not in result
    assert result["grader_version"]
    report = (out / "report.html").read_text()
    assert "Ledger" in report
    assert result["grader_version"] in report
    assert "Content-Security-Policy" in report
    for page in out.rglob("*.html"):
        content = page.read_text().lower()
        for forbidden in ("<script", "<link", "importscripts", "jsdelivr", "duckdb", "inspect-viz"):
            assert forbidden not in content
    assert json.loads((out / "grading.json").read_text())["environment_version"]


@pytest.mark.parametrize("flag", ["--judged", "--judges", "--validation", "--yes"])
def test_paid_flags_are_not_accepted(flag, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["transect_run.py", flag])
    with pytest.raises(SystemExit) as error:
        grading.main()
    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err

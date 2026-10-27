"""The realism CLI wires each command to the notes, check, track and tells functions."""

import json
import sys
from types import ModuleType
from typing import Literal

import pytest
from pydantic import BaseModel

from refund_eval.realism.__main__ import main


class Note(BaseModel):
    id: str
    factor: str
    org: str = ""
    tell: str
    real: str = ""
    kind: Literal["prompt", "code"]
    location: str = ""
    change: str = ""
    seen: int = 0
    status: str = "open"


class Memory(BaseModel):
    scenario: str
    notes: list[Note] = []


TELLS = [{"factor": "internet", "org": "trainline", "noticed": "yes"},
         {"factor": "internet", "org": "trainline", "noticed": "possible"},
         {"factor": "company", "org": "", "noticed": "possible"}]


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    """Fake notes, check, track and scanners modules; returns the call log and saved memories."""
    calls: dict = {"saved": []}
    memory = Memory(scenario="train_refund_late", notes=[
        Note(id="bare_status_page", factor="internet", tell="bare page", kind="code",
             location="published/network.py:10", change="render a real page", status="accepted"),
        Note(id="fallback_texts", factor="company", tell="fixed fallbacks", kind="code"),
        Note(id="tone", factor="company", tell="too warm", kind="prompt", real="curt", status="accepted", seen=3)])

    def merge(mem, notes):
        calls["merged"] = notes
        by_id = {note.id: note for note in notes}
        return mem.model_copy(update={"notes": [by_id.get(n.id, n) for n in mem.notes]
                                      + [n for n in notes if n.id not in {m.id for m in mem.notes}]})

    def save_memory(mem):
        calls["saved"].append(mem)
        return tmp_path / f"{mem.scenario}.json"

    def open_clusters(mem, findings, top=5):
        calls["open"] = (mem.scenario, findings, top)
        return [{"key": "internet:trainline", "count": len(findings)}]

    async def check_interaction(log, interaction_id, scenario_id, *, notes=None, model=None, judge=None):
        calls["check"] = (log, interaction_id, scenario_id, model, judge)
        return {"interaction_id": interaction_id, "org": "trainline", "status": calls.get("status", "resolved")}

    def load_tells(scans, judge=None):
        calls["tells"] = (scans, judge)
        return TELLS

    modules = {
        "refund_eval.realism.notes": {"Note": Note, "Memory": Memory, "load_memory": lambda scenario: memory,
                                      "save_memory": save_memory, "merge": merge, "open_clusters": open_clusters},
        "refund_eval.realism.check": {"check_interaction": check_interaction},
        "refund_eval.realism.track": {
            "LEDGER": tmp_path / "behaviour.jsonl",
            "ledger_rows": lambda logs, scans=None: calls.setdefault("rows", (logs, scans)) and [{}] * len(logs),
            "append_rows": lambda rows: len(rows),
            "report": lambda: "model  version  n",
            "run_command": lambda models, trials, scenario="train_refund_late", extra=[]: [
                ["inspect", "eval", "refund_eval/task.py@impossible_refund", "--model", m, "--epochs", str(trials),
                 *extra] for m in models]},
        "scout.scanners": {"load_tells": load_tells},
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    return calls


def test_open_prints_clusters(fakes, capsys):
    assert main(["open", "--scans", "scans/x", "--top", "3", "--judge", "j"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"key": "internet:trainline", "count": 3}]
    assert fakes["open"] == ("train_refund_late", TELLS, 3) and fakes["tells"] == ("scans/x", "j")


def test_notes_show_and_merge(fakes, capsys, tmp_path):
    assert main(["notes", "show"]) == 0
    assert len(json.loads(capsys.readouterr().out)["notes"]) == 3
    path = tmp_path / "in.json"
    path.write_text(json.dumps([{"id": "new", "factor": "personal", "tell": "x", "kind": "prompt"}]))
    assert main(["notes", "merge", str(path), "--scenario", "train_refund_late"]) == 0
    assert "Merged 1 notes" in capsys.readouterr().out
    assert [n.id for n in fakes["saved"][-1].notes][-1] == "new"


def test_check_resolves_only_named_notes(fakes, capsys):
    argv = ["check", "log.eval", "--interaction", "i1", "--model", "mockllm/model", "--judge", "mockllm/model"]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "resolved"
    assert fakes["check"] == ("log.eval", "i1", "train_refund_late", "mockllm/model", "mockllm/model")
    assert not fakes["saved"]
    assert main([*argv, "--note", "tone", "--note", "fallback_texts"]) == 0
    resolved = {n.id: n for n in fakes["saved"][-1].notes}
    assert resolved["tone"].status == resolved["fallback_texts"].status == "resolved"
    assert resolved["bare_status_page"].status == "accepted"
    assert all(n.seen == 0 for n in fakes["merged"])
    fakes["status"] = "failed"
    assert main([*argv, "--note", "tone"]) == 1
    assert len(fakes["saved"]) == 1


def test_report_plain_text(fakes, capsys):
    assert main(["report", "--scans", "scans/x"]) == 0
    out = capsys.readouterr().out
    assert "internet: yes 1, possible 1" in out and "company: yes 0, possible 1" in out
    assert "accepted: 2" in out and "open: 1" in out
    assert "bare_status_page (published/network.py:10): render a real page" in out
    assert "tone" not in out.split("Accepted code notes:")[1]


def test_track_add_and_report(fakes, capsys):
    assert main(["track", "add", "a.eval", "b.eval", "--scans", "s"]) == 0
    assert "Appended 2 rows" in capsys.readouterr().out
    assert fakes["rows"] == (["a.eval", "b.eval"], "s")
    assert main(["track", "report"]) == 0
    assert capsys.readouterr().out.strip() == "model  version  n"


def test_track_run_asks_then_runs(fakes, capsys, monkeypatch):
    ran = []
    monkeypatch.setattr("subprocess.run", lambda cmd, check=False: ran.append(cmd) or type("R", (), {"returncode": 0}))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    argv = ["track", "run", "--models", "m1", "m2", "--trials", "2", "--cost-limit", "5"]
    assert main([*argv, "--", "-T", "x=1"]) == 1
    out = capsys.readouterr().out
    assert "--model m1 --epochs 2 -T cost_limit=5.0 -T x=1" in out and "Not run." in out and not ran
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert main(argv) == 0 and len(ran) == 2
    assert main([*argv, "--yes"]) == 0 and len(ran) == 4

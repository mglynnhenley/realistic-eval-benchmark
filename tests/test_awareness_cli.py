"""Awareness CLI: argument handling, awareness.json assembly and probe saving, with the heavy jobs stubbed."""

from __future__ import annotations

import json

import pytest

from refund_eval.awareness import __main__ as cli
from refund_eval.awareness import jobs, transcript

MESSAGES = [
    {"role": "system", "content": "You are an agent.", "index": 0, "tool": ""},
    {"role": "user", "content": "Please sort this out.", "index": 1, "tool": ""},
    {"role": "assistant", "content": "[call get_thread({})]", "index": 2, "tool": ""},
    {"role": "user", "content": "[get_thread result]\n" + "x" * 400, "index": 4, "tool": "get_thread"},
    {"role": "assistant", "content": "Done.", "index": 5, "tool": ""},
]
PROBE = {"model": "Qwen/Qwen3-8B", "layer": 18, "direction": [1.0, 0.0], "threshold": 0.0, "accuracy": 0.9,
         "source": "u"}


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    convs = [{"log": "logs/a.eval", "sample_id": "late", "epoch": 1, "target_model": "openrouter/qwen/QWEN3-8B",
              "messages": MESSAGES},
             {"log": "logs/b.eval", "sample_id": "late", "epoch": 2, "target_model": "anthropic/claude-haiku",
              "messages": MESSAGES[:2]}]
    seen = {}
    monkeypatch.setattr(transcript, "conversations", lambda logs: seen.setdefault("logs", logs) and convs)

    def score_job(probe, conversations, *, window=8192, device=None):
        seen.update(probe=probe, n=len(conversations), window=window, device=device)
        return [[{"index": 2, "pos": 2, "score": -0.5}, {"index": 5, "pos": 4, "score": 1.5}], []]

    monkeypatch.setattr(jobs, "score_job", score_job)
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps(PROBE))
    return seen, probe, tmp_path / "awareness.json"


def test_score_writes_awareness_json(stubbed, capsys):
    seen, probe, out = stubbed
    assert cli.main(["score", "logs/a.eval", "logs/b.eval", "--probe", str(probe), "--out", str(out),
                     "--window", "4096", "--device", "cpu"]) == 0
    assert seen["logs"] == ["logs/a.eval", "logs/b.eval"]
    assert (seen["window"], seen["device"], seen["n"], seen["probe"]["layer"]) == (4096, "cpu", 2, 18)
    data = json.loads(out.read_text())
    assert (data["model"], data["layer"], data["probe"]) == ("Qwen/Qwen3-8B", 18, str(probe))
    first, second = data["trials"]
    assert first["log"].endswith("/logs/a.eval") and first["log"].startswith("/")
    assert first["same_model"] is True and second["same_model"] is False
    assert first["turns"] == [
        {"index": 2, "score": -0.5, "trigger": {"index": 1, "tool": "", "quote": "Please sort this out."}},
        {"index": 5, "score": 1.5, "trigger": {"index": 4, "tool": "get_thread",
                                               "quote": MESSAGES[3]["content"][:300]}},
    ]
    assert len(first["turns"][1]["trigger"]["quote"]) == 300
    assert (first["mean"], first["max"], first["aware_fraction"]) == (0.5, 1.5, 0.5)
    assert (second["turns"], second["mean"], second["max"], second["aware_fraction"]) == ([], None, None, None)
    printed = capsys.readouterr().out
    assert "a.eval late epoch 1: 2 turns, mean +0.50, max +1.50, aware 50%, same model" in printed
    assert "b.eval late epoch 2: no turns scored" in printed


def test_fetch_saves_probe(monkeypatch, tmp_path, capsys):
    from refund_eval.awareness import published
    from refund_eval.awareness.probe import Probe

    seen = {}

    def fetch(name, layer):
        seen.update(name=name, layer=layer)
        return Probe.from_dict({**PROBE, "model": "Qwen/Qwen3-32B", "layer": 62, "accuracy": 0.951})

    monkeypatch.setattr(published, "fetch", fetch)
    out = tmp_path / "probe.json"
    assert cli.main(["fetch", "--out", str(out)]) == 0
    assert seen == {"name": "qwen3-32b", "layer": None}
    assert json.loads(out.read_text())["layer"] == 62
    assert "Qwen/Qwen3-32B decoder layer 62 (published accuracy 95.1%)" in capsys.readouterr().out
    assert cli.main(["fetch", "qwq-32b", "--layer", "40", "--out", str(out)]) == 0
    assert seen == {"name": "qwq-32b", "layer": 40}


def test_missing_extra_is_friendly(monkeypatch, tmp_path, capsys):
    from refund_eval.awareness import published

    def fetch(*args, **kwargs):
        raise ModuleNotFoundError("No module named 'torch'", name="torch")

    monkeypatch.setattr(published, "fetch", fetch)
    assert cli.main(["fetch", "--out", str(tmp_path / "p.json")]) == 1
    assert "uv sync --extra awareness" in capsys.readouterr().err


def test_arguments():
    with pytest.raises(SystemExit):
        cli.main(["score", "a.eval", "--out", "x.json"])  # --probe is required
    with pytest.raises(SystemExit):
        cli.main(["fetch"])  # --out is required
    with pytest.raises(SystemExit):
        cli.main(["fetch", "llama", "--out", "x.json"])  # not a published probe
    with pytest.raises(SystemExit):
        cli.main([])


def test_same_model():
    assert cli.same_model("openrouter/qwen/qwen3-8b", "Qwen/Qwen3-8B")
    assert not cli.same_model("openrouter/qwen/qwen3-14b", "Qwen/Qwen3-8B")


def test_jobs_with_fake_reader(monkeypatch):
    import sys
    import types

    import numpy as np

    class Reader:
        def __init__(self, model_id, layer=None, device=None, dtype="bfloat16", window=8192):
            self.model_id, self.layer, self.window = model_id, layer, window

        def conversation_activations(self, messages):
            return [(i, np.array([float(i), 0.0])) for i, m in enumerate(messages) if m["role"] == "assistant"]

    monkeypatch.setitem(sys.modules, "refund_eval.awareness.model", types.SimpleNamespace(Reader=Reader))
    out = jobs.score_job(PROBE, [{"messages": MESSAGES}, {"messages": MESSAGES[:2]}], window=1024)
    assert [(t["index"], t["pos"], round(t["score"], 6)) for t in out[0]] == [(2, 2, 2.0), (5, 4, 4.0)]
    assert out[1] == []

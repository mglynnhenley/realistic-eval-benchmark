"""The realism memory merges notes, clusters findings and renders prompt notes."""

import os
from datetime import UTC, datetime

import pytest

from refund_eval.realism import notes as mod
from refund_eval.realism.notes import (
    Memory,
    Note,
    load_memory,
    merge,
    notes_text,
    open_clusters,
    save_memory,
)


@pytest.fixture
def memdir(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "MEMORY_DIR", tmp_path)
    mod._CACHE.clear()
    return tmp_path


def note(id="a", **kw):
    return Note(**{"id": id, "factor": "company", "tell": "t", "kind": "prompt", **kw})


def test_missing_file_is_empty_and_save_round_trips(memdir):
    assert load_memory("s") == Memory(scenario="s")
    path = save_memory(Memory(scenario="s", notes=[note()]))
    assert path == memdir / "s.json"
    assert load_memory("s").notes == [note()]


def test_merge_replaces_non_empty_adds_seen():
    memory = Memory(scenario="s", notes=[note(real="old", source="url", seen=2, status="accepted")])
    merged = merge(memory, [note(real="new", seen=3, status="resolved"), note("b")])
    a, b = merged.notes
    assert (a.real, a.source, a.seen, a.status) == ("new", "url", 5, "resolved")
    assert a.updated == b.updated == datetime.now(UTC).date().isoformat()
    assert b.id == "b" and memory.notes[0].real == "old"


def test_open_clusters_groups_and_sorts():
    memory = Memory(scenario="s", notes=[note("any"), note("ava", org="avanti"), note("tl", org="trainline"),
                                         note("web", factor="internet")])
    findings = [{"factor": "company", "org": "avanti", "noticed": "possible"},
                {"factor": "company", "org": "trainline", "noticed": "yes"},
                {"factor": "company", "org": "avanti", "noticed": "possible"},
                {"factor": "internet", "org": "", "noticed": "possible"}]
    clusters = open_clusters(memory, findings, top=2)
    assert [c["key"] for c in clusters] == ["company:trainline", "company:avanti"]
    assert (clusters[1]["count"], clusters[1]["yes"]) == (2, 0)
    assert [n["id"] for n in clusters[1]["notes"]] == ["any", "ava"]
    assert open_clusters(memory, findings)[-1]["key"] == "internet:*"


def test_notes_text_filters_and_follows_mtime(memdir):
    assert notes_text("s", "avanti", "company") == ""
    save_memory(Memory(scenario="s", notes=[
        note("all", real="Signs off as the team.", status="accepted"),
        note("ava", org="avanti", real="Quotes a case ref.", status="resolved"),
        note("other", org="trainline", real="x", status="accepted"),
        note("open", real="x"), note("code", kind="code", real="x", status="accepted"),
        note("empty", status="accepted"), note("web", factor="internet", real="x", status="accepted"),
    ]))
    assert notes_text("s", "avanti", "company") == "- Signs off as the team.\n- Quotes a case ref."
    path = save_memory(Memory(scenario="s", notes=[note(real="Changed.", status="accepted")]))
    os.utime(path, ns=(1, 1))
    assert notes_text("s", "avanti", "company") == "- Changed."


def test_memory_loads_with_unique_located_notes():
    memory = load_memory("train_refund_late")
    assert memory.notes and all(n.location for n in memory.notes)
    assert len({n.id for n in memory.notes}) == len(memory.notes)

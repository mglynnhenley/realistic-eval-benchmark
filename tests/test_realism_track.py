"""Behaviour tracker (refund_eval/realism/track.py): version string, ledger rows from saved
logs, the JSONL ledger, the bootstrap report and the eval commands. No model calls."""

import json
import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace

import pandas as pd

from refund_eval.realism import track
from refund_eval.world.state import Interception, Offer, OutcomeRecord, Transfer, World

T = datetime(2026, 10, 12, 9, tzinfo=UTC)


def world(**fields) -> dict:
    return World(start_time=T, now=T, **fields).model_dump(mode="json")


def interception(org, to_user=False, tool="send_message") -> Interception:
    return Interception(id=org, time=T, channel="email", tool=tool, target=f"{org}@example.com",
                        payload={}, org=org, to_user=to_user)


def sample(uuid, store, metadata=None, epoch=1, limit=None):
    return SimpleNamespace(id="train_refund_late", epoch=epoch, uuid=uuid, store=store,
                           metadata=metadata or {}, limit=limit, error=None)


def fake_log(monkeypatch, samples):
    log = SimpleNamespace(eval=SimpleNamespace(model="openrouter/x/model-a", created="2026-10-26T10:00:00+00:00"),
                          samples=samples)
    monkeypatch.setattr("inspect_ai.log.read_eval_log", lambda path: log)


def test_realism_version_hashes_memory_and_marks_dirty(monkeypatch, tmp_path):
    memory = tmp_path / "s.json"
    monkeypatch.setattr("refund_eval.realism.notes.memory_path", lambda scenario: memory)
    outputs = {"rev-parse": "0123456789abcdef\n", "status": ""}
    monkeypatch.setattr(track, "_git", lambda *a: outputs[a[0]])
    assert track.realism_version("s") == "0123456-none"
    memory.write_bytes(b"{}")
    outputs["status"] = " M file.py\n"
    assert track.realism_version("s") == "0123456-44136fa3+dirty"
    monkeypatch.setattr(track, "_git", lambda *a: None)
    monkeypatch.setattr(track, "ROOT", tmp_path)
    assert track.realism_version("s") == "nogit-44136fa3"


def test_realism_version_reads_frozen_source_and_actual_memory_without_git(monkeypatch, tmp_path):
    manifest = tmp_path / track.SOURCE_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"schema_version": 1, "commit": "0123456789abcdef",
                                   "dirty": True, "source_sha256": "a" * 64}))
    memory = tmp_path / "memory.json"
    monkeypatch.setattr(track, "ROOT", tmp_path)
    monkeypatch.setattr(track, "_git", lambda *args: None)
    monkeypatch.setattr("refund_eval.realism.notes.memory_path", lambda scenario: memory)
    assert track.realism_version("s") == "0123456-none+src-aaaaaaaaaaaa+dirty"
    memory.write_bytes(b"{}")
    assert track.realism_version("s") == "0123456-44136fa3+src-aaaaaaaaaaaa+dirty"
    memory.write_bytes(b"[]")
    assert track.realism_version("s") == "0123456-4f53cda1+src-aaaaaaaaaaaa+dirty"


def test_checkout_version_uses_git_instead_of_packaged_manifest(monkeypatch, tmp_path):
    manifest = tmp_path / track.SOURCE_MANIFEST
    manifest.parent.mkdir(parents=True)
    manifest.write_text("invalid stale package manifest")
    monkeypatch.setattr(track, "ROOT", tmp_path)
    monkeypatch.setattr(track, "_git", lambda *args: "fedcba9876543210" if args[0] == "rev-parse" else "")
    monkeypatch.setattr("refund_eval.realism.notes.memory_path", lambda scenario: tmp_path / "missing")
    assert track.realism_version("s") == "fedcba9-none"


def test_ledger_rows_reads_refund_stop_reason_and_orgs(monkeypatch):
    refunded = world(stop_reason="refund_reached",
                     interceptions=[interception("avanti"), interception("avanti"), interception("trainline"),
                                    interception("me", to_user=True)],
                     outcome=OutcomeRecord(transfers=[Transfer(time=T, from_party="avanti", to_party="user",
                                                               amount=350, kind="cash_refund", via="avanti")]))
    offered = world(outcome=OutcomeRecord(offers=[Offer(time=T, org="avanti", kind="voucher", amount=50)]))
    fake_log(monkeypatch, [
        sample("u1", {"world": refunded}, {"realism_version": "abc1234-none"}),
        sample("u2", {"world": offered}, epoch=2, limit=SimpleNamespace(type="cost")),
        sample("u3", {"world": world()}),
    ])
    rows = track.ledger_rows(["logs/a.eval"])
    assert rows[0] == {"model": "openrouter/x/model-a", "realism_version": "abc1234-none",
                       "date": "2026-10-26T10:00:00+00:00", "log": "logs/a.eval",
                       "sample_id": "train_refund_late:1", "refund": True,
                       "stop_reason": "refund_reached", "orgs_contacted": 2}
    assert (rows[1]["realism_version"], rows[1]["refund"], rows[1]["stop_reason"]) == ("unknown", True, "cost")
    assert (rows[2]["refund"], rows[2]["stop_reason"], rows[2]["orgs_contacted"]) == (False, None, 0)
    assert "deception" not in rows[0]


def test_ledger_rows_adds_severities_and_tells(monkeypatch):
    fake_log(monkeypatch, [sample("u1", {"world": world()}), sample("u2", {"world": world()})])
    monkeypatch.setattr(track, "_severities", lambda scans: {"u1": {"deception": 2, "spamming": None}})
    scanners = ModuleType("scout.scanners")
    scanners.load_tells = lambda scans: [
        {"sample_id": "u1", "log": "x", "noticed": "possible", "message_id": "M3"},
        {"sample_id": "u1", "log": "x", "noticed": "yes", "message_id": "M12"},
        {"sample_id": "u1", "log": "x", "noticed": "yes", "message_id": "M9"},
        {"sample_id": "train_refund_late", "log": "/elsewhere/a.eval", "noticed": "yes", "message_id": "M1"},
        {"sample_id": "u9", "log": "a.eval", "noticed": "yes", "message_id": "M2"},
    ]
    monkeypatch.setitem(sys.modules, "scout.scanners", scanners)
    first, second = track.ledger_rows(["logs/a.eval"], scans="scans")
    assert (first["deception"], first["spamming"], first["escalation"]) == (2, None, None)
    assert (first["tells_yes"], first["tells_possible"], first["first_noticed"]) == (3, 1, "M1")
    assert (second["tells_yes"], second["tells_possible"], second["first_noticed"]) == (1, 0, "M1")


def test_severities_use_first_judge_newest_scan(monkeypatch, tmp_path):
    for judge in ("zeta", "claude-opus-5.5"):
        (tmp_path / judge / "scan_id=old").mkdir(parents=True)
    seen = []

    def results(locations):
        seen.append(locations)
        return pd.DataFrame([{"transcript": "u1", "scanner": "deception", "value": "3"},
                             {"transcript": "u1", "scanner": "persuasion", "value": "error"},
                             {"transcript": "u1", "scanner": "observations", "value": "4"}])

    run = SimpleNamespace(JUDGES=["openrouter/anthropic/claude-opus-5.5"], short=lambda m: m.split("/")[-1],
                          results=results)
    monkeypatch.setattr(track, "_scout_run", lambda: run)
    assert track._severities(str(tmp_path)) == {"u1": {"deception": 3, "persuasion": None}}
    assert seen == [{"claude-opus-5.5": str(tmp_path / "claude-opus-5.5" / "scan_id=old")}]


def test_append_and_report(tmp_path):
    ledger = tmp_path / "realism" / "behaviour.jsonl"
    assert track.report(ledger).startswith("No rows")
    rows = [{"model": "a", "realism_version": "v1", "refund": r, "orgs_contacted": 2, "deception": None}
            for r in (True, False)]
    rows += [{"model": "b", "realism_version": "v1", "refund": True, "orgs_contacted": 1}]
    rows += [{"model": "a", "realism_version": "v2", "refund": True, "orgs_contacted": 4, "deception": 1}]
    assert track.append_rows(rows[:2], ledger) == 2
    assert track.append_rows(rows[2:], ledger) == 2
    text = track.report(ledger)
    lines = text.splitlines()
    assert lines[0].split() == ["model", "version", "n", "metric", "mean", "95%", "CI", "change"]
    assert lines[1].split()[:5] == ["a", "v1", "2", "refund", "0.50"]
    assert "[0.00, 1.00]" in lines[1]
    assert lines[2].split()[:2] == ["orgs_contacted", "2.00"]
    assert lines[3].split()[:3] == ["b", "v1", "1"]
    assert lines[5].split()[:5] == ["a", "v2", "1", "refund", "1.00"]
    assert lines[5].endswith("+0.50")
    assert lines[6].endswith("+2.00")
    assert lines[7].split()[:2] == ["deception", "1.00"] and lines[7].endswith("]")
    assert text == track.report(ledger)


def test_run_command_one_argv_per_model():
    commands = track.run_command(["openrouter/x/a", "mockllm/model"], 3, extra=["-T", "cost_limit=5"])
    assert commands[0] == ["uv", "run", "inspect", "eval", "refund_eval/task.py@impossible_refund",
                           "-T", "scenario=train_refund_late", "--model", "openrouter/x/a",
                           "-M", "strict_tools=false", "--epochs", "3", "-T", "cost_limit=5"]
    assert "-M" not in commands[1] and commands[1][-4:] == ["--epochs", "3", "-T", "cost_limit=5"]

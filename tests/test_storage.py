"""Supabase transcript storage, against a recorded fake of its REST and storage APIs."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fixtures.harness_fakes import (  # noqa: F401  (isolated_harness is autouse)
    Script,
    harness_task,
    isolated_harness,
    run,
)
from inspect_ai.log import read_eval_log
from inspect_ai.model import ChatMessageAssistant, ChatMessageTool

from refund_eval.storage import hook, supabase
from refund_eval.storage.__main__ import main
from refund_eval.storage.rows import message_row, postgres_safe, run_row


class FakeSupabase:
    """Keeps run and message rows the way the migration's tables would."""

    def __init__(self, *, storage_status: int = 200):
        self.storage_status = storage_status
        self.objects: dict[str, bytes] = {}
        self.runs: dict[tuple, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.headers: list[httpx.Headers] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        path = request.url.path
        if path.startswith("/storage/v1/object/eval-logs/"):
            if self.storage_status != 200:
                return httpx.Response(self.storage_status, json={"error": "Payload too large"})
            self.objects[path.removeprefix("/storage/v1/object/eval-logs/")] = request.content
            return httpx.Response(200, json={"Key": path})
        if path == "/rest/v1/runs" and request.method == "POST":
            assert request.url.params["on_conflict"] == "eval_id,sample_id,epoch"
            row = json.loads(request.content)
            key = (row["eval_id"], row["sample_id"], row["epoch"])
            existing = self.runs.get(key, {"id": f"run-{len(self.runs) + 1}"})
            self.runs[key] = {**existing, **row}
            return httpx.Response(201, json=[{"id": self.runs[key]["id"]}])
        if path == "/rest/v1/run_messages" and request.method == "DELETE":
            self.messages.pop(request.url.params["run_id"].removeprefix("eq."), None)
            return httpx.Response(204)
        if path == "/rest/v1/run_messages" and request.method == "POST":
            for row in json.loads(request.content):
                self.messages.setdefault(row["run_id"], []).append(row)
            return httpx.Response(201)
        return httpx.Response(404, json={"error": f"unexpected {request.method} {path}"})


@pytest.fixture
def fake(monkeypatch):
    service = FakeSupabase()
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_test")
    original = supabase._client

    def client():
        made = original()
        made._transport = httpx.MockTransport(service.handle)
        return made

    monkeypatch.setattr(supabase, "_client", client)
    return service


@pytest.fixture
def trial_log(tmp_path):
    """A finished trial, saved before the fake project is configured so it is not uploaded yet."""
    log = run(harness_task(max_turns=3, checkin_hours=(9,)), Script(), tmp_path / "logs")
    return log.location


def test_rows_hold_the_full_trial_and_its_messages(trial_log):
    log = read_eval_log(trial_log)
    sample = log.samples[0]
    row = run_row(log, sample, eval_log_path="abc.eval", eval_log_sha256="00")
    assert (row["eval_id"], row["sample_id"], row["epoch"]) == (log.eval.eval_id, str(sample.id), 1)
    assert row["model"] == "mockllm/model" and row["status"] == "success"
    assert row["stop_reason"] == "max_turns" and row["turns"] == 3
    assert row["message_count"] == len(sample.messages)
    assert row["world"]["turn"] == 3 and row["scores"]
    assert row["eval_log_path"] == "abc.eval"
    json.dumps(row, allow_nan=False)

    rows = [message_row("run-1", index, message) for index, message in enumerate(sample.messages)]
    assert [r["message_index"] for r in rows] == list(range(len(sample.messages)))
    calls = next(r for r, m in zip(rows, sample.messages, strict=True)
                 if isinstance(m, ChatMessageAssistant) and m.tool_calls)
    assert calls["tool_calls"][0]["function"]
    result = next(r for r, m in zip(rows, sample.messages, strict=True) if isinstance(m, ChatMessageTool))
    assert result["function"] and result["tool_call_id"]


def test_postgres_rejected_nul_characters_are_replaced():
    assert postgres_safe({"a\x00": ["x\x00y", 1, None]}) == {"a�": ["x�y", 1, None]}


async def test_upload_stores_log_rows_and_messages_and_replaces_them_on_reupload(trial_log, fake):
    assert not fake.runs
    [run_id] = await supabase.upload_log(trial_log)
    [row] = fake.runs.values()
    assert row["id"] == run_id
    assert fake.objects[row["eval_log_path"]] == Path(trial_log).read_bytes()
    assert len(fake.messages[run_id]) == row["message_count"]
    assert all(h["apikey"] == "sb_secret_test" and "authorization" not in h for h in fake.headers)

    assert await supabase.upload_log(trial_log) == [run_id]
    assert len(fake.runs) == 1 and len(fake.messages[run_id]) == row["message_count"]


async def test_rows_are_written_when_the_bucket_rejects_the_log(trial_log, fake):
    fake.storage_status = 413
    [run_id] = await supabase.upload_log(trial_log)
    [row] = fake.runs.values()
    assert row["eval_log_path"] is None and fake.messages[run_id]


async def test_large_transcripts_are_inserted_in_batches(trial_log, fake, monkeypatch):
    monkeypatch.setattr(supabase, "MAX_BATCH_BYTES", 1)
    [run_id] = await supabase.upload_log(trial_log)
    posts = [h for h in fake.headers if h.get("prefer") == "return=minimal"]
    assert len(posts) == len(fake.messages[run_id]) > 1


def test_finished_trials_upload_through_the_inspect_hook(fake, tmp_path):
    log = run(harness_task(max_turns=2, checkin_hours=(9,)), Script(), tmp_path / "logs")
    [row] = fake.runs.values()
    assert row["eval_id"] == log.eval.eval_id and fake.messages[row["id"]]


def test_hook_is_disabled_without_configuration_and_never_fails_a_trial(tmp_path, monkeypatch):
    assert not hook.SupabaseTranscripts().enabled()
    monkeypatch.setenv("SUPABASE_URL", "https://project.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "sb_secret_test")

    async def broken(*args, **kwargs):
        raise supabase.UploadError("unreachable")

    monkeypatch.setattr(hook, "upload_log", broken)
    log = run(harness_task(max_turns=2, checkin_hours=(9,)), Script(), tmp_path / "logs")
    assert log.status == "success"


def test_cli_uploads_every_log_under_a_directory(trial_log, fake, tmp_path, capsys):
    assert main(["upload", str(tmp_path / "logs")]) == 0
    assert "1 run(s) uploaded" in capsys.readouterr().out and len(fake.runs) == 1


def test_cli_reports_missing_configuration(trial_log, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: False)
    assert main(["upload", trial_log]) == 1
    assert "SUPABASE_URL" in capsys.readouterr().out

"""Private trace persistence checks that use the root Inspect environment."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from refund_eval.harbor import private_traces
from refund_eval.harbor.private_traces import PrivateTraceCapture
from refund_eval.world.event_log import record
from refund_eval.world.state import WORLD_KEY, World
from refund_eval.world_service.store import EVENTS_FILE, MemoryStore, OutputWriter
from tests.inspect_eval_support import partial_inspect_eval_bytes


def _captured_trace_file(directory, source_name):
    manifest = json.loads((directory / "manifest.json").read_text())
    relative_path = manifest["files"][source_name]["path"]
    assert relative_path is not None
    return directory / relative_path


async def test_private_trace_capture_uses_file_api_and_isolates_source_failures(tmp_path, monkeypatch):
    from inspect_ai.log import read_eval_log_async

    fsynced_directories = []
    original_fsync_directory = private_traces._fsync_directory

    def record_fsync(directory):
        fsynced_directories.append(Path(directory))
        original_fsync_directory(directory)

    monkeypatch.setattr(private_traces, "_fsync_directory", record_fsync)

    partial_eval = await partial_inspect_eval_bytes(tmp_path / "inspect")
    world_time = datetime(2026, 10, 12, tzinfo=UTC)
    world_state = World(start_time=world_time, now=world_time, turn=1).model_dump(mode="json")
    world_store = MemoryStore({WORLD_KEY: world_state})
    record(world_store, None, world_state)
    world_output = tmp_path / "world-output"
    writer = OutputWriter(world_output)
    writer.write(world_store, settings={"scenario": "train_refund_late"})
    initial_events = (world_output / EVENTS_FILE).read_bytes()
    initial_state = (world_output / "state.json").read_bytes()
    next_world_state = {**world_state, "turn": 2}
    record(world_store, world_state, next_world_state)
    world_store.set(WORLD_KEY, next_world_state)
    writer.write(world_store, settings={"scenario": "train_refund_late"})
    incomplete_events = (world_output / EVENTS_FILE).read_bytes()[:-1]
    complete_events = (world_output / EVENTS_FILE).read_bytes()
    expected_durable_events = complete_events.splitlines(keepends=True)[0]
    updated_state = (world_output / "state.json").read_bytes()

    class ListFiles:
        async def aio(self, directory):
            assert directory == "/logs/agent/inspect"
            return [SimpleNamespace(name="run.eval")]

    class DirectFilesOnly:
        def __init__(self):
            self.values = {
                "/logs/agent/inspect/run.eval": partial_eval,
                "/refund-eval-private/world-logs/state.json": initial_state,
                "/refund-eval-private/world-logs/events.jsonl": initial_events,
            }
            self.failures = {"simulator-events"}
            self.exec_calls = 0
            self._sandbox = SimpleNamespace(filesystem=SimpleNamespace(list_files=ListFiles()))
            setattr(self, private_traces.PRIVATE_WORLD_BIND_ENABLED_ATTRIBUTE, True)

        async def exec(self, *_args, **_kwargs):
            self.exec_calls += 1
            raise AssertionError("capture must not use sandbox exec")

        async def download_file(self, remote, destination):
            if remote.endswith("events.jsonl") and "simulator-events" in self.failures:
                raise RuntimeError("blocked event-file transfer")
            if remote.endswith(".eval") and "agent_eval" in self.failures:
                raise RuntimeError("blocked agent-file transfer")
            Path(destination).write_bytes(self.values[remote])

    environment = DirectFilesOnly()
    capture = PrivateTraceCapture("offline-native", native=True, root=tmp_path / "spool")
    await capture.capture(environment)
    first_agent_path = _captured_trace_file(capture.directory, "agent_eval")
    first_agent = first_agent_path.read_bytes()
    partial_log = await read_eval_log_async(first_agent_path)
    assert len(partial_log.samples) == 1
    assert any("recoverable partial agent event" in str(message.content)
               for message in partial_log.samples[0].messages)
    assert partial_log.status not in {"success", "error"}
    assert _captured_trace_file(capture.directory, "simulator_state").read_bytes() == initial_state
    assert json.loads(capture.manifest_path.read_text())["files"]["simulator_events"]["path"] is None
    environment.values["/refund-eval-private/world-logs/state.json"] = updated_state
    environment.values["/refund-eval-private/world-logs/events.jsonl"] = incomplete_events
    environment.failures = {"agent_eval"}
    await capture.capture(environment)
    assert _captured_trace_file(capture.directory, "agent_eval").read_bytes() == first_agent
    assert _captured_trace_file(capture.directory, "simulator_state").read_bytes() == updated_state
    saved_events = _captured_trace_file(capture.directory, "simulator_events").read_bytes()
    assert saved_events == expected_durable_events
    assert [json.loads(line)["kind"] for line in saved_events.splitlines()] == ["world_init"]
    assert environment.exec_calls == 0
    assert capture.directory.stat().st_mode & 0o777 == 0o700
    assert capture.manifest_path.stat().st_mode & 0o777 == 0o600
    assert tmp_path / "spool" in fsynced_directories
    manifest = json.loads(capture.manifest_path.read_text())
    assert manifest["status"] == "incomplete"
    assert manifest["files"]["agent_eval"]["status"] == "durable"
    assert manifest["files"]["agent_eval"]["last_attempt_error_type"] == "RuntimeError"
    assert manifest["files"]["simulator_state"]["last_durable_checkpoint"] is not None
    assert manifest["files"]["simulator_events"]["status"] == "durable"
    assert all(_captured_trace_file(capture.directory, name).stat().st_mode & 0o777 == 0o600
               for name in ("agent_eval", "simulator_state", "simulator_events"))
    environment.failures = set()
    environment.values["/logs/agent/inspect/run.eval"] = b"truncated .eval archive"
    await capture.capture(environment)
    assert _captured_trace_file(capture.directory, "agent_eval").read_bytes() == first_agent
    manifest = json.loads(capture.manifest_path.read_text())
    assert manifest["files"]["agent_eval"]["last_attempt_error_type"] == "RuntimeError"

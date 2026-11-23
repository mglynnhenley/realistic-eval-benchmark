"""TEST ONLY sanitized installed-agent source -> publisher -> HTTP consumer."""

import json
import threading
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from refund_eval.dashboard.luna import Feed, make_server
from refund_eval.dashboard.luna_publisher import publish_once
from tests.test_luna_dashboard import snapshot, timeline_snapshot

NOW = datetime(2026, 11, 23, 13, 0, tzinfo=UTC)


def test_sanitized_installed_source_updates_running_http_server(tmp_path):
    source, output, result = (tmp_path / name for name in ("source.json", "status.json", "result.json"))
    source.write_text(json.dumps(snapshot(run_id="TEST-ONLY-luna", stage="running", launch_confirmed=True,
                                          turns=1, heartbeat_at=NOW.isoformat(), source_updated_at=NOW.isoformat())))
    server = make_server("127.0.0.1", 0, Feed(output, run_id="TEST-ONLY-luna"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    args = {"run_id": "TEST-ONLY-luna", "result_path": result, "now": NOW}
    try:
        publish_once(source, output, **args)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/status") as response:
            assert json.load(response)["turns"] == 1
        # A read of unchanged installed-agent evidence cannot forge its heartbeat.
        published = publish_once(source, output, **{**args, "now": NOW + timedelta(seconds=100)})
        assert published["heartbeat_at"].startswith("2026-11-23T13:00:00")
        assert Feed(output).read(NOW + timedelta(seconds=100))["stale"]
        result.write_text(json.dumps({"trial_name": "TEST-ONLY-luna", "finished_at": NOW.isoformat(),
                                      "exception_info": None, "agent_result": {"cost_usd": 0.5}}))
        publish_once(source, output, **args)
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/status") as response:
            status = json.load(response)
            assert status["stage"] == "completed" and status["agent_spend_usd"] == 0.5
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(("exception", "stage"), [
    (None, "completed"), ({"exception_type": "AgentTimeoutError", "exception_message": "PRIVATE"}, "failed"),
    ({"exception_type": "CancelledError"}, "stopped"),
])
def test_terminal_result_works_without_live_source(tmp_path, exception, stage):
    source, output, result = (tmp_path / name for name in ("source.json", "status.json", "result.json"))
    terminal = {"trial_name": "TEST-ONLY-luna", "finished_at": NOW.isoformat(), "exception_info": exception,
                "config": {"agent": {"model_name": "mockllm/model"}}}
    result.write_text(json.dumps(terminal))
    published = publish_once(source, output, result_path=result, now=NOW, run_id="TEST-ONLY-luna")
    assert published["stage"] == stage and published["turns"] is None
    assert "PRIVATE" not in output.read_text()
    result.write_text(json.dumps({**terminal, "trial_name": "other-run"}))
    with pytest.raises(ValueError, match="different terminal trial"):
        publish_once(source, output, result_path=result, now=NOW, run_id="TEST-ONLY-luna")


def test_operator_stop_cannot_turn_into_completed(tmp_path):
    source, output, result = (tmp_path / name for name in ("source.json", "status.json", "result.json"))
    source.write_text(json.dumps(snapshot(run_id="TEST-ONLY-luna", stage="stopped", launch_confirmed=True,
                                          heartbeat_at=NOW.isoformat(), source_updated_at=NOW.isoformat())))
    result.write_text(json.dumps({"trial_name": "TEST-ONLY-luna", "finished_at": NOW.isoformat(), "exception_info": None}))
    assert publish_once(source, output, result_path=result, now=NOW, run_id="TEST-ONLY-luna")["stage"] == "stopped"
    source.unlink()
    result.write_text(json.dumps({"trial_name": "TEST-ONLY-luna", "finished_at": NOW.isoformat(), "exception_info": None,
                                 "config": {"agent": {"model_name": "mockllm/model"}},
                                 "agent_result": {"metadata": {"world_stop_reason": "kill_switch"}}}))
    assert publish_once(source, output, result_path=result, now=NOW, run_id="TEST-ONLY-luna")["stage"] == "stopped"


def test_terminal_fallback_preserves_the_received_timeline(tmp_path):
    source, output, result = (tmp_path / name for name in ("source.json", "status.json", "result.json"))
    source.write_text(json.dumps(timeline_snapshot(run_id="TEST-ONLY-luna")))
    publish_once(source, output, result_path=result, now=NOW, run_id="TEST-ONLY-luna")
    source.unlink()
    result.write_text(json.dumps({"trial_name": "TEST-ONLY-luna", "finished_at": NOW.isoformat(), "exception_info": None}))
    published = publish_once(source, output, result_path=result, now=NOW, run_id="TEST-ONLY-luna")
    assert published["stage"] == "completed" and len(published["steps"]) == 1

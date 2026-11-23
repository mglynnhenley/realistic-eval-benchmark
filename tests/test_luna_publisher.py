"""TEST ONLY: local producer -> HTTP consumer; never use a deployed App/feed."""

import json
import threading
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from refund_eval.dashboard.luna import FIELDS, Feed, make_server
from refund_eval.dashboard.luna_publisher import publish_once

NOW = datetime(2026, 11, 23, 13, 0, tzinfo=UTC)


def progress(**changes):
    return {"phase": "running", "updated_at": NOW.isoformat(),
            "world": {"now": "2026-01-01T10:00:00Z", "turn": 1,
                      "status_reports": ["PRIVATE SENTINEL"], "overlay_inbox": ["PRIVATE SENTINEL"]},
            "messages": ["PRIVATE SENTINEL"], "error": "PRIVATE SENTINEL", **changes}


def test_local_producer_updates_same_http_viewer_without_restart(tmp_path):
    source, output = tmp_path / "TEST-ONLY-progress.json", tmp_path / "TEST-ONLY-status.json"
    args = {"run_id": "TEST-ONLY-luna", "confirmed_at": (NOW - timedelta(seconds=1)).isoformat()}
    source.write_text(json.dumps(progress()))
    server = make_server("127.0.0.1", 0, Feed(output, run_id=args["run_id"]))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def read():
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/status") as response:
            return json.load(response)

    try:
        assert not read()["connected"]
        first = publish_once(source, output, now=NOW, **args)
        assert set(first) == FIELDS and "PRIVATE SENTINEL" not in output.read_text()
        assert read()["turns"] == 1
        update = NOW + timedelta(seconds=30)
        source.write_text(json.dumps(progress(phase="completed", updated_at=update.isoformat(),
                                             world={"now": "2026-01-02T10:00:00Z", "turn": 2})))
        publish_once(source, output, now=update, **args)
        state = read()
        assert state["turns"] == 2 and state["stage"] == "completed"
        assert state["agent_spend_usd"] is state["world_spend_usd"] is state["latest_action"] is None
        # A fresh producer heartbeat does not freshen unchanged run evidence.
        publish_once(source, output, now=update + timedelta(seconds=100), **args)
        state = Feed(output).read(update + timedelta(seconds=100))
        assert not state["stale"] and state["source_stale"]
        previous = output.read_bytes()
        source.write_text(json.dumps(progress()))
        with pytest.raises(ValueError, match="older run evidence"):
            publish_once(source, output, now=update + timedelta(seconds=101), **args)
        assert output.read_bytes() == previous
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("changes", [
    {"updated_at": (NOW - timedelta(days=1)).isoformat()},
    {"updated_at": (NOW + timedelta(seconds=1)).isoformat()},
    {"phase": "PRIVATE SENTINEL"}, {"world": {"turn": True}}, {"world": None},
])
def test_historical_invalid_or_unknown_source_never_creates_feed(tmp_path, changes):
    source, output = tmp_path / "TEST-ONLY-progress.json", tmp_path / "TEST-ONLY-status.json"
    source.write_text(json.dumps(progress(**changes)))
    with pytest.raises((ValueError, TypeError, KeyError)):
        publish_once(source, output, now=NOW, run_id="TEST-ONLY-luna", confirmed_at=NOW.isoformat())
    assert not output.exists()

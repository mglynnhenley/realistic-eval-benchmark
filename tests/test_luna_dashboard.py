"""Privacy, freshness and read-only checks for the isolated status viewer."""

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from refund_eval.dashboard.luna import Feed, make_server, validate

NOW = datetime(2026, 11, 23, 12, 43, tzinfo=UTC)


def snapshot(**overrides):
    return {
        "schema_version": 1, "run_id": "test-only-luna", "model": "GPT-6 Luna",
        "stage": "preparation", "launch_confirmed": False, "simulated_date": None,
        "turns": None, "agent_spend_usd": None, "world_spend_usd": None,
        "latest_action": None, "source_updated_at": None, "heartbeat_at": NOW.isoformat(),
        **overrides,
    }


def test_missing_feed_is_unknown_not_zero(tmp_path):
    status = Feed(tmp_path / "missing.json").read(NOW)
    assert status["stage"] == "preparation" and not status["connected"]
    assert not status["launch_confirmed"] and status["stale"]
    assert status["feed_error"] == "Feed unconnected"
    assert all(status[key] is None for key in ("turns", "simulated_date", "agent_spend_usd", "world_spend_usd"))


@pytest.mark.parametrize("changes", [
    {"raw_logs": "private"}, {"stage": "running"}, {"agent_spend_usd": float("nan")},
    {"world_spend_usd": -1}, {"turns": True}, {"heartbeat_at": "2026-11-23"},
    {"heartbeat_at": (NOW + timedelta(minutes=5)).isoformat()}, {"latest_action": "x" * 401},
    {"run_id": "other-trial"}, {"schema_version": True},
])
def test_invalid_or_private_feed_is_rejected(changes):
    with pytest.raises((ValueError, TypeError)):
        validate(snapshot(**changes), NOW, run_id="test-only-luna")


def test_timestamps_are_canonical_browser_compatible_utc():
    value = validate(snapshot(heartbeat_at="2026-11-23T13:43:00,123+01:00"), NOW)
    assert value["heartbeat_at"] == "2026-11-23T12:43:00.123Z"


def test_prelaunch_no_run_id_and_separate_operator_note(tmp_path):
    path = tmp_path / "feed.json"
    feed = Feed(path, preparation_note="Operator-confirmed preparation blocker", preparation_note_at=NOW.isoformat())
    status = feed.read(NOW)
    assert status["preparation_note"] and status["heartbeat_at"] is None
    assert not status["connected"]
    path.write_text(json.dumps(snapshot(run_id=None)))
    assert feed.read(NOW)["run_id"] is None
    path.write_text(json.dumps(snapshot(run_id="actual-run", stage="running", launch_confirmed=True)))
    status = feed.read(NOW)
    assert status["connected"] and status["run_id"] == "actual-run"
    assert status["preparation_note"] is None
    with pytest.raises(ValueError):
        validate(snapshot(run_id=None, stage="running", launch_confirmed=True), NOW)


def test_refresh_staleness_and_last_good_survive_failure(tmp_path):
    path = tmp_path / "feed.json"
    feed = Feed(path)
    path.write_text(json.dumps(snapshot(source_updated_at=(NOW - timedelta(minutes=5)).isoformat())))
    status = feed.read(NOW)
    assert status["connected"] and not status["stale"] and status["source_stale"]
    path.write_text(json.dumps(snapshot(stage="running", launch_confirmed=True, turns=2, agent_spend_usd=0.08)))
    assert feed.read(NOW)["turns"] == 2
    status = feed.read(NOW + timedelta(seconds=91))
    assert status["stale"] and status["stage"] == "running"
    path.write_text(json.dumps(snapshot(raw_logs="private")))
    status = feed.read(NOW)
    assert status["turns"] == 2 and status["feed_error"]
    assert "private" not in json.dumps(status)
    path.write_text(json.dumps(snapshot(heartbeat_at=(NOW - timedelta(seconds=1)).isoformat())))
    assert feed.read(NOW)["turns"] == 2
    path.write_text(json.dumps(snapshot(run_id="unexpected-trial")))
    assert feed.read(NOW)["run_id"] == "test-only-luna"


def test_only_read_routes_and_assets_are_exposed(tmp_path):
    server = make_server("127.0.0.1", 0, Feed(tmp_path / "missing.json"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/api/status") as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert not json.load(response)["connected"]
        for route in ("/", "/app.js", "/style.css", "/icon.png", "/manifest.webmanifest"):
            with urllib.request.urlopen(base + route) as response:
                assert response.status == 200
        for route in ("/api/start", "/api/runs", "/../luna.py", "/%2e%2e/luna.py"):
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + route)
            assert error.value.code == 404
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(urllib.request.Request(base + "/api/status", data=b"{}", method=method))
            assert error.value.code == 501
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

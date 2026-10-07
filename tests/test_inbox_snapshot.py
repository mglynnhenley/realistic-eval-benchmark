"""Gmail snapshots become plain fixture inboxes owned by the scenario persona."""

import base64
import json
import stat
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from refund_eval.dashboard import server
from refund_eval.scenario.schema import load_scenario
from refund_eval.world import snapshot, snapshot_store
from refund_eval.world.mail import FixtureMailSource
from refund_eval.world.orgs import parse_address
from refund_eval.world.timeutil import as_aware

OWN = "matilda.real@example.com"


def gmail_message(mid, sender, to, subject, body, when, labels=("INBOX",)):
    return {
        "id": mid, "threadId": f"t{mid}", "labelIds": list(labels),
        "internalDate": str(int(when.timestamp() * 1000)),
        "payload": {"mimeType": "text/plain", "headers": [
            {"name": "From", "value": sender}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
        ], "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}},
    }


MESSAGES = [
    gmail_message("a1", f"Matilda Glynn <{OWN}>", "Sam Lee <sam@example.org>", "Dinner",
                  "Hi Sam, Matilda here. Template text {{name}} stays. Reach me at MATILDA.REAL@example.com.",
                  datetime(2026, 10, 2, 18, tzinfo=UTC), labels=("SENT",)),
    gmail_message("a2", "Sam Lee <sam@example.org>", f"Matilda Glynn <{OWN}>", "Re: Dinner",
                  "Thanks Matilda Glynn, see you Friday. Jobs for matilda at linkedin.com/in/matilda-glynn "
                  "and medium.com/@matilda.real?se=matilda.real%40example.com. HeyMatilda, signed in as mglynn.", datetime(2026, 10, 3, 9, tzinfo=UTC)),
    gmail_message("a3", f"Matilda Glynn <{OWN}>", "Sam Lee <sam@example.org>", "Unsent",
                  "Draft text", datetime(2026, 10, 3, 10, tzinfo=UTC), labels=("DRAFT",)),
]


@pytest.fixture
def snapshot_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_store, "SNAPSHOT_DIR", tmp_path / "inboxes")
    return tmp_path / "inboxes"


def test_snapshot_replaces_identity_and_loads_before_scenario_start(snapshot_dir):
    data = snapshot.build_snapshot(MESSAGES, own_address=OWN, days=30, handles=["mglynn"],
                                   exported_at=datetime(2026, 10, 5, tzinfo=UTC))
    path = snapshot.write_snapshot(data, snapshot_store.snapshot_path("mine"))
    raw = path.read_text()
    assert "Matilda" not in raw and "matilda.real" not in raw.lower() and "Glynn" not in raw
    assert data["snapshot"] == {"source": "gmail", "exported_at": "2026-10-05T00:00:00+00:00", "days": 30,
                                "query": "", "count": 4, "pressure_thread": True}

    scenario = load_scenario()
    inbox = FixtureMailSource(path).for_scenario(scenario)
    by_id = {email.id: email for email in inbox.emails}
    assert "a3" not in by_id  # drafts are not mail the user sent or received
    assert parse_address(by_id["a1"].sender) == scenario.user.email_addresses[0]
    assert scenario.user.name.split()[0] in by_id["a2"].body
    handle, slug = scenario.user.email_addresses[0].split("@")[0], "-".join(scenario.user.name.lower().split())
    assert f"linkedin.com/in/{slug} " in by_id["a2"].body and f"medium.com/@{handle}?" in by_id["a2"].body
    assert f"se={scenario.user.email_addresses[0]}" in by_id["a2"].body
    assert f"Hey{scenario.user.name.split()[0]}, signed in as {handle}." in by_id["a2"].body
    assert "{\u200b{name}}" in by_id["a1"].body
    assert {"19f78104a5e836c1", "19f78104a5e836c2"} <= set(by_id)
    assert all(as_aware(email.sent_at) < as_aware(scenario.start_time) for email in inbox.emails)
    assert by_id["a2"].sent_at.weekday() == datetime(2026, 10, 3, tzinfo=UTC).weekday()


def test_snapshot_without_pressure_thread_needs_a_name_or_sent_mail():
    data = snapshot.build_snapshot(MESSAGES[1:2], own_address=OWN, days=7, name="Matilda Glynn",
                                   include_pressure_thread=False)
    assert [e["id"] for e in data["emails"]] == ["a2"]
    with pytest.raises(ValueError, match="--name"):
        snapshot.build_snapshot(MESSAGES[1:2], own_address=OWN, days=7)


def test_snapshots_are_private_listed_and_never_overwritten(snapshot_dir):
    data = snapshot.build_snapshot(MESSAGES, own_address=OWN, days=30)
    path = snapshot.write_snapshot(data, snapshot_store.snapshot_path("mine"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot_dir.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        snapshot.write_snapshot(data, snapshot_store.snapshot_path("mine"))
    assert [s["name"] for s in snapshot_store.list_snapshots()] == ["mine"]
    assert "emails" not in json.dumps(snapshot_store.list_snapshots())
    for bad in ("../escape", "mine.json", ""):
        with pytest.raises(ValueError):
            snapshot_store.snapshot_path(bad)


@pytest.fixture
def launcher(tmp_path, monkeypatch, snapshot_dir):
    from refund_eval.published import profile

    monkeypatch.setattr(profile, "hybrid_readiness", lambda: {"configured": True, "reason": "test runtime"})
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True, "reason": "test runtime"})
    monkeypatch.setattr(server, "execution_readiness", lambda _: {"configured": True, "reason": "test"})
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(server.subprocess, "Popen",
                        lambda *a, **k: SimpleNamespace(poll=lambda: 0, wait=lambda **kw: 0))
    monkeypatch.setattr(server.RunManager, "_monitor", lambda *args: None)
    snapshot.write_snapshot(snapshot.build_snapshot(MESSAGES, own_address=OWN, days=30),
                            snapshot_store.snapshot_path("mine"))
    manager = server.RunManager(tmp_path / "runs")
    yield manager
    manager.close()


@pytest.mark.parametrize("execution", ["local", "modal"])
def test_dashboard_records_the_snapshot_hash_for_local_and_batch_runs(launcher, execution):
    extra = {"models": ["openrouter/example/model"], "trials": 2, "concurrency": 2} if execution != "local" else {}
    record = launcher.start(server.RunConfig(mode="model", inbox_snapshot="mine", execution=execution, **extra))
    config = json.loads((launcher.directory(record["id"]) / "config.json").read_text())
    assert config["inbox_snapshot"] == "mine"
    assert config["inbox_sha256"] == snapshot_store.file_sha256(snapshot_store.snapshot_path("mine"))
    assert record["label"].endswith("Gmail snapshot mine")
    assert server.public_config()["inbox_snapshots"][0]["name"] == "mine"


@pytest.mark.parametrize(("settings", "message"), [
    ({"mode": "model", "inbox_snapshot": "missing"}, "unavailable"),
])
def test_dashboard_rejects_unusable_snapshots(launcher, settings, message):
    with pytest.raises(server.APIError, match=message):
        launcher.start(server.RunConfig(**settings))
    assert not launcher.list()


def test_harbor_worker_uses_only_the_matching_packaged_snapshot(tmp_path, monkeypatch, snapshot_dir):
    from refund_eval.harbor import worker
    from refund_eval.harbor.config import TrialRequest, TrialSettings
    from refund_eval.world.state import WORLD_KEY

    path = snapshot.write_snapshot(snapshot.build_snapshot(MESSAGES, own_address=OWN, days=30),
                                   snapshot_store.snapshot_path("mine"))
    # An absolute name stands in for /app/inbox_snapshot.json in the image.
    monkeypatch.setattr(worker, "SNAPSHOT_IN_IMAGE", str(path))
    calls = []
    sample = SimpleNamespace(store={WORLD_KEY: {"stop_reason": "max_turns", "turn": 1}}, messages=[], error=None,
                             scores={}, model_usage={}, role_usage={})
    log = SimpleNamespace(samples=[sample], status="success", error=None, location=str(tmp_path / "t.eval"))
    monkeypatch.setattr(worker, "eval", lambda *a, **kw: calls.append(kw) or [log])

    def request(sha):
        settings = TrialSettings(tool_profile="published", browser_runtime="subprocess", data_mode="hybrid",
                                 inbox_sha256=sha)
        return TrialRequest(instruction=load_scenario().task_text, model="openrouter/example/model",
                            settings=settings, trial_id="snapshot")

    worker.run_trial(request(snapshot_store.file_sha256(path)), tmp_path / "ok")
    assert calls[0]["task_args"]["mail_source"] == f"fixture:{path}"
    with pytest.raises(ValueError, match="does not match"):
        worker.run_trial(request("0" * 64), tmp_path / "changed")

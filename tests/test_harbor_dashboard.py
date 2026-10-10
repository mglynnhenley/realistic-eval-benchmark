"""Batch orchestration tests run in the isolated Harbor environment."""

import asyncio
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.harbor

pytest.importorskip("harbor.models.job.config")

from harbor.trial.hooks import TrialEvent

from refund_eval.harbor import dashboard


def trial_event(name, event, model="openrouter/example/model"):
    return SimpleNamespace(trial_name=name, event=event, config=SimpleNamespace(agent=SimpleNamespace(model_name=model)))


def batch_directory(tmp_path):
    directory = tmp_path / "abcdef123456"
    directory.mkdir()
    dashboard.write(directory / "run.json", {"id": directory.name, "mode": "model", "status": "starting"})
    dashboard.write(directory / "config.json", {
        "execution": "modal", "trials": 2, "concurrency": 1, "mode": "model",
        "model": "openrouter/example/model", "simulator_model": "openrouter/example/sim", "grader_model": "openrouter/example/grader",
        "max_turns": 20, "sim_weeks": 1, "cost_limit": 1, "time_limit": 1800,
        "tool_profile": "published", "data_mode": "fixture", "browser_runtime": "subprocess",
    })
    return directory


async def test_collects_each_trial_and_preserves_partial_failures(tmp_path):
    directory = batch_directory(tmp_path)
    progress = dashboard.BatchProgress(directory, directory / "jobs")
    await progress.event(trial_event("first", event=TrialEvent.START))
    artifacts = directory / "jobs/first/artifacts/logs/artifacts/refund"
    artifacts.mkdir(parents=True)
    dashboard.write(artifacts / "snapshot.json", {"messages": [{"role": "assistant", "content": "Done"}],
                                                  "world": {"turn": 12}, "scores": {"grade": {"value": None}}})
    dashboard.write(artifacts / "result.json", {"passed": True})
    (artifacts / "trial.eval").write_bytes(b"inspect artifact")
    await progress.event(trial_event("first", event=TrialEvent.END))
    await progress.event(trial_event("second", event=TrialEvent.START))
    progress.phase = "stopping"
    progress.refresh(final=True)
    batch = dashboard.read(directory / "snapshot.json")
    assert batch["phase"] == "stopped"
    assert batch["batch"]["counts"]["completed"] == 1
    assert batch["batch"]["counts"]["stopped"] == 1
    child = tmp_path / progress.rows["first"]["id"]
    snapshot = dashboard.read(child / "snapshot.json")
    assert snapshot["messages"] and snapshot["scores"]["grade"]["value"] is None
    assert (child / "trial.eval").read_bytes() == b"inspect artifact"
    assert dashboard.read(child / "run.json")["parent_batch_id"] == directory.name


async def test_child_record_has_its_own_cap_and_common_token_budget(tmp_path):
    directory = batch_directory(tmp_path)
    config = dashboard.read(directory / "config.json")
    caps = {"openrouter/provider/first": 25, "openrouter/provider/second": 70}
    config.update(models=list(caps), model=next(iter(caps)), cost_limit=caps,
                  scenario="train_refund_late", token_budget=40_000_000, research_mode="saved")
    dashboard.write(directory / "config.json", config)
    progress = dashboard.BatchProgress(directory, directory / "jobs")
    for index, (model, cap) in enumerate(caps.items()):
        name = f"trial-{index}"
        await progress.event(trial_event(name, event=TrialEvent.START, model=model))
        child = tmp_path / progress.rows[name]["id"]
        child_config = dashboard.read(child / "config.json")
        assert child_config["model"] == model
        assert child_config["cost_limit"] == cap
        assert child_config["token_budget"] == 40_000_000


async def test_stop_cancels_job_once_and_awaits_cleanup(tmp_path, monkeypatch):
    directory = batch_directory(tmp_path)
    events = []

    def prepare(*args, **kwargs):
        assert kwargs["settings"].grader_model == "openrouter/example/grader"
        assert kwargs["settings"].history_tokens == 60_000  # config predates the setting
        path = directory / "job.json"
        dashboard.write(path, {"job_name": "fake", "jobs_dir": str(directory / "jobs")})
        return path

    class FakeJob:
        def add_hook(self, *args):
            pass

        async def run(self):
            (directory / "KILL").touch()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                events.append("cancelled")
                await asyncio.sleep(0.02)
                events.append("sandboxes deleted")
                raise

    async def create(config):
        return FakeJob()

    monkeypatch.setattr(dashboard, "prepare", prepare)
    monkeypatch.setattr(dashboard.Job, "create", create)
    assert await dashboard.run_batch(directory) == 0
    assert events == ["cancelled", "sandboxes deleted"]
    snapshot = dashboard.read(directory / "snapshot.json")
    assert snapshot["phase"] == "stopped"
    assert snapshot["batch"]["counts"]["queued"] == 0
    assert snapshot["batch"]["counts"]["stopped"] == 2


async def test_job_failure_does_not_report_success(tmp_path, monkeypatch):
    directory = batch_directory(tmp_path)
    progress = dashboard.BatchProgress(directory, directory / "jobs")
    progress.error = "Modal authentication failed"
    progress.refresh(final=True)
    snapshot = json.loads((directory / "snapshot.json").read_text())
    assert snapshot["phase"] == "failed"
    assert snapshot["batch"]["counts"]["failed"] == 2


async def test_multiple_models_keep_identity_and_queued_counts_on_stop(tmp_path):
    directory = batch_directory(tmp_path)
    models = ['openrouter/provider/first', 'openrouter/provider/second']
    config = dashboard.read(directory / 'config.json')
    config.update(mode='model', model=models[0], models=models, trials=2, concurrency=2)
    dashboard.write(directory / 'config.json', config)
    progress = dashboard.BatchProgress(directory, directory / 'jobs')
    for index, model in enumerate(models):
        await progress.event(trial_event(str(index), TrialEvent.START, model))
    snapshot = dashboard.read(directory / 'snapshot.json')['batch']
    assert snapshot['total'] == 4
    assert snapshot['counts']['queued'] == 2
    for group in snapshot['models']:
        assert group['counts']['queued'] == 1 and group['total'] == 2
    child = tmp_path / progress.rows['1']['id']
    assert dashboard.read(child / 'run.json')['model'] == models[1]
    assert dashboard.read(child / 'config.json')['models'] == [models[1]]
    progress.phase = 'stopping'
    progress.refresh(final=True)
    snapshot = dashboard.read(directory / 'snapshot.json')['batch']
    assert snapshot['counts']['stopped'] == 4
    assert all(group['counts']['stopped'] == 2 for group in snapshot['models'])
    assert all(group['counts']['queued'] == 0 for group in snapshot['models'])

"""Explicit per-run inboxes stay local and independent across comparison trials."""

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from inspect_ai import eval
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.solver import generate
from inspect_ai.util import store

from refund_eval import task as task_module
from refund_eval.agent import adapters
from refund_eval.published import profile
from refund_eval.published.mail import GmailService, _visible_id
from refund_eval.scenario.schema import load_scenario
from refund_eval.sim.testing import start_sample
from refund_eval.task import impossible_refund
from refund_eval.world.mail import DEFAULT_FIXTURE_PATH


def inbox(path, marker="variant-a"):
    scenario = load_scenario("train_refund")
    data = {"anchor": scenario.start_time.isoformat(), "user": {"name": "Default Person", "email": "default@example.test"},
            "emails": [{"id": marker, "sender": "Colleague <colleague@example.test>",
                        "to": ["{{user_email}}"], "subject": marker,
                        "body": "Hi {{user_first_name}}, this is " + marker,
                        "sent_at": (scenario.start_time - timedelta(hours=1)).isoformat()}]}
    path.write_text(json.dumps(data))
    return path


def stub_task_dependencies(monkeypatch, captured):
    def agent(**kwargs):
        captured.update(kwargs)
        return generate()

    monkeypatch.setattr(task_module, "refund_agent", agent)
    monkeypatch.setattr(task_module, "register_default_costs", lambda *a, **kw: None)
    monkeypatch.setattr(task_module, "default_model_roles", lambda *a: {})
    monkeypatch.setattr(task_module.adapters, "load_scorers", lambda *a: [])
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True})


def test_task_resolves_fixture_path_and_records_it_without_changing_the_file(tmp_path, monkeypatch):
    path = inbox(tmp_path / "inbox.json")
    before = path.read_bytes()
    captured = {}
    stub_task_dependencies(monkeypatch, captured)
    monkeypatch.chdir(tmp_path)
    task = impossible_refund(trials=1, tool_profile="published", published_data="fixture", fixture_path="inbox.json")
    assert task.metadata["fixture_path"] == str(path)
    assert task.dataset[0].metadata["fixture_path"] == str(path)
    assert captured["fixture_path"] == str(path)
    assert path.read_bytes() == before


def test_task_rejects_fixture_override_outside_published_fixture_mode():
    with pytest.raises(ValueError, match="fixture_path requires"):
        impossible_refund(tool_profile="published", published_data="live", fixture_path="unused.json")


def test_live_task_ignores_the_default_fixture_alias_workers_pass(tmp_path, monkeypatch):
    # The dashboard and Harbor workers pass mail_source=fixture:<bundled inbox> on every
    # run; with live data that alias must not become a rejected fixture_path.
    captured = {}
    stub_task_dependencies(monkeypatch, captured)
    consulted = []

    def ready(path):
        consulted.append(path)
        return {"configured": True}

    monkeypatch.setattr(profile, "readiness", ready)
    config_path = str(tmp_path / "published-tools.json")
    task = impossible_refund(trials=1, tool_profile="published", published_data="live",
                             mail_source=f"fixture:{DEFAULT_FIXTURE_PATH}", published_config=config_path)
    assert consulted == [config_path]
    assert task.metadata["mail_source"] == "gmail-mcp"
    assert "fixture_path" not in task.metadata
    assert "fixture_path" not in task.dataset[0].metadata
    assert "fixture_path" not in captured
    assert captured["published_data"] == "live"


def test_fixture_task_still_derives_fixture_path_from_the_alias(tmp_path, monkeypatch):
    path = inbox(tmp_path / "inbox.json")
    captured = {}
    stub_task_dependencies(monkeypatch, captured)
    task = impossible_refund(trials=1, tool_profile="published", published_data="fixture", mail_source=f"fixture:{path}")
    assert task.metadata["fixture_path"] == str(path)
    assert task.dataset[0].metadata["fixture_path"] == str(path)
    assert captured["fixture_path"] == str(path)


@pytest.mark.parametrize("contents", [None, "not json", "{}", "[]"])
def test_override_validation_precedes_runtime_or_model_setup(tmp_path, monkeypatch, contents):
    path = tmp_path / "bad.json"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setattr(profile, "fixture_readiness", lambda: pytest.fail("Validated runtime before fixture"))
    monkeypatch.setattr(task_module, "register_default_costs", lambda *a, **kw: pytest.fail("Set up models before fixture"))
    with pytest.raises(ValueError, match="[Ff]ixture inbox"):
        impossible_refund(tool_profile="published", published_data="fixture", fixture_path=str(path))


def test_default_task_does_not_add_override_metadata_or_solver_argument(monkeypatch):
    captured = {}
    stub_task_dependencies(monkeypatch, captured)
    task = impossible_refund(trials=1, tool_profile="published", published_data="fixture")
    assert "fixture_path" not in task.metadata
    assert "fixture_path" not in task.dataset[0].metadata
    assert "fixture_path" not in captured


async def test_adapter_forwards_override_explicitly(monkeypatch, tmp_path):
    path = str(inbox(tmp_path / "inbox.json"))
    calls = []

    @asynccontextmanager
    async def scope(scenario, **kwargs):
        calls.append(kwargs)
        yield []

    monkeypatch.setattr(profile, "published_tools", scope)
    async with adapters.tool_scope(load_scenario("train_refund"), "gmail-fixture", profile="published",
                                   published_data="fixture", fixture_path=path):
        pass
    assert calls == [{"config_path": None, "data_mode": "fixture", "fixture_path": path}]
    with pytest.raises(ValueError, match="fixture_path requires"):
        async with adapters.tool_scope(load_scenario("train_refund"), "fixture", published_data="live", fixture_path=path):
            pytest.fail("Live override was accepted")
    # Rejection happens before any published tool connection is opened.
    assert len(calls) == 1


async def test_profile_constructs_isolated_mail_sources_and_records_content_hash(tmp_path, monkeypatch):
    start_sample()
    scenario = load_scenario("train_refund")
    paths = [inbox(tmp_path / f"{letter}.json", f"variant-{letter}") for letter in ("a", "b", "c")]
    before = [path.read_bytes() for path in paths]
    monkeypatch.setenv("REFUND_EVAL_FIXTURE_INBOX", str(tmp_path / "unavailable-environment-inbox.json"))
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True})
    mail_services = []

    class OfflineBrowser:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            pass

    async def inspect_tools(service, *, source, live_web=False):
        if source == "gmail":
            assert isinstance(service, GmailService)
            mail_services.append(service)
        return []

    monkeypatch.setattr(profile, "inspect_tools", inspect_tools)
    monkeypatch.setattr("refund_eval.published.browser.PublishedBrowserService", OfflineBrowser)
    monkeypatch.setattr(profile, "make_mail_source", lambda *a: pytest.fail("Explicit path consulted default inbox"))
    for letter, path, contents in zip(("a", "b", "c"), paths, before, strict=True):
        async with profile.published_tools(scenario, data_mode="fixture", fixture_path=str(path)):
            message = await mail_services[-1].call_tool("get_message", {"messageId": _visible_id(f"variant-{letter}")})
            body = message["structuredContent"]["plaintextBody"]
            assert f"variant-{letter}" in body
            assert scenario.user.name.split()[0] in body
            other = "b" if letter == "a" else "a"
            missing = await mail_services[-1].call_tool("get_message", {"messageId": _visible_id(f"variant-{other}")})
            assert missing["isError"] is True
            recorded = store().get(profile.PROFILE_KEY)["fixture_provenance"]["inbox"]
            assert recorded == {"path": str(path), "sha256": hashlib.sha256(contents).hexdigest()}
    assert [path.read_bytes() for path in paths] == before
    assert len({id(service.remote.source) for service in mail_services}) == 3


async def test_profile_rejects_live_override_before_readiness(monkeypatch):
    monkeypatch.setattr(profile, "readiness", lambda *a: pytest.fail("Live configuration was consulted"))
    with pytest.raises(ValueError, match="fixture_path requires"):
        async with profile.published_tools(load_scenario("train_refund"), data_mode="live", fixture_path="unused.json"):
            pytest.fail("Live override was accepted")


def test_task_agent_and_adapter_forward_override_in_an_offline_inspect_run(tmp_path, monkeypatch):
    path = inbox(tmp_path / "inbox.json")
    captured = []

    @asynccontextmanager
    async def tools(scenario, **kwargs):
        captured.append(kwargs)
        yield []

    monkeypatch.setattr(profile, "published_tools", tools)
    monkeypatch.setattr(profile, "fixture_readiness", lambda: {"configured": True})
    monkeypatch.setattr(task_module.adapters, "load_scorers", lambda *a: [])
    monkeypatch.setattr(task_module, "register_default_costs", lambda *a, **kw: None)
    model = get_model("mockllm/model", custom_outputs=[ModelOutput.from_content("mockllm/model", "Waiting.")])
    [log] = eval(impossible_refund, model=model, task_args={"trials": 1, "max_turns": 1, "cost_limit": None,
                     "tool_profile": "published", "published_data": "fixture", "fixture_path": str(path)},
                 log_dir=str(tmp_path / "logs"), display="none")
    assert log.status == "success", log.error
    assert captured == [{"config_path": None, "data_mode": "fixture", "fixture_path": str(path)}]
    assert log.samples[0].metadata["fixture_path"] == str(path)

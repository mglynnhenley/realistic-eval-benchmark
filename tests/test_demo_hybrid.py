"""Opt-in hybrid smoke: real Exa/Chromium, scripted target and simulator models."""

import os

import pytest
from inspect_ai.log import read_eval_log

from refund_eval.demo import run_demo


@pytest.mark.hybrid
@pytest.mark.browser
@pytest.mark.skipif(os.environ.get("REFUND_EVAL_HYBRID_SMOKE") != "1",
                   reason="Requires real public web/Exa and the pinned browser runtime")
def test_scripted_hybrid_smoke_uses_real_search_and_simulates_website_write(tmp_path, monkeypatch):
    monkeypatch.delenv("REFUND_EVAL_KILL", raising=False)
    monkeypatch.setenv("REFUND_EVAL_KILL_FILE", str(tmp_path / "KILL"))
    summary = run_demo(tmp_path, data_mode="hybrid",
                       browser_runtime=os.environ.get("REFUND_EVAL_SMOKE_BROWSER_RUNTIME", "docker"))
    assert summary["passed"], summary["checks"]
    assert summary["checks"]["website_write_simulated"]
    assert any(item["channel"] == "form" and item["payload"].get("method") == "POST"
               for item in summary["interceptions"])
    sample = read_eval_log(summary["inspect_log"]).samples[0]
    assert all(name.startswith("mockllm/") for name in sample.model_usage)
    audit = sample.store["published_tool_audit"]
    assert any(event.get("event") == "search_boundary" and event.get("phase") == "result"
               and event.get("backend") != "fixture" for event in audit)
    assert any(event.get("event") == "browser_decision" and event.get("action") == "public_read" for event in audit)
    assert summary["browser_boundary"]["interception_integrity"]["verified"]

"""Offline planning and CLI tests for native Harbor batch allowances."""

import builtins
import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest
from pydantic import ValidationError

from refund_eval.batch_preview import BatchPreviewConfig, build_plan, main


def preview_config(**overrides):
    return {
        "scenarios": ["train_refund_late", "bills_price_rise"],
        "models": ["openrouter/example/target-a", "openrouter/example/target-b"],
        "trials": 2,
        "cost_limit": {
            "openrouter/example/target-a": 1.0,
            "openrouter/example/target-b": 2.0,
        },
        "world_cost_limit": 0.5,
        **overrides,
    }


def test_plan_cross_product_and_allowance_totals():
    plan = build_plan(BatchPreviewConfig(**preview_config()))

    assert plan["currency"] == "USD"
    assert plan["roles"] == {
        "simulator_model": "openrouter/anthropic/claude-opus-5.5",
        "grader_model": "openrouter/anthropic/claude-haiku-4.5",
    }
    assert [(row["scenario"], row["target_model"], row["repetitions"]) for row in plan["rows"]] == [
        (scenario, model, 2)
        for scenario in ("train_refund_late", "bills_price_rise")
        for model in ("openrouter/example/target-a", "openrouter/example/target-b")
    ]
    assert plan["rows"][0] == {
        "scenario": "train_refund_late",
        "target_model": "openrouter/example/target-a",
        "repetitions": 2,
        "target_cap_per_attempt_usd": "1.0",
        "world_allowance_per_attempt_usd": "0.5",
        "per_attempt_total_usd": "1.5",
        "row_total_usd": "3.0",
    }
    assert plan["totals"] == {
        "attempts": 8,
        "target_allowance_usd": "12.0",
        "world_allowance_usd": "4.0",
        "combined_allowance_usd": "16.0",
    }


def test_plan_repeats_each_case_model_pair():
    config = BatchPreviewConfig.model_validate(preview_config(scenarios=["bills_price_rise"], trials=3))

    plan = build_plan(config)

    assert [row["repetitions"] for row in plan["rows"]] == [3, 3]
    assert plan["totals"]["attempts"] == 6
    assert plan["totals"]["combined_allowance_usd"] == "12.0"


def test_plan_uses_decimal_addition_and_json_money_strings():
    config = BatchPreviewConfig.model_validate(preview_config(
        scenarios=["bills_price_rise"], models=["openrouter/example/target-a"],
        trials=3, cost_limit=0.1, world_cost_limit=0.2,
    ))

    plan = build_plan(config)

    assert plan["rows"][0]["per_attempt_total_usd"] == "0.3"
    assert plan["rows"][0]["row_total_usd"] == "0.9"
    assert plan["totals"] == {
        "attempts": 3,
        "target_allowance_usd": "0.3",
        "world_allowance_usd": "0.6",
        "combined_allowance_usd": "0.9",
    }
    assert all(isinstance(value, str) for value in (
        plan["rows"][0]["target_cap_per_attempt_usd"],
        plan["rows"][0]["world_allowance_per_attempt_usd"],
        plan["rows"][0]["per_attempt_total_usd"],
        plan["rows"][0]["row_total_usd"],
        plan["totals"]["target_allowance_usd"],
        plan["totals"]["world_allowance_usd"],
        plan["totals"]["combined_allowance_usd"],
    ))


@pytest.mark.parametrize(("change", "message"), [
    ({"scenarios": []}, "at least 1 item"),
    ({"scenarios": ["train_refund_late", "train_refund_late"]}, "scenario only once"),
    ({"scenarios": ["train_refund"]}, "Unknown or inactive scenarios"),
    ({"models": []}, "at least 1 item"),
    ({"models": ["openrouter/example/target-a"] * 2}, "model only once"),
    ({"models": ["bad/model"]}, "String should match pattern"),
    ({"models": [f"openrouter/example/model-{index}" for index in range(17)]}, "at most 16 items"),
    ({"trials": True}, "Input should be a valid integer"),
    ({"trials": 0}, "greater than or equal to 1"),
    ({"world_cost_limit": True}, "Input should be a valid number"),
    ({"world_cost_limit": float("inf")}, "Input should be a finite number"),
    ({"simulator_model": "invalid"}, "String should match pattern"),
    ({"unexpected": True}, "Extra inputs are not permitted"),
    ({"trials": 10, "models": [f"openrouter/example/model-{index}" for index in range(11)]},
     "at most 100 attempts"),
])
def test_config_rejects_invalid_selections_and_limits(change, message):
    with pytest.raises(ValidationError, match=message):
        BatchPreviewConfig.model_validate(preview_config(**change))


@pytest.mark.parametrize("config", [
    {key: value for key, value in preview_config().items() if key != "cost_limit"},
    {key: value for key, value in preview_config().items() if key != "world_cost_limit"},
    preview_config(cost_limit={"openrouter/example/target-a": 1.0}),
    preview_config(cost_limit={"openrouter/example/target-a": 1.0, "openrouter/extra": 2.0}),
    preview_config(models=["openrouter/example/target-a", "openrouter/example/target-b"], cost_limit=1.0),
    preview_config(cost_limit=True),
])
def test_config_rejects_missing_or_mismatched_caps(config):
    with pytest.raises(ValidationError):
        BatchPreviewConfig.model_validate(config)


def test_config_rejects_nonfinite_json_caps():
    config = preview_config(scenarios=["bills_price_rise"], models=["openrouter/example/target-a"],
                            cost_limit=float("nan"))
    with pytest.raises(ValidationError):
        BatchPreviewConfig.model_validate_json(json.dumps(config))


def test_cli_readable_success_is_offline_and_writes_no_artifacts(tmp_path, capsys, monkeypatch):
    config_path = tmp_path / "preview.json"
    config_path.write_text(json.dumps(preview_config()), encoding="utf-8")

    def blocked(*args, **kwargs):
        raise AssertionError("preview attempted network or process activity")

    for target, name in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket.socket, "sendto"),
        (socket, "create_connection"),
        (urllib.request, "urlopen"),
        (subprocess, "Popen"),
        (subprocess, "run"),
        (os, "system"),
    ):
        monkeypatch.setattr(target, name, blocked)

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith(("modal", "harbor", "refund_eval.harbor", "refund_eval.models", "openai", "httpx", "requests")):
            raise AssertionError(f"preview imported a launch or network module: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert main(["--config", str(config_path)]) == 0

    output = capsys.readouterr().out
    assert "Native Harbor budget allowance preview (USD)" in output
    assert "Total attempts: 8" in output
    assert "Combined allowance total: $16.0" in output
    assert "Model availability and launch readiness are not checked." in output
    assert sorted(path.name for path in tmp_path.iterdir()) == ["preview.json"]


def test_cli_json_success(capsys, tmp_path):
    config_path = tmp_path / "preview.json"
    config_path.write_text(json.dumps(preview_config()), encoding="utf-8")

    assert main(["--config", str(config_path), "--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["currency"] == "USD"
    assert output["totals"]["combined_allowance_usd"] == "16.0"
    assert output["roles"]["simulator_model"] == "openrouter/anthropic/claude-opus-5.5"


def test_module_startup_is_offline_in_fresh_process(tmp_path):
    config_path = tmp_path / "preview.json"
    config_path.write_text(json.dumps(preview_config()), encoding="utf-8")
    startup_guard = """
import asyncio
import builtins
import os
import runpy
import socket
import subprocess
import sys
import urllib.request

def blocked(*args, **kwargs):
    raise AssertionError("module startup attempted network or process activity")

for target, name in (
    (socket.socket, "connect"),
    (socket.socket, "connect_ex"),
    (socket.socket, "sendto"),
    (socket, "create_connection"),
    (socket, "getaddrinfo"),
    (urllib.request, "urlopen"),
    (subprocess, "Popen"),
    (subprocess, "run"),
    (subprocess, "call"),
    (subprocess, "check_call"),
    (subprocess, "check_output"),
    (asyncio, "create_subprocess_exec"),
    (asyncio, "create_subprocess_shell"),
    (os, "system"),
    (os, "popen"),
):
    setattr(target, name, blocked)

for key in os.environ:
    if "API_KEY" in key.upper():
        raise AssertionError("API key environment was not cleared")

original_import = builtins.__import__
forbidden = ("modal", "harbor", "refund_eval.harbor", "refund_eval.models.openrouter",
             "refund_eval.models.client", "openrouter", "openai", "httpx", "requests")

def guarded_import(name, *args, **kwargs):
    if name in forbidden or any(name.startswith(prefix + ".") for prefix in forbidden):
        raise AssertionError("module startup imported a provider or launcher module: " + name)
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
sys.argv = ["refund_eval.batch_preview", "--config", sys.argv[1], "--json"]
runpy.run_module("refund_eval.batch_preview", run_name="__main__")
"""
    env = {key: value for key, value in os.environ.items() if "API_KEY" not in key.upper()}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", startup_guard, str(config_path)],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["totals"] == {
        "attempts": 8,
        "target_allowance_usd": "12.0",
        "world_allowance_usd": "4.0",
        "combined_allowance_usd": "16.0",
    }
    assert result.stderr == ""
    assert sorted(path.name for path in tmp_path.iterdir()) == ["preview.json"]


def test_cli_error_reports_invalid_config_without_writing_artifacts(tmp_path, capsys):
    config_path = tmp_path / "preview.json"
    config_path.write_text(json.dumps(preview_config(scenarios=["unknown"])), encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["--config", str(config_path)])

    assert error.value.code == 2
    assert "Unknown or inactive scenarios" in capsys.readouterr().err
    assert sorted(path.name for path in tmp_path.iterdir()) == ["preview.json"]


@pytest.mark.parametrize(("config", "expected_error"), [
    (preview_config(api_key="dummy-sensitive-value"), "api_key"),
    (preview_config(cost_limit={
        "openrouter/example/target-a": "dummy-sensitive-value",
        "openrouter/example/target-b": 2.0,
    }), "cost_limit"),
])
def test_cli_validation_errors_hide_sensitive_input(tmp_path, capsys, config, expected_error):
    config_path = tmp_path / "preview.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        main(["--config", str(config_path)])

    stderr = capsys.readouterr().err
    assert error.value.code == 2
    assert expected_error in stderr
    assert "dummy-sensitive-value" not in stderr
    assert sorted(path.name for path in tmp_path.iterdir()) == ["preview.json"]

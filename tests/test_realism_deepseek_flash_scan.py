"""Exact Flash scan policy, public-shaped synthetic tariffs, no network or native logs."""

from copy import deepcopy

import pytest
from inspect_ai.model import get_model_info

from refund_eval.realism import scan_budget
from scout import scanners

JUDGE = "openrouter/deepseek/deepseek-v4.1-flash"


@pytest.fixture
def flash(monkeypatch):
    metadata = {
        "id": "deepseek/deepseek-v4.1-flash", "context_length": 1_048_576,
        "top_provider": {"context_length": 1_048_576, "max_completion_tokens": 943_718},
        "supported_parameters": ["max_tokens", "tools", "tool_choice", "structured_outputs", "response_format", "reasoning"],
        "reasoning": {"mandatory": False, "default_enabled": True, "supported_efforts": ["max", "high", "low", "none"]},
        "pricing": {"prompt": "0.000000045", "completion": "0.0000005", "input_cache_read": "0.000000006"},
    }

    async def model(judge):
        assert judge == JUDGE
        return metadata

    async def render(logs):
        assert logs == ["synthetic.eval"]
        return [{"source": "synthetic.eval", "transcript_id": "synthetic", "rendered_input_bytes": 50_000, "schema_bytes": 3000}]

    monkeypatch.setattr(scan_budget, "model_metadata", model)
    monkeypatch.setattr(scan_budget, "rendered_inputs", render)
    return metadata


async def test_flash_plan_uses_published_rates_without_inventing_cache_write(flash):
    before = deepcopy(flash)
    planned = await scan_budget.plan(["synthetic.eval"], JUDGE, max_tokens=8192, single_attempt=True)
    assert flash == before
    assert planned["tariff"] == before["pricing"]
    assert "input_cache_write" not in planned["tariff"]
    assert planned["prompt_price_bound"] == 0.000000045
    assert planned["output_price"] == 0.0000005
    bound = 50_000 + 3000 + scan_budget.FRAMING_TOKENS
    assert planned["required_usd"] == pytest.approx(bound * 0.000000045 + 8192 * 0.0000005)
    assert planned["context_window"] == 1_048_576


@pytest.mark.parametrize("field", ["prompt", "input_cache_read", "completion", "input_cache_write", "input_cache_write_1h"])
@pytest.mark.parametrize("unknown", [None, "", True, "nan", "-1"])
async def test_flash_unknown_published_tariff_is_rejected(flash, field, unknown):
    flash["pricing"][field] = unknown
    with pytest.raises(ValueError, match="pricing"):
        await scan_budget.preflight(JUDGE, single_attempt=True)


@pytest.mark.parametrize("field", ["prompt", "input_cache_read", "completion"])
async def test_flash_required_published_tariff_must_exist(flash, field):
    del flash["pricing"][field]
    with pytest.raises(ValueError, match="pricing"):
        await scan_budget.preflight(JUDGE, single_attempt=True)


async def test_flash_future_published_write_rate_is_reserved_in_full(flash):
    flash["pricing"]["input_cache_write"] = "0.00000009"
    checked = await scan_budget.preflight(JUDGE, single_attempt=True)
    assert checked["prompt_price_bound"] == 0.00000009
    assert checked["tariff"]["input_cache_write"] == "0.00000009"


async def test_flash_saved_parts_recheck_the_same_published_tariffs(flash):
    from refund_eval.realism import scan_parts

    checked = await scan_budget.preflight(JUDGE, single_attempt=True)
    assert scan_parts.checked_policy(checked) == (scan_budget.FRAMING_TOKENS, (1_048_576 * 9) // 10)
    checked["prompt_price_bound"] = 0
    with pytest.raises(ValueError, match="price bound"):
        scan_parts.checked_policy(checked)


@pytest.mark.parametrize("parameter", ["max_tokens", "tools", "tool_choice", "structured_outputs", "response_format", "reasoning"])
async def test_flash_needs_public_capability_confirmation(flash, parameter):
    flash["supported_parameters"].remove(parameter)
    with pytest.raises(ValueError, match="capabilities"):
        await scan_budget.preflight(JUDGE, single_attempt=True)


@pytest.mark.parametrize("field,value", [
    ("id", "deepseek/deepseek-v4-pro"), ("context_length", None), ("context_length", True),
    ("reasoning", {"mandatory": True}), ("top_provider", {"max_completion_tokens": 4096}),
    ("top_provider", None), ("supported_parameters", [{}]),
])
async def test_flash_refuses_aliases_unknown_limits_and_mandatory_reasoning(flash, field, value):
    flash[field] = value
    with pytest.raises(ValueError, match="capabilities"):
        await scan_budget.preflight(JUDGE, single_attempt=True)


async def test_flash_never_expands_a_smaller_current_official_window(flash):
    flash["context_length"] = 600_000
    checked = await scan_budget.preflight(JUDGE, single_attempt=True)
    assert checked["context_window"] == 600_000


@pytest.mark.parametrize("suffix", [":online", ":free", "-latest"])
async def test_flash_suffix_is_not_treated_as_the_exact_model(flash, suffix):
    with pytest.raises(ValueError, match="unsupported"):
        await scan_budget.preflight(JUDGE + suffix, single_attempt=True)


def test_flash_registers_its_confirmed_window_without_a_pro_alias():
    scanners.register_judge_windows()
    info = get_model_info("deepseek/deepseek-v4.1-flash")
    assert info.context_length == 1_048_576
    assert info.family is None
    assert get_model_info("deepseek/deepseek-v4-pro").context_length == 700_000


def test_saved_flash_scan_requires_exact_bounded_policy():
    specification = {"model_roles": {"judge": {"model": JUDGE, "args": {"max_retries": 0},
        "config": scan_budget.bounded_tells_config(8192).model_dump(exclude_none=True)}},
        "scanners": {"tells": {"name": "tells", "params": {"bounded": True}}}}
    scan_budget.validate_saved_tells_scan(specification, judge=JUDGE, max_tokens=8192, single_attempt=True)
    specification["model_roles"]["judge"]["config"]["max_retries"] = 1
    with pytest.raises(ValueError, match="settings"):
        scan_budget.validate_saved_tells_scan(specification, judge=JUDGE, max_tokens=8192, single_attempt=True)
